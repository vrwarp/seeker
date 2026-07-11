"""Tests for the OpenAI Realtime conductor session (no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from seeker.config import OpenAIConfig
from seeker.openai_session import LinearResampler, OpenAIRealtimeSession
from seeker.position_tracker import PositionTracker


class FakeWebSocket:
    """Captures sent events; serves scripted incoming events."""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        for event in incoming or []:
            self.feed(event)

    def feed(self, event: dict) -> None:
        self._incoming.put_nowait(json.dumps(event))

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self) -> None:
        pass

    def sent_types(self) -> list[str]:
        return [m["type"] for m in self.sent]


class RecordingToolHandler:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.ok = ok

    async def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"ok": self.ok}


def make_session(
    handler: RecordingToolHandler | None = None,
    tracker: PositionTracker | None = None,
    mode: str = "sermon",
    **config_overrides,
) -> tuple[OpenAIRealtimeSession, RecordingToolHandler, FakeWebSocket]:
    config = OpenAIConfig(api_key="test-key", **config_overrides)
    handler = handler or RecordingToolHandler()
    session = OpenAIRealtimeSession(
        config,
        asyncio.Queue(),
        handler,
        audio_config=None,
        tracker=tracker,
        mode=mode,
    )
    ws = FakeWebSocket()
    session._ws = ws  # bypass network
    session._running = True
    return session, handler, ws


class TestLinearResampler:
    def test_passthrough_at_same_rate(self):
        r = LinearResampler(24_000, 24_000)
        data = b"\x01\x00" * 100
        assert r.process(data) == data

    def test_16k_to_24k_produces_three_halves_the_samples(self):
        r = LinearResampler(16_000, 24_000)
        total_out = 0
        for _ in range(10):
            out = r.process(b"\x00\x01" * 512)  # 512 samples per chunk
            total_out += len(out) // 2
        # 5120 samples in → ~7680 out (±1 for phase carry)
        assert abs(total_out - 7680) <= 1

    def test_constant_signal_stays_constant(self):
        r = LinearResampler(16_000, 24_000)
        out = r.process((1000).to_bytes(2, "little", signed=True) * 64)
        samples = [
            int.from_bytes(out[i : i + 2], "little", signed=True) for i in range(0, len(out), 2)
        ]
        assert samples and all(s == 1000 for s in samples)


class TestSetup:
    @pytest.mark.asyncio
    async def test_send_setup_configures_conductor_session(self):
        session, _, ws = make_session()
        ws.feed({"type": "session.created"})
        ws.feed({"type": "session.updated"})

        tool = {"type": "function", "name": "trigger_presentation_slide"}
        await session.send_setup("PROMPT", [tool])

        update = next(m for m in ws.sent if m["type"] == "session.update")
        cfg = update["session"]
        assert cfg["type"] == "realtime"
        assert cfg["instructions"] == "PROMPT"
        assert cfg["output_modalities"] == ["text"]
        assert cfg["audio"]["input"]["turn_detection"] is None  # no VAD anywhere
        assert cfg["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24_000}
        assert cfg["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"
        assert cfg["tools"] == [tool]
        assert cfg["truncation"]["type"] == "retention_ratio"

    @pytest.mark.asyncio
    async def test_server_vad_mode_configures_vad(self):
        session, _, ws = make_session(turn_mode="server_vad")
        ws.feed({"type": "session.created"})
        ws.feed({"type": "session.updated"})
        await session.send_setup("PROMPT", [])

        update = next(m for m in ws.sent if m["type"] == "session.update")
        assert update["session"]["audio"]["input"]["turn_detection"]["type"] == "server_vad"

    @pytest.mark.asyncio
    async def test_setup_reinjects_state_after_restart(self):
        session, _, ws = make_session()
        session._slide_state_text = "[STATE] Slide 7 is now on screen (via model)."
        ws.feed({"type": "session.created"})
        ws.feed({"type": "session.updated"})
        await session.send_setup("PROMPT", [])

        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert items and "Slide 7" in items[0]["item"]["content"][0]["text"]

    def test_conductor_runs_three_loops_but_full_duplex_does_not_tick(self):
        session, _, _ = make_session()
        coros = session.run_coros()
        assert len(coros) == 3
        for c in coros:
            c.close()

        session_fd, _, _ = make_session(turn_mode="full_duplex")
        coros = session_fd.run_coros()
        assert len(coros) == 2  # no conductor: the model acts on its own
        for c in coros:
            c.close()


class TestToolCalls:
    @pytest.mark.asyncio
    async def test_function_call_dispatches_and_returns_output(self):
        session, handler, ws = make_session()
        await session._dispatch(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "trigger_presentation_slide",
                    "arguments": '{"next_slide_index": 3}',
                },
            }
        )

        assert handler.calls == [("trigger_presentation_slide", {"next_slide_index": 3})]
        outputs = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert outputs[0]["item"]["type"] == "function_call_output"
        assert outputs[0]["item"]["call_id"] == "call_1"
        assert json.loads(outputs[0]["item"]["output"]) == {"ok": True}
        # Fire-and-forget: no follow-up response is requested.
        assert "response.create" not in ws.sent_types()
        assert session.stats["model_fires"] == 1

    @pytest.mark.asyncio
    async def test_duplicate_call_ids_fire_once(self):
        session, handler, _ = make_session()
        item = {
            "type": "function_call",
            "call_id": "call_dup",
            "name": "trigger_presentation_slide",
            "arguments": '{"next_slide_index": 1}',
        }
        await session._dispatch({"type": "response.output_item.done", "item": item})
        # The same completed call also appears in response.done output.
        await session._dispatch(
            {"type": "response.done", "response": {"status": "completed", "output": [item]}}
        )
        assert len(handler.calls) == 1

    @pytest.mark.asyncio
    async def test_rejected_trigger_does_not_count_as_fire(self):
        session, handler, _ = make_session(handler=RecordingToolHandler(ok=False))
        await session._dispatch(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "trigger_presentation_slide",
                    "arguments": '{"next_slide_index": 99}',
                },
            }
        )
        assert session.stats["model_fires"] == 0


class TestResponseLifecycle:
    @pytest.mark.asyncio
    async def test_response_done_clears_active_flag_and_counts_usage(self):
        session, _, _ = make_session()
        session._response_active = True
        await session._dispatch(
            {
                "type": "response.done",
                "response": {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 900,
                        "output_tokens": 12,
                        "input_token_details": {"cached_tokens": 850},
                    },
                },
            }
        )
        assert session._response_active is False
        assert session.stats["input_tokens"] == 900
        assert session.stats["cached_input_tokens"] == 850

    @pytest.mark.asyncio
    async def test_benign_race_errors_are_tolerated(self):
        session, _, _ = make_session()
        await session._dispatch(
            {
                "type": "error",
                "error": {"code": "conversation_already_has_active_response"},
            }
        )
        await session._dispatch(
            {"type": "error", "error": {"code": "input_audio_buffer_commit_empty"}}
        )


class TestConductor:
    @pytest.mark.asyncio
    async def test_commit_requires_pending_audio(self):
        session, _, ws = make_session()
        await session._commit()  # nothing appended yet
        assert "input_audio_buffer.commit" not in ws.sent_types()

        session._pending_audio_ms = 500.0
        session._uncommitted_audio = True
        await session._commit()
        assert "input_audio_buffer.commit" in ws.sent_types()
        assert session._pending_audio_ms == 0.0
        assert session._committed_since_decision is True

    @pytest.mark.asyncio
    async def test_decision_request_is_text_only_and_marks_active(self):
        session, _, ws = make_session()
        session._boundary_tick = True
        await session._request_decision()

        req = next(m for m in ws.sent if m["type"] == "response.create")
        assert req["response"]["output_modalities"] == ["text"]
        assert session._response_active is True
        assert session._boundary_tick is False
        assert session.stats["ticks"] == 1


SONG_BLOCKS = [
    (0, "Amazing grace how sweet the sound that saved a wretch like me", "Verse 1"),
    (1, "My chains are gone I've been set free my God my Savior has ransomed me", "Chorus"),
]


class TestTrackerFusion:
    @pytest.mark.asyncio
    async def test_transcript_autofires_verbatim_next_slide(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        session, handler, ws = make_session(tracker=tracker, mode="song")

        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "my chains are gone I've been set free",
            }
        )

        assert handler.calls and handler.calls[0][1]["next_slide_index"] == 1
        assert session.stats["tracker_fires"] == 1
        # The model is told what the tracker did.
        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert any("Slide 1" in i["item"]["content"][0]["text"] for i in items)

    @pytest.mark.asyncio
    async def test_boundary_evidence_requests_immediate_tick(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        session, handler, _ = make_session(
            tracker=tracker, mode="song", tracker_autofire=False
        )
        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "that saved a wretch like me",
            }
        )
        assert session._boundary_tick is True
        assert handler.calls == []  # autofire disabled: the model decides

    @pytest.mark.asyncio
    async def test_no_autofire_in_sermon_mode(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=None)
        session, handler, _ = make_session(tracker=tracker, mode="sermon")
        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "my chains are gone I've been set free",
            }
        )
        assert handler.calls == []

    @pytest.mark.asyncio
    async def test_adlib_jump_back_autofires_when_unambiguous(self):
        # Arrangement is exhausted (on the Chorus); the leader goes back to
        # Verse 1 anyway — an off-plan jump the old fixed-path tracker missed.
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        tracker.anchor(1)
        session, handler, _ = make_session(tracker=tracker, mode="song")
        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "amazing grace how sweet the sound",
            }
        )
        # Verse 1 restarting is the only strong hypothesis → tracker fires it.
        assert handler.calls and handler.calls[0][1]["next_slide_index"] == 0
        assert handler.calls[0][1]["section_label"] == "section_jump"

    @pytest.mark.asyncio
    async def test_repeat_cue_alerts_model_instead_of_firing(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        session, handler, ws = make_session(tracker=tracker, mode="song")
        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "come on church one more time",
            }
        )
        assert handler.calls == []
        assert session._boundary_tick is True
        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert any("one more time" in i["item"]["content"][0]["text"] for i in items)

    @pytest.mark.asyncio
    async def test_twin_sections_escalate_with_hypotheses_hint(self):
        blocks = [
            (0, "Verse words that are unique here in this line", "Verse 1"),
            (1, "Sing it out sing it loud forever and ever", "Chorus 1"),
            (2, "Sing it out sing it loud forever and ever", "Chorus 2"),
        ]
        tracker = PositionTracker(
            blocks=blocks, arrangement=["Verse 1", "Chorus 1", "Chorus 2"]
        )
        tracker.anchor(1)
        tracker.anchor(2)  # on Chorus 2, arrangement exhausted; twins remain
        session, handler, ws = make_session(tracker=tracker, mode="song")
        await session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "sing it out sing it loud forever and ever",
            }
        )
        assert handler.calls == []  # lexically undecidable — never guess
        assert session._boundary_tick is True
        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        hints = [i for i in items if "[TRACKER]" in i["item"]["content"][0]["text"]]
        assert hints and "more than one slide" in hints[0]["item"]["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_hints_are_rate_limited(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        session, _, ws = make_session(tracker=tracker, mode="song")
        for _ in range(3):
            await session._dispatch(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "one more time",
                }
            )
        hints = [
            m
            for m in ws.sent
            if m["type"] == "conversation.item.create"
            and "[TRACKER]" in m["item"]["content"][0]["text"]
        ]
        assert len(hints) == 1

    @pytest.mark.asyncio
    async def test_operator_override_reanchors_and_informs_model(self):
        tracker = PositionTracker(blocks=SONG_BLOCKS, arrangement=["Verse 1", "Chorus"])
        session, _, ws = make_session(tracker=tracker, mode="song")

        session.notify_external_slide_change(1)
        await asyncio.sleep(0)  # let the ensure_future task run

        assert tracker.current_index == 1
        items = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert any("Operator manually moved" in i["item"]["content"][0]["text"] for i in items)
