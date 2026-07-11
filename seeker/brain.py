"""Provider-agnostic protocols for realtime slide brains.

The daemon does not care *who* decides a slide change or *when* the decision
was made — only that audio flows in and tool calls come out. Every brain
(Gemini Live, OpenAI Realtime conductor, a future gpt-live full-duplex
session) presents this same surface, so swapping models is a config change,
not a rewrite.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, Protocol


class ToolHandler(Protocol):
    """Protocol for handling model tool calls locally."""

    async def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


class RealtimeBrain(Protocol):
    """A realtime model session that listens to audio and fires slide tools."""

    async def connect(self) -> None: ...

    async def send_setup(self, system_prompt: str, tools: list[dict]) -> None: ...

    def run_coros(self) -> list[Coroutine[Any, Any, None]]:
        """Long-running coroutines the daemon should supervise as tasks."""
        ...

    async def disconnect(self, reconnecting: bool = False) -> None: ...

    def notify_external_slide_change(self, index: int) -> None:
        """Tell the brain a human (not the model) moved the slide."""
        ...
