"""Tests for the prompt builder."""

from __future__ import annotations

from seeker.config import GeminiConfig
from seeker.manuscript_parser import Manuscript, SlideBlock
from seeker.prompt_builder import (
    SLIDE_TOOL_DECLARATION,
    SONG_SLIDE_TOOL_DECLARATION,
    build_setup_payload,
    build_system_prompt,
    get_tool_declaration,
)


class TestBuildSystemPrompt:
    def test_injects_manuscript(self):
        template = "INSTRUCTIONS\n\n{manuscript_xml}"
        manuscript = Manuscript(blocks=[SlideBlock(index=0, content="Hello")])
        prompt = build_system_prompt(template, manuscript)
        assert "<presentation_manuscript>" in prompt
        assert "Hello" in prompt
        assert "INSTRUCTIONS" in prompt

    def test_no_placeholder_unchanged(self):
        template = "No placeholder here."
        manuscript = Manuscript(blocks=[])
        prompt = build_system_prompt(template, manuscript)
        assert prompt == "No placeholder here."


class TestBuildSetupPayload:
    def test_payload_structure(self):
        config = GeminiConfig(api_key="key")
        payload = build_setup_payload(
            system_prompt="test prompt",
            tools=[SLIDE_TOOL_DECLARATION],
            config=config,
        )
        setup = payload["setup"]
        assert setup["model"] == config.model
        assert setup["generationConfig"]["temperature"] == 0.0
        assert setup["systemInstruction"]["parts"][0]["text"] == "test prompt"
        assert len(setup["tools"][0]["functionDeclarations"]) == 1
        assert setup["contextWindowCompression"]["slidingWindow"]["targetTokens"] == 100000

    def test_resumption_handle(self):
        config = GeminiConfig(api_key="key")
        payload = build_setup_payload("p", [SLIDE_TOOL_DECLARATION], config, resumption_handle="abc123")
        assert payload["setup"]["sessionResumption"]["handle"] == "abc123"


class TestToolDeclaration:
    def test_has_required_fields(self):
        assert SLIDE_TOOL_DECLARATION["name"] == "trigger_presentation_slide"
        params = SLIDE_TOOL_DECLARATION["parameters"]
        assert "next_slide_index" in params["properties"]
        assert "next_slide_index" in params["required"]


class TestSongToolDeclaration:
    def test_has_section_label_property(self):
        props = SONG_SLIDE_TOOL_DECLARATION["parameters"]["properties"]
        assert "section_label" in props
        assert props["section_label"]["type"] == "STRING"

    def test_section_label_not_required(self):
        required = SONG_SLIDE_TOOL_DECLARATION["parameters"]["required"]
        assert "section_label" not in required

    def test_same_function_name(self):
        assert SONG_SLIDE_TOOL_DECLARATION["name"] == "trigger_presentation_slide"


class TestGetToolDeclaration:
    def test_sermon_mode(self):
        decl = get_tool_declaration("sermon")
        assert decl is SLIDE_TOOL_DECLARATION

    def test_song_mode(self):
        decl = get_tool_declaration("song")
        assert decl is SONG_SLIDE_TOOL_DECLARATION

    def test_default_is_sermon(self):
        decl = get_tool_declaration()
        assert decl is SLIDE_TOOL_DECLARATION


class TestBuildSystemPromptAnticipation:
    def test_replaces_anticipation_seconds(self):
        template = "Lead time: {anticipation_seconds}s\n\n{manuscript_xml}"
        manuscript = Manuscript(blocks=[SlideBlock(index=0, content="Hello")])
        prompt = build_system_prompt(
            template, manuscript, anticipation_seconds=1.5, mode="song"
        )
        assert "Lead time: 1.5s" in prompt
        assert 'type="song"' in prompt

    def test_no_anticipation_leaves_placeholder(self):
        template = "Lead time: {anticipation_seconds}s\n\n{manuscript_xml}"
        manuscript = Manuscript(blocks=[])
        prompt = build_system_prompt(template, manuscript)
        assert "{anticipation_seconds}" in prompt
