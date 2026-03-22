"""Phase 5 — Main daemon orchestrator and lifecycle manager."""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from seeker.audio_capture import create_audio_task
from seeker.config import SeekerConfig
from seeker.gemini_session import GeminiSession
from seeker.manuscript_parser import Manuscript, load_manuscript
from seeker.prompt_builder import build_system_prompt, get_tool_declaration, load_prompt_template
from seeker.propresenter_client import ProPresenterClient, ProPresenterToolHandler

log = logging.getLogger(__name__)


async def _normalize_arrangement_with_llm(
    raw_arrangement: str,
    slide_labels: list[str],
    api_key: str,
) -> str:
    """Use Gemini text API to normalize arrangement abbreviations to match slide labels."""
    import aiohttp as _aiohttp

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + api_key
    )
    prompt = (
        "You are given a raw song arrangement from a PDF and a list of slide section labels "
        "from ProPresenter. Convert the arrangement into a numbered list where each entry uses "
        "the EXACT slide label name from the ProPresenter list. If a section has no matching slide "
        "(like 'Intro' or 'Instrumental'), keep the original name.\n\n"
        "Output ONLY the numbered list, nothing else. Example:\n"
        "1. Intro\n2. Verse 1\n3. Chorus 1\n4. Verse 2\n5. Chorus 1\n\n"
        f"ProPresenter slide labels: {slide_labels}\n\n"
        f"Raw arrangement:\n{raw_arrangement}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0},
    }
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.warning("LLM arrangement normalization failed (HTTP %d)", resp.status)
                    return ""
                data = await resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                log.info("LLM-normalized arrangement:\n%s", text.strip())
                return text.strip()
    except Exception as exc:
        log.warning("LLM arrangement normalization failed: %r", exc)
        return ""


def _expand_arrangement_abbrev(line: str) -> str:
    """Expand common arrangement abbreviations to match ProPresenter labels."""
    stripped = line.strip()
    # Remove performance notes after " - " (e.g. "V2 - Susanna" → "V2")
    section = re.split(r'\s*-\s+', stripped)[0].strip()
    # Remove modifiers like "x2", "x3", trailing notes in parens
    section = re.sub(r'\s*x\d+', '', section, flags=re.IGNORECASE)
    section = re.sub(r'\s*\(.*?\)', '', section)
    section = section.strip(' ,')
    if not section:
        return ""

    upper = section.upper()

    # Exact/prefix mappings — order matters (longer matches first)
    mappings = [
        # Verse variants
        (r'^V(\d+)$', r'Verse \1'),
        (r'^V$', 'Verse'),
        # Pre-chorus
        (r'^PC(\d*)$', lambda m: f'Pre-Chorus{" " + m.group(1) if m.group(1) else ""}'),
        # Chorus variants: C1A → Chorus 1, C1B → Chorus 1, C2 → Chorus 2, C → Chorus
        (r'^C(\d+)[A-Z]?$', r'Chorus \1'),
        (r'^C$', 'Chorus'),
        # Bridge variants
        (r'^B(\d+)([a-z])?$', lambda m: f'Bridge {m.group(1)}{m.group(2) or ""}'),
        (r'^B$', 'Bridge'),
        # Pass through known full names
        (r'^(Intro|Outro|Instrumental|Interlude|Ending|Turnaround|Bridge|Chorus|Verse).*$', r'\1'),
    ]

    for pattern, repl in mappings:
        m = re.match(pattern, section, re.IGNORECASE)
        if m:
            if callable(repl):
                return repl(m)
            return m.expand(repl)

    # Return as-is if no mapping matched (e.g. quoted lyrics, "Sing a Little Louder")
    return section


