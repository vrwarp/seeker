"""OpenAI Realtime API session — the conductor-driven full-duplex pivot.

Replaces server-VAD-gated cognition (the Gemini failure mode on continuous
worship audio) with a client-owned decision clock. See
docs/design/gpt-live-pivot.md for the full rationale. In short:

  * Audio streams to the server continuously via ``input_audio_buffer.append``;
    with ``turn_detection: null`` the server never segments it and ingestion
    never pauses — there is no half-duplex dead zone and no VAD anywhere in
    the trigger path.
  * A commit cadence turns the buffer into conversation audio items (each
    commit also yields an input transcription, which feeds the lexical
    :class:`~seeker.position_tracker.PositionTracker`).
  * The conductor requests a decision (``response.create``) the moment the
    tracker senses a boundary, and otherwise at least every
    ``tick_max_interval_s``. A decision is text-only: a
    ``trigger_presentation_slide`` call or the word ``HOLD``.
  * In song mode the tracker may fire unambiguous verbatim lyric matches
    directly (still routed through ``evaluate_trigger``) without waiting for
    the model.

``turn_mode`` keeps the same class ready for gpt-live's API: ``"conductor"``
is today's mode; ``"full_duplex"`` disables commits/ticks entirely on the
assumption the model acts on its own, which is how OpenAI describes gpt-live
("decisions many times per second: … or invoke a tool"); ``"server_vad"``
reproduces the turn-segmented baseline for A/B comparison.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from array import array
from collections.abc import Coroutine
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from seeker.brain import ToolHandler
from seeker.config import OpenAIConfig
from seeker.position_tracker import PositionTracker, detect_repeat_cue

log = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.WARNING)

TOOL_NAME = "trigger_presentation_slide"


class LinearResampler:
    """Streaming linear-interpolation PCM16 resampler (pure Python).

    The realtime API accepts only 24 kHz mono PCM16; capture hardware and the
    legacy Gemini path run at 16 kHz. Carries fractional phase and the last
    sample across chunks so chunk boundaries stay continuous.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._phase = 0.0
        self._last: int | None = None

    def process(self, data: bytes) -> bytes:
        if self.src_rate == self.dst_rate or not data:
            return data
        chunk = array("h")
        chunk.frombytes(data)
        if self._last is not None:
            samples = array("h", [self._last])
            samples.extend(chunk)
        else:
            samples = chunk
        n = len(samples)
        if n < 2:
            if n:
                self._last = samples[-1]
            return b""

        out = array("h")
        step = self.src_rate / self.dst_rate
        pos = self._phase
        limit = n - 1
        while pos < limit - 1e-9:
            i = int(pos)
            frac = pos - i
            a = samples[i]
            b = samples[i + 1]
            out.append(int(a + (b - a) * frac))
            pos += step
        self._phase = pos - limit
        self._last = samples[-1]
        return out.tobytes()


