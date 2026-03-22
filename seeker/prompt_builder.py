"""Phase 3 — Prompt template builder for Gemini system instructions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from seeker.config import GeminiConfig
from seeker.manuscript_parser import Manuscript

# Default tool declaration for slide triggering
SLIDE_TOOL_DECLARATION: dict[str, Any] = {
    "name": "trigger_presentation_slide",
    "description": (
        "Advance the live presentation to the next slide. Call this function "
        "ONLY when the speaker has semantically completed the content of the "
        "current slide block and is moving to the next topic. The next_slide_index "
        "is the 0-based index of the target slide from the manuscript."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "next_slide_index": {
                "type": "INTEGER",
                "description": (
                    "The 0-based index of the next slide to display, "
                    "corresponding to the slide_block index in the manuscript."
                ),
            }
        },
        "required": ["next_slide_index"],
    },
}

SONG_SLIDE_TOOL_DECLARATION: dict[str, Any] = {
    "name": "trigger_presentation_slide",
    "description": (
        "Jump to a specific slide in the live presentation. In song mode, "
        "slides can be triggered non-linearly to handle repeated sections "
        "(e.g. returning to the chorus). The next_slide_index is the 0-based "
        "index of the target slide from the manuscript."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "next_slide_index": {
                "type": "INTEGER",
                "description": (
                    "The 0-based index of the target slide to display, "
                    "corresponding to the slide_block index in the manuscript."
                ),
            },
            "section_label": {
                "type": "STRING",
                "description": "Section being triggered (e.g. 'Chorus'). For logging only.",
            },
        },
        "required": ["next_slide_index"],
    },
}


def get_tool_declaration(mode: str = "sermon") -> dict[str, Any]:
    """Return the appropriate tool declaration for the given mode."""
    if mode == "song":
        return SONG_SLIDE_TOOL_DECLARATION
    return SLIDE_TOOL_DECLARATION


def load_prompt_template(path: str | Path) -> str:
    """Load a prompt template from disk."""
    return Path(path).read_text(encoding="utf-8")


def build_system_prompt(
    template: str,
    manuscript: Manuscript,
    *,
    anticipation_seconds: float | None = None,
    mode: str = "",
    song_arrangement: str = "",
) -> str:
    """Inject the manuscript XML into the prompt template."""
    result = template.replace("{manuscript_xml}", manuscript.to_xml(mode=mode))
    if anticipation_seconds is not None:
        result = result.replace("{anticipation_seconds}", str(anticipation_seconds))
    result = result.replace("{song_arrangement}", song_arrangement)
    return result


def build_setup_payload(
    system_prompt: str,
    tools: list[dict[str, Any]],
    config: GeminiConfig,
    resumption_handle: str | None = None,
) -> dict[str, Any]:
    """Construct the full ``BidiGenerateContentSetup`` JSON payload."""
    return {
        "setup": {
            "model": config.model,
            "generationConfig": {
                "responseModalities": ["TEXT"],
                "temperature": 0.0,
            },
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "tools": [{"functionDeclarations": tools}],
            "realtimeInputConfig": {
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            },
            "contextWindowCompression": {
                "slidingWindow": {
                    "targetTokens": config.target_tokens,
                },
            },
            "sessionResumption": {
                "handle": resumption_handle,
            },
        }
    }