def _extract_arrangement(pdf_path: str, song_title: str) -> str:
    """Extract the arrangement for a specific song from an arrangement sheet PDF.

    Returns a numbered section-order list with expanded labels, or empty string.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        log.warning("pymupdf not installed — cannot read arrangement PDF")
        return ""

    path = Path(pdf_path)
    if not path.exists():
        log.warning("Arrangement PDF not found: %s", pdf_path)
        return ""

    text = ""
    doc = fitz.open(str(path))
    for page in doc:
        text += page.get_text()
    doc.close()

    # Split into song blocks — each starts with a song title line followed by
    # a key in parentheses, e.g. "Freedom (G)" or "1K HALLELUJAHS (D)"
    blocks = re.split(r'\n(?=\S+.*?\([A-G][b#]?\))', text)

    # Find the block whose header best matches the song title
    title_lower = song_title.lower().strip()
    for block in blocks:
        header = block.strip().split('\n')[0].lower()
        # Check if the song title words appear in the header
        if title_lower in header or all(w in header for w in title_lower.split()):
            # Trim: stop at lines that look like a different section (prayer, another song, etc.)
            lines = block.strip().split('\n')
            kept: list[str] = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if i > 2 and re.match(r'^(.*Prayer|Offering|Benediction|Closing)\b', stripped, re.IGNORECASE):
                    break
                if i > 2 and re.match(r'^.+\s*-\s*[A-G][b#]?$', stripped):
                    break
                kept.append(stripped)

            # Return raw arrangement lines (skip title + lead vocalist header)
            raw_lines = [l for l in kept[2:] if l.strip().strip('\u200b')]
            result = '\n'.join(raw_lines)
            log.info("Raw arrangement for '%s':\n%s", song_title, result)
            return result

    log.warning("No arrangement found for '%s' in PDF", song_title)
    return ""


class DaemonState(enum.Enum):
    INITIALIZING = "initializing"
    DORMANT = "dormant"
    ACTIVATING = "activating"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    KILLED = "killed"


class SeekerDaemon:
    """Top-level orchestrator that wires all subsystems together."""

    def __init__(self, config: SeekerConfig) -> None:
        self.config = config
        self.state = DaemonState.INITIALIZING

        # Subsystem handles (created on activation)
        self._audio_queue: asyncio.Queue[bytes] | None = None
        self._gemini_session: GeminiSession | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._pp_client: ProPresenterClient | None = None
        self._task_group_tasks: list[asyncio.Task[Any]] = []

        # Metrics
        self.current_slide_index = 0
        self.total_slides = 0
        self.session_start: float | None = None
        self.trigger_latencies: list[float] = []
        self.error_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, manuscript_path: str | None = None) -> None:
        """Entry-point: set up operator server, then wait for activation."""
        log.info("Seeker daemon starting...")
        self.state = DaemonState.DORMANT

        if manuscript_path or self.config.prompt.mode == "song":
            await self.activate(manuscript_path)

    async def activate(self, manuscript_path: str | None = None) -> None:
        """Transition from DORMANT → STREAMING."""
        self.state = DaemonState.ACTIVATING
        mode = self.config.prompt.mode

        self._http_session = aiohttp.ClientSession()
        self._pp_client = ProPresenterClient(self.config.propresenter, self._http_session)

        if mode == "song":
            # In song mode, fetch lyrics from ProPresenter
            slides = await self._pp_client.get_presentation_slides()
            pres = await self._pp_client.get_active_presentation()
            manuscript = Manuscript.from_slide_infos(
                slides, title=pres.name if pres else ""
            )
            log.info(
                "Song mode: fetched %d slides from ProPresenter (%s)",
                len(slides),
                manuscript.title,
            )
            template = load_prompt_template(self.config.prompt.song_template)
            tool_decl = get_tool_declaration("song")
            presentation_uuid = pres.uuid if pres else ""

            # Load arrangement
            song_arrangement = ""
            arr_path = self.config.prompt.arrangement_pdf
            if arr_path:
                if arr_path.endswith(".txt"):
                    # Plain text arrangement file — use as-is
                    song_arrangement = Path(arr_path).read_text(encoding="utf-8").strip()
                    log.info("Loaded arrangement from text file:\n%s", song_arrangement)
                else:
                    # PDF arrangement — extract and normalize
                    raw_arrangement = _extract_arrangement(arr_path, manuscript.title)
                    if raw_arrangement:
                        slide_labels = [s.group_name for s in slides]
                        normalized = await _normalize_arrangement_with_llm(
                            raw_arrangement, slide_labels, self.config.gemini.api_key,
                        )
                        if normalized:
                            song_arrangement = normalized
                        else:
                            expanded_lines = [_expand_arrangement_abbrev(l) for l in raw_arrangement.split('\n') if l.strip()]
                            song_arrangement = '\n'.join(f"{i+1}. {l}" for i, l in enumerate(expanded_lines) if l)
        else:
            # Sermon mode: load manuscript from file
            song_arrangement = ""
            presentation_uuid = ""
            if not manuscript_path:
                raise ValueError("Manuscript path required for sermon mode")
            log.info("Activating with manuscript: %s", manuscript_path)
            manuscript = load_manuscript(manuscript_path)
            template = load_prompt_template(self.config.prompt.template)
            tool_decl = get_tool_declaration("sermon")

        self.total_slides = len(manuscript.blocks)
        system_prompt = build_system_prompt(
            template,
            manuscript,
            anticipation_seconds=(
                self.config.prompt.anticipation_seconds if mode == "song" else None
            ),
            mode=mode if mode == "song" else "",
            song_arrangement=song_arrangement if mode == "song" else "",
        )

        # Create subsystems
        self._audio_queue = asyncio.Queue(maxsize=self.config.audio.queue_max_size)
        tool_handler = ProPresenterToolHandler(self._pp_client, presentation_uuid)
        self._gemini_session = GeminiSession(
            self.config.gemini, self._audio_queue, tool_handler
        )

        # Connect to Gemini
        await self._gemini_session.connect()
        await self._gemini_session.send_setup(system_prompt, [tool_decl])

        self.session_start = time.monotonic()
        self.state = DaemonState.STREAMING
        log.info("Streaming — tracking %d slides.", self.total_slides)

        # Run concurrent tasks
        if self.config.audio.audio_file:
            from seeker.file_audio import create_file_audio_task
            audio_task_coro = create_file_audio_task(self.config.audio, self._audio_queue)
        else:
            audio_task_coro = create_audio_task(self.config.audio, self._audio_queue)

        tasks = [
            asyncio.create_task(audio_task_coro),
            asyncio.create_task(self._gemini_session.stream_audio()),
            asyncio.create_task(self._gemini_session.receive_messages()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.deactivate()

    async def deactivate(self) -> None:
        """Gracefully stop streaming and return to DORMANT."""
        log.info("Deactivating...")
        if self._gemini_session:
            await self._gemini_session.disconnect()
        if self._http_session:
            await self._http_session.close()
        self.state = DaemonState.DORMANT
        log.info("Returned to dormant state.")

    async def kill(self) -> None:
        """Emergency stop — sever Gemini connection immediately."""
        log.warning("KILL SWITCH activated.")
        if self._gemini_session:
            await self._gemini_session.disconnect()
        if self._http_session:
            await self._http_session.close()
        self.state = DaemonState.KILLED
        log.warning("Daemon killed. Manual control restored.")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        duration = 0.0
        if self.session_start:
            duration = time.monotonic() - self.session_start

        avg_latency = (
            sum(self.trigger_latencies) / len(self.trigger_latencies)
            if self.trigger_latencies
            else 0.0
        )
        last_latency = self.trigger_latencies[-1] if self.trigger_latencies else 0.0

        return {
            "state": self.state.value,
            "current_slide_index": self.current_slide_index,
            "total_slides": self.total_slides,
            "session_duration_s": round(duration, 1),
            "gemini_connected": self._gemini_session is not None and self._gemini_session._running,
            "propresenter_connected": self._pp_client is not None,
            "audio_queue_depth": self._audio_queue.qsize() if self._audio_queue else 0,
            "last_trigger_latency_ms": round(last_latency, 1),
            "avg_trigger_latency_ms": round(avg_latency, 1),
            "errors_count": self.error_count,
        }


# ------------------------------------------------------------------
# Operator HTTP server
# ------------------------------------------------------------------


class OperatorServer:
    """Lightweight HTTP API for operator controls (kill-switch, status, etc.)."""

    def __init__(self, daemon: SeekerDaemon, config: SeekerConfig) -> None:
        self.daemon = daemon
        self.config = config
        self._app = web.Application()
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_post("/api/activate", self._handle_activate)
        self._app.router.add_post("/api/deactivate", self._handle_deactivate)
        self._app.router.add_post("/api/kill", self._handle_kill)

    async def start(self) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self.config.operator.http_host,
            self.config.operator.http_port,
        )
        await site.start()
        log.info(
            "Operator server listening on %s:%d",
            self.config.operator.http_host,
            self.config.operator.http_port,
        )

    # -- Route handlers --

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.daemon.get_status())

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_activate(self, request: web.Request) -> web.Response:
        body = await request.json() if request.content_length else {}
        manuscript = body.get("manuscript", self.daemon.config.prompt.manuscript)
        if not manuscript and self.daemon.config.prompt.mode != "song":
            return web.json_response({"error": "No manuscript specified"}, status=400)
        asyncio.create_task(self.daemon.activate(manuscript or None))
        return web.json_response({"status": "activating"})

    async def _handle_deactivate(self, _request: web.Request) -> web.Response:
        await self.daemon.deactivate()
        return web.json_response({"status": "dormant"})

    async def _handle_kill(self, _request: web.Request) -> web.Response:
        await self.daemon.kill()
        return web.json_response({"status": "killed"})