class OpenAIRealtimeSession:
    """Manages a GA Realtime API WebSocket session as a silent slide brain.

    Responsibilities:
      - Connect, configure the session (no VAD, text-only output, tools,
        streaming input transcription, far-field noise reduction).
      - Stream audio continuously; commit + request decisions on its own clock.
      - Dispatch tool calls to the local handler and return outputs.
      - Fuse the deterministic PositionTracker (boundary ticks + autofire).
      - Rotate sessions ahead of the 60-minute cap and reconnect with backoff,
        re-anchoring the fresh conversation from client-held state.
    """

    def __init__(
        self,
        config: OpenAIConfig,
        audio_queue: asyncio.Queue[bytes],
        tool_handler: ToolHandler,
        audio_config: Any = None,
        tracker: PositionTracker | None = None,
        mode: str = "sermon",
    ) -> None:
        self.config = config
        self.audio_queue = audio_queue
        self.tool_handler = tool_handler
        self.audio_config = audio_config
        self.tracker = tracker
        self.mode = mode

        self._ws: ClientConnection | None = None
        self._running = False
        self._configured = False
        self._reconnect_lock = asyncio.Lock()
        self._system_prompt = ""
        self._tools: list[dict] = []

        src_rate = getattr(audio_config, "sample_rate", config.sample_rate) or config.sample_rate
        self._resampler = LinearResampler(src_rate, config.sample_rate)

        # Conductor state
        self._pending_audio_ms = 0.0
        self._uncommitted_audio = False
        self._committed_since_decision = False
        self._response_active = False
        self._boundary_tick = False
        self._last_commit_at = 0.0
        self._last_tick_at = 0.0
        self._last_hint_at = 0.0
        self._session_started_at = 0.0
        self._handled_call_ids: set[str] = set()

        # Client-held anchor for reconnect/rotation re-priming.
        self._slide_state_text = ""

        # Metrics (surfaced via daemon /api/status)
        self.stats: dict[str, Any] = {
            "ticks": 0,
            "commits": 0,
            "model_fires": 0,
            "tracker_fires": 0,
            "holds": 0,
            "rotations": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WSS connection to the Realtime API."""
        url = f"{self.config.base_url}?model={self.config.model}"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        self._configured = False
        self._ws = await websockets.connect(url, additional_headers=headers)
        self._running = True
        self._session_started_at = time.monotonic()
        self._pending_audio_ms = 0.0
        self._uncommitted_audio = False
        self._response_active = False
        log.info("OpenAI Realtime WebSocket connected (model=%s).", self.config.model)

    async def disconnect(self, reconnecting: bool = False) -> None:
        """Gracefully close the WebSocket."""
        if not reconnecting:
            self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        log.info("OpenAI Realtime WebSocket disconnected.")

    async def send_setup(self, system_prompt: str, tools: list[dict]) -> None:
        """Configure the session and prime the fresh conversation."""
        assert self._ws is not None
        self._system_prompt = system_prompt
        self._tools = tools

        # The server greets with session.created before accepting updates.
        await self._await_event("session.created")

        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": system_prompt,
            "output_modalities": ["text"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.config.sample_rate},
                    "turn_detection": self._turn_detection(),
                    "transcription": self._transcription(),
                    "noise_reduction": (
                        {"type": self.config.noise_reduction}
                        if self.config.noise_reduction
                        else None
                    ),
                },
            },
            "tools": tools,
            "tool_choice": "auto",
            "max_output_tokens": self.config.max_response_tokens,
            "truncation": {
                "type": "retention_ratio",
                "retention_ratio": self.config.truncation_retention_ratio,
                "token_limits": {
                    "post_instructions": self.config.post_instructions_token_limit,
                },
            },
        }
        if self.config.reasoning_effort:
            session["reasoning"] = {"effort": self.config.reasoning_effort}

        await self._send({"type": "session.update", "session": session})
        await self._await_event("session.updated")
        self._configured = True
        log.info(
            "Session configured (turn_mode=%s, transcription=%s).",
            self.config.turn_mode,
            self.config.transcribe_model,
        )

        # Re-anchor a fresh conversation from client-held state (rotation or
        # reconnect): the server conversation is gone, but our position isn't.
        if self._slide_state_text:
            await self._post_state_item(
                f"Session restarted. {self._slide_state_text} Continue tracking from there."
            )

    def _turn_detection(self) -> dict[str, Any] | None:
        if self.config.turn_mode == "server_vad":
            # A/B baseline: server segments turns and auto-responds.
            return {"type": "server_vad", "create_response": True, "interrupt_response": False}
        # conductor and full_duplex: the server never segments.
        return None

    def _transcription(self) -> dict[str, Any] | None:
        if not self.config.transcribe_model:
            return None
        transcription: dict[str, Any] = {"model": self.config.transcribe_model}
        if self.config.transcribe_language:
            transcription["language"] = self.config.transcribe_language
        # `delay` is only honored by gpt-realtime-whisper.
        if self.config.transcribe_delay and "whisper" in self.config.transcribe_model:
            transcription["delay"] = self.config.transcribe_delay
        return transcription

    def run_coros(self) -> list[Coroutine[Any, Any, None]]:
        """Long-running coroutines for the daemon to supervise."""
        coros = [self.stream_audio(), self.receive_messages()]
        if self.config.turn_mode == "conductor":
            coros.append(self.conduct())
        return coros

    # ------------------------------------------------------------------
    # Audio streaming (egress)
    # ------------------------------------------------------------------

    async def stream_audio(self) -> None:
        """Continuously read capture chunks, resample, and append.

        Appends are fire-and-forget and never segmented server-side: this loop
        runs identically whether the model is idle, deciding, or rotating.
        """
        while self._running:
            try:
                pcm = await asyncio.wait_for(self.audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if self._ws is None:
                continue

            pcm = self._resampler.process(pcm)
            if not pcm:
                continue
            message = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
            try:
                await self._ws.send(json.dumps(message))
            except websockets.exceptions.ConnectionClosed:
                log.debug("WebSocket closed during audio append.")
                continue
            self._pending_audio_ms += (len(pcm) / 2) / self.config.sample_rate * 1000.0
            self._uncommitted_audio = True

    # ------------------------------------------------------------------
    # The conductor — Seeker's own decision clock
    # ------------------------------------------------------------------

    async def conduct(self) -> None:
        """Commit audio and request decisions on Seeker's clock, not a VAD's.

        Cadence policy:
          * commit at least every ``commit_interval_s`` (keeps transcription —
            the tracker's feed — flowing, and keeps context fresh);
          * decide immediately (bounded by ``tick_min_interval_s``) when the
            tracker senses the current slide's ending or a plausible entry
            into the next slide;
          * otherwise decide every ``tick_max_interval_s`` while new audio has
            arrived — dead air never burns responses.
        """
        cfg = self.config
        try:
            while self._running:
                await asyncio.sleep(0.05)
                # Never commit or tick a session that isn't fully configured
                # (e.g. mid-reconnect): a response before session.update lands
                # would run with default settings — including audio output.
                if self._ws is None or not self._configured or not self._running:
                    continue

                now = time.monotonic()

                # Proactive rotation ahead of the hard 60-min session cap.
                if (
                    cfg.session_rotate_s > 0
                    and now - self._session_started_at >= cfg.session_rotate_s
                    and not self._response_active
                ):
                    log.info("Rotating session ahead of the 60-minute cap.")
                    self.stats["rotations"] += 1
                    await self._reconnect()
                    continue

                if self._pending_audio_ms >= cfg.min_commit_ms and (
                    now - self._last_commit_at >= cfg.commit_interval_s
                ):
                    await self._commit()

                since_tick = now - self._last_tick_at
                boundary_due = self._boundary_tick and since_tick >= cfg.tick_min_interval_s
                cadence_due = self._committed_since_decision and (
                    since_tick >= cfg.tick_max_interval_s
                )
                if (boundary_due or cadence_due) and not self._response_active:
                    # Sweep in the freshest audio right before deciding.
                    if self._pending_audio_ms >= cfg.min_commit_ms:
                        await self._commit()
                    await self._request_decision()
        except asyncio.CancelledError:
            return

    async def _commit(self) -> None:
        if self._ws is None or not self._uncommitted_audio:
            return
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except websockets.exceptions.ConnectionClosed:
            return
        self._pending_audio_ms = 0.0
        self._uncommitted_audio = False
        self._committed_since_decision = True
        self._last_commit_at = time.monotonic()
        self.stats["commits"] += 1

    async def _request_decision(self) -> None:
        if self._ws is None:
            return
        payload = {
            "type": "response.create",
            "response": {
                "output_modalities": ["text"],
                "metadata": {"purpose": "decision_tick"},
            },
        }
        try:
            await self._ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            return
        self._response_active = True
        self._boundary_tick = False
        self._committed_since_decision = False
        self._last_tick_at = time.monotonic()
        self.stats["ticks"] += 1

    # ------------------------------------------------------------------
    # Message reception (ingress)
    # ------------------------------------------------------------------

    async def receive_messages(self) -> None:
        """Listen for server events and dispatch."""
        silence_s = 0
        while self._running:
            # Park while a (re)connect handshake owns the socket: _await_event
            # is reading it, and websockets forbids two concurrent recv() calls.
            if self._ws is None or not self._configured:
                await asyncio.sleep(0.2)
                continue
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
                silence_s = 0
            except asyncio.TimeoutError:
                silence_s += 30
                log.warning("No event from OpenAI for %ds", silence_s)
                continue
            except websockets.exceptions.ConnectionClosed:
                if not self._running:
                    break
                log.warning("WebSocket closed unexpectedly — reconnecting.")
                await self._reconnect()
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Undecodable event from server.")
                continue
            await self._dispatch(event)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype == "error":
            err = event.get("error", {})
            code = err.get("code", "")
            # Benign in a race: a tick landed while a response was in flight,
            # or a commit landed on an already-empty buffer.
            benign = ("conversation_already_has_active_response", "input_audio_buffer_commit_empty")
            if code in benign:
                log.debug("Realtime API notice: %s", code)
            else:
                log.error("Realtime API error: %s", err)
            return

        if etype == "response.created":
            self._response_active = True
            return

        if etype == "response.done":
            await self._on_response_done(event.get("response", {}))
            return

        if etype == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                await self._handle_function_call(item)
            return

        if etype.endswith("input_audio_transcription.completed"):
            transcript = event.get("transcript", "")
            if transcript.strip():
                log.info("Hearing: %s", transcript.strip())
                await self._on_transcript(transcript)
            return

        if etype.endswith("input_audio_transcription.failed"):
            log.warning("Input transcription failed: %s", event.get("error", {}))
            return

        if etype in (
            "session.created",
            "session.updated",
            "input_audio_buffer.committed",
            "conversation.item.created",
            "conversation.item.added",
            "conversation.item.done",
            "rate_limits.updated",
        ) or etype.startswith(("response.output_text", "response.text", "response.content_part",
                               "response.output_item.added",
                               "conversation.item.input_audio_transcription")):
            log.debug("Realtime event: %s", etype)
            return

        log.debug("Unhandled realtime event type: %s", etype)

    async def _on_response_done(self, response: dict[str, Any]) -> None:
        self._response_active = False

        usage = response.get("usage", {})
        self.stats["input_tokens"] += usage.get("input_tokens", 0)
        self.stats["output_tokens"] += usage.get("output_tokens", 0)
        details = usage.get("input_token_details", {})
        self.stats["cached_input_tokens"] += details.get("cached_tokens", 0)

        status = response.get("status", "")
        if status not in ("completed", "cancelled", ""):
            log.warning("Response ended with status=%s: %s", status, response.get("status_details"))

        for item in response.get("output", []):
            if item.get("type") == "function_call":
                await self._handle_function_call(item)
            elif item.get("type") == "message":
                text = "".join(
                    part.get("text", "")
                    for part in item.get("content", [])
                    if isinstance(part, dict)
                ).strip()
                if text:
                    self.stats["holds"] += 1
                    log.debug("Decision: %s", text)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def _handle_function_call(self, item: dict[str, Any]) -> None:
        call_id = item.get("call_id", "")
        if not call_id or call_id in self._handled_call_ids:
            return
        self._handled_call_ids.add(call_id)

        name = item.get("name", "")
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            log.warning("Tool call %s had undecodable arguments.", name)
            args = {}

        log.info("Model tool call: %s(%s)", name, args)
        result = await self.tool_handler.handle(name, args)

        await self._send_json_safe(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            }
        )
        # Deliberately no follow-up response.create: Seeker never wants the
        # model to talk about the result; the next tick carries on.

        if result.get("ok"):
            self.stats["model_fires"] += 1
            index = args.get("next_slide_index")
            if isinstance(index, int):
                self._record_slide(index, source="model")

    # ------------------------------------------------------------------
    # Tracker fusion (perception plane)
    # ------------------------------------------------------------------

    async def _on_transcript(self, transcript: str) -> None:
        if self.tracker is None:
            return

        # Spoken leader cues ("one more time!") precede ad-lib repeats that no
        # arrangement predicts — warn the model and make it think now.
        cue = detect_repeat_cue(transcript) if self.mode == "song" else None

        self.tracker.feed(transcript)
        proposal = self.tracker.propose()

        if (
            proposal is not None
            and self.mode == "song"
            and self.config.tracker_autofire
            and proposal.confidence >= self.config.tracker_autofire_confidence
            and proposal.margin >= self.config.tracker_autofire_margin
        ):
            # Verbatim lyric evidence with a clear win over every rival
            # hypothesis (including repeats and off-plan jumps): fire without
            # waiting for a tick. The handler still applies every
            # evaluate_trigger guard.
            result = await self.tool_handler.handle(
                TOOL_NAME,
                {"next_slide_index": proposal.index, "section_label": proposal.reason},
            )
            if result.get("ok"):
                self.stats["tracker_fires"] += 1
                self._record_slide(proposal.index, source=f"tracker:{proposal.reason}")
                await self._post_state_item(self._slide_state_text)
            return

        if cue:
            self._boundary_tick = True
            await self._post_hint(
                f"[TRACKER] Leader cue heard: '{cue}'. Expect an ad-lib repeat of the "
                "current section even if the arrangement moves on — follow the singing."
            )
            return

        # Strong-but-ambiguous evidence (near-tie with a twin section or the
        # current slide → propose() returned None, or a proposal too close to
        # a rival to autofire): escalate to the model with the shortlist —
        # it can hear what text cannot show.
        strong_ambiguity = False
        if proposal is None and self.mode == "song" and self.tracker.arrangement:
            movers = [h for h in self.tracker.hypotheses() if h.reason != "current"]
            strong_ambiguity = bool(movers) and (
                movers[0].evidence >= self.tracker.propose_threshold
            )

        if proposal is not None or strong_ambiguity:
            self._boundary_tick = True
            await self._post_hint(self._format_hypotheses_hint())
        elif self.tracker.near_boundary():
            self._boundary_tick = True

    def _format_hypotheses_hint(self) -> str:
        top = [h for h in self.tracker.hypotheses() if h.evidence > 0.4][:3]
        if not top:
            return ""
        listing = "; ".join(
            f"slide {h.index}"
            + (f" '{h.label}'" if h.label else "")
            + f" ({h.reason}, {h.evidence:.2f})"
            for h in top
        )
        return (
            f"[TRACKER] The lyrics being sung match more than one slide: {listing}. "
            "Decide by ear (arrangement order, band cues, which repeat this is)."
        )

    async def _post_hint(self, text: str) -> None:
        """Inject a rate-limited advisory item for the model's next decision."""
        if not text:
            return
        now = time.monotonic()
        if now - self._last_hint_at < self.config.hint_cooldown_s:
            return
        self._last_hint_at = now
        await self._post_state_item(text)

    def _record_slide(self, index: int, source: str) -> None:
        label = ""
        if self.tracker is not None:
            self.tracker.anchor(index)
            label = self.tracker.label_for(index)
        label_part = f" ({label})" if label else ""
        self._slide_state_text = (
            f"[STATE] Slide {index}{label_part} is now on screen (via {source})."
        )

    def notify_external_slide_change(self, index: int) -> None:
        """A human moved the deck: re-anchor and tell the model to yield."""
        if self.tracker is not None:
            self.tracker.anchor(index)
        self._slide_state_text = f"[STATE] Operator manually moved to slide {index}."
        if self._running and self._ws is not None:
            asyncio.ensure_future(
                self._post_state_item(
                    f"{self._slide_state_text} Re-anchor to it and hold until the "
                    "audio clearly moves on."
                )
            )

    async def _post_state_item(self, text: str) -> None:
        """Inject a system item so the model always knows the true deck state."""
        if not text:
            return
        await self._send_json_safe(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    # ------------------------------------------------------------------
    # Reconnect / rotation
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Reconnect with backoff and re-prime the fresh conversation.

        There is no server-side session resumption: position state lives on
        the client (tracker + slide state text), so a fresh conversation costs
        only the re-priming system item.

        Rotation (conduct loop) and a ConnectionClosed (receive loop) can race
        into this; the lock makes the second caller wait for — not repeat —
        the first caller's reconnect.
        """
        if self._reconnect_lock.locked():
            async with self._reconnect_lock:
                return
        async with self._reconnect_lock:
            await self.disconnect(reconnecting=True)
            attempt = 0
            while self._running:
                try:
                    await self.connect()
                    if self._system_prompt:
                        await self.send_setup(self._system_prompt, self._tools)
                    log.info("Reconnected to OpenAI Realtime (attempt %d).", attempt + 1)
                    return
                except Exception as exc:
                    delay = min(
                        2**attempt + random.uniform(0, 1),
                        self.config.reconnect_max_backoff_s,
                    )
                    log.warning(
                        "Reconnect attempt %d failed: %s. Retrying in %.1fs",
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    async def _send_json_safe(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            log.warning("Cannot send %s: WebSocket closed.", payload.get("type"))

    async def _await_event(self, expected_type: str, timeout: float = 10.0) -> dict[str, Any]:
        """Read events until *expected_type* arrives (setup handshake only)."""
        assert self._ws is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {expected_type}")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            event = json.loads(raw)
            if event.get("type") == expected_type:
                return event
            if event.get("type") == "error":
                raise RuntimeError(f"Realtime API error during setup: {event.get('error')}")
            log.debug("Ignoring %s while waiting for %s", event.get("type"), expected_type)
