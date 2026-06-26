"""Phase 4 — ProPresenter 7 REST API client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from seeker.config import ProPresenterConfig
from seeker.tracking import TrackingState, evaluate_trigger

log = logging.getLogger(__name__)


@dataclass
class PresentationInfo:
    """Metadata about the currently active ProPresenter presentation."""

    uuid: str
    name: str
    slide_count: int
    current_index: int


@dataclass
class SlideInfo:
    """A single slide with its lyric text and group label."""

    index: int          # flat 0-based index across all groups
    text: str           # slide lyric text
    group_name: str     # "Verse 1", "Chorus", "Bridge", etc.


class ProPresenterClient:
    """Async HTTP client for ProPresenter 7's REST API."""

    def __init__(self, config: ProPresenterConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self._session = session

    @property
    def base_url(self) -> str:
        return self.config.base_url

    # ------------------------------------------------------------------
    # Slide triggers
    # ------------------------------------------------------------------

    async def trigger_next(self) -> bool:
        """Advance the active presentation by one slide (sequential)."""
        return await self._get_ok("/v1/presentation/active/next/trigger")

    async def trigger_index(self, uuid: str, index: int) -> bool:
        """Jump to a specific slide by presentation UUID and index."""
        return await self._get_ok(f"/v1/presentation/{uuid}/{index}/trigger")

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    async def get_active_presentation(self) -> PresentationInfo | None:
        """Return metadata about the currently active presentation."""
        data = await self._get_json("/v1/presentation/active")
        if data is None:
            return None
        # API nests under "presentation" key
        pres = data.get("presentation", data)
        groups = pres.get("groups", [])
        total_slides = sum(len(g.get("slides", [])) for g in groups)
        return PresentationInfo(
            uuid=pres.get("id", {}).get("uuid", ""),
            name=pres.get("id", {}).get("name", ""),
            slide_count=total_slides,
            current_index=pres.get("presentation_index", 0),
        )

    async def get_current_slide_index(self) -> int | None:
        """Return the index of the currently displayed slide."""
        data = await self._get_json("/v1/status/slide")
        if data is None:
            return None
        return data.get("current", {}).get("index")

    async def get_presentation_slides(self) -> list[SlideInfo]:
        """Fetch all slides from the active presentation with group labels."""
        data = await self._get_json("/v1/presentation/active")
        if data is None:
            return []
        # API nests under "presentation" key
        pres = data.get("presentation", data)
        slides: list[SlideInfo] = []
        flat_index = 0
        for group in pres.get("groups", []):
            group_name = group.get("name", "")
            for slide in group.get("slides", []):
                text = slide.get("text", "")
                if slide.get("enabled", True):
                    slides.append(SlideInfo(index=flat_index, text=text, group_name=group_name))
                flat_index += 1
        return slides

    async def health_check(self) -> bool:
        """Return True if the ProPresenter API is reachable."""
        return await self._get_ok("/version")

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _get_ok(self, path: str) -> bool:
        """Issue a GET request and return True if response is 2xx."""
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_s)
        try:
            async with self._session.get(url, timeout=timeout) as resp:
                ok = resp.ok
                if not ok:
                    log.warning("ProPresenter %s returned %d", path, resp.status)
                return ok
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.error("ProPresenter request failed (%s): %s", path, exc)
            return False

    async def _get_json(self, path: str) -> dict[str, Any] | None:
        """Issue a GET request and return JSON body, or None on failure."""
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_s)
        try:
            async with self._session.get(url, timeout=timeout) as resp:
                if resp.ok:
                    return await resp.json()
                log.warning("ProPresenter %s returned %d", path, resp.status)
                return None
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.error("ProPresenter request failed (%s): %s", path, exc)
            return None


class ProPresenterToolHandler:
    """Bridges Gemini tool calls to ProPresenter REST commands.

    Implements the ``ToolHandler`` protocol expected by ``GeminiSession``.

    When a :class:`~seeker.tracking.TrackingState` is supplied, every proposed
    trigger is run through :func:`~seeker.tracking.evaluate_trigger` first, so a
    hallucinated out-of-range index, a redundant re-fire, or a trigger that would
    fight a human operator is rejected before it ever reaches ProPresenter. On a
    successful trigger the handler records what it commanded so the reconcile loop
    can distinguish agent-driven slide changes from manual operator overrides.

    With no ``state`` it behaves exactly as before (unconstrained fire-and-forget).
    """

    def __init__(
        self,
        client: ProPresenterClient,
        presentation_uuid: str = "",
        *,
        state: TrackingState | None = None,
        max_jump: int | None = None,
        allow_nonlinear: bool = True,
        auto_yield_cooldown_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.presentation_uuid = presentation_uuid
        self.state = state
        self.max_jump = max_jump
        self.allow_nonlinear = allow_nonlinear
        self.auto_yield_cooldown_s = auto_yield_cooldown_s
        self._clock = clock

    async def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != "trigger_presentation_slide":
            log.warning("Unknown tool call: %s", name)
            return {"error": f"Unknown function: {name}"}

        slide_index = args.get("next_slide_index", -1)
        section_label = args.get("section_label", "")
        if section_label:
            log.info("Triggering slide → index %d [%s]", slide_index, section_label)
        else:
            log.info("Triggering slide → index %d", slide_index)

        if self.state is not None:
            decision = evaluate_trigger(
                self.state,
                slide_index,
                max_jump=self.max_jump,
                allow_nonlinear=self.allow_nonlinear,
                auto_yield_cooldown_s=self.auto_yield_cooldown_s,
                now=self._clock(),
            )
            if not decision.allow:
                log.warning(
                    "Slide trigger rejected (index=%d, current=%d): %s",
                    slide_index,
                    self.state.current_index,
                    decision.reason,
                )
                return {"ok": False, "reason": decision.reason}

        if self.presentation_uuid:
            success = await self.client.trigger_index(self.presentation_uuid, slide_index)
        else:
            success = await self.client.trigger_next()

        if success and self.state is not None:
            # Record the command so the reconcile loop attributes this slide change
            # to the agent (not a human), and optimistically advance the known
            # position; the reconcile loop confirms it against the real screen.
            self.state.commanded_index = slide_index
            self.state.commanded_at = self._clock()
            self.state.current_index = slide_index

        return {"ok": success}
