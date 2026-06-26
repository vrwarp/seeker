"""Tests for the ProPresenter client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from seeker.config import ProPresenterConfig
from seeker.propresenter_client import ProPresenterClient, ProPresenterToolHandler, SlideInfo
from seeker.tracking import TrackingState


class TestProPresenterClient:
    def test_base_url(self, pp_config):
        client = ProPresenterClient(pp_config, MagicMock())
        assert client.base_url == "http://127.0.0.1:50001"

    def test_base_url_custom_port(self):
        config = ProPresenterConfig(host="10.0.0.5", port=1025)
        client = ProPresenterClient(config, MagicMock())
        assert client.base_url == "http://10.0.0.5:1025"


class TestProPresenterToolHandler:
    @pytest.mark.asyncio
    async def test_triggers_by_index_when_uuid_provided(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)

        handler = ProPresenterToolHandler(mock_client, presentation_uuid="abc-123")
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 3})

        assert result["ok"] is True
        mock_client.trigger_index.assert_awaited_once_with("abc-123", 3)

    @pytest.mark.asyncio
    async def test_triggers_next_when_no_uuid(self):
        mock_client = MagicMock()
        mock_client.trigger_next = AsyncMock(return_value=True)

        handler = ProPresenterToolHandler(mock_client)
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 3})

        assert result["ok"] is True
        mock_client.trigger_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_unknown_function(self):
        handler = ProPresenterToolHandler(MagicMock())
        result = await handler.handle("unknown_func", {})
        assert "error" in result


class TestGetPresentationSlides:
    @pytest.mark.asyncio
    async def test_parses_groups_and_slides(self):
        mock_response = {
            "groups": [
                {"name": "Verse 1", "slides": [
                    {"text": "First verse line 1", "enabled": True},
                    {"text": "First verse line 2", "enabled": True},
                ]},
                {"name": "Chorus", "slides": [
                    {"text": "Chorus lyrics", "enabled": True},
                ]},
            ]
        }
        client = ProPresenterClient.__new__(ProPresenterClient)
        client.config = ProPresenterConfig()
        client._session = MagicMock()
        client._get_json = AsyncMock(return_value=mock_response)

        slides = await client.get_presentation_slides()
        assert len(slides) == 3
        assert slides[0] == SlideInfo(index=0, text="First verse line 1", group_name="Verse 1")
        assert slides[1] == SlideInfo(index=1, text="First verse line 2", group_name="Verse 1")
        assert slides[2] == SlideInfo(index=2, text="Chorus lyrics", group_name="Chorus")

    @pytest.mark.asyncio
    async def test_returns_empty_on_none(self):
        client = ProPresenterClient.__new__(ProPresenterClient)
        client._get_json = AsyncMock(return_value=None)
        slides = await client.get_presentation_slides()
        assert slides == []

    @pytest.mark.asyncio
    async def test_skips_disabled_slides(self):
        mock_response = {
            "groups": [
                {"name": "Verse 1", "slides": [
                    {"text": "Enabled", "enabled": True},
                    {"text": "Disabled", "enabled": False},
                ]},
            ]
        }
        client = ProPresenterClient.__new__(ProPresenterClient)
        client._get_json = AsyncMock(return_value=mock_response)
        slides = await client.get_presentation_slides()
        assert len(slides) == 1
        assert slides[0].text == "Enabled"


class TestToolHandlerSectionLabel:
    @pytest.mark.asyncio
    async def test_logs_section_label(self):
        mock_client = MagicMock()
        mock_client.trigger_next = AsyncMock(return_value=True)

        handler = ProPresenterToolHandler(mock_client)
        result = await handler.handle(
            "trigger_presentation_slide",
            {"next_slide_index": 1, "section_label": "Chorus"},
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_works_without_section_label(self):
        mock_client = MagicMock()
        mock_client.trigger_next = AsyncMock(return_value=True)

        handler = ProPresenterToolHandler(mock_client)
        result = await handler.handle(
            "trigger_presentation_slide",
            {"next_slide_index": 1},
        )
        assert result["ok"] is True


class TestToolHandlerWithTrackingState:
    @pytest.mark.asyncio
    async def test_records_command_and_position_on_success(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)
        state = TrackingState(total_slides=10, current_index=2)

        handler = ProPresenterToolHandler(
            mock_client, presentation_uuid="abc", state=state, clock=lambda: 42.0
        )
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 3})

        assert result["ok"] is True
        assert state.commanded_index == 3
        assert state.current_index == 3
        assert state.commanded_at == 42.0
        mock_client.trigger_index.assert_awaited_once_with("abc", 3)

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_without_calling_propresenter(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)
        state = TrackingState(total_slides=5, current_index=1)

        handler = ProPresenterToolHandler(mock_client, presentation_uuid="abc", state=state)
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 99})

        assert result["ok"] is False
        assert result["reason"] == "out_of_range"
        mock_client.trigger_index.assert_not_called()
        # State must not advance on a rejected trigger.
        assert state.current_index == 1
        assert state.commanded_index == -1

    @pytest.mark.asyncio
    async def test_suppresses_noop_for_current_slide(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)
        state = TrackingState(total_slides=5, current_index=2)

        handler = ProPresenterToolHandler(mock_client, presentation_uuid="abc", state=state)
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 2})

        assert result["ok"] is False
        assert result["reason"] == "noop_already_current"
        mock_client.trigger_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_enforces_locality_window_in_sequential_mode(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)
        state = TrackingState(total_slides=50, current_index=2)

        handler = ProPresenterToolHandler(
            mock_client, presentation_uuid="abc", state=state, max_jump=3, allow_nonlinear=False
        )
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 30})

        assert result["ok"] is False
        assert result["reason"] == "jump_too_large"
        mock_client.trigger_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_yields_to_operator_override(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=True)
        state = TrackingState(
            total_slides=10, current_index=4, operator_override=True, override_at=100.0
        )

        handler = ProPresenterToolHandler(
            mock_client,
            presentation_uuid="abc",
            state=state,
            auto_yield_cooldown_s=5.0,
            clock=lambda: 102.0,
        )
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 5})

        assert result["ok"] is False
        assert result["reason"] == "operator_override"
        mock_client.trigger_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_advance_state_when_trigger_fails(self):
        mock_client = MagicMock()
        mock_client.trigger_index = AsyncMock(return_value=False)
        state = TrackingState(total_slides=10, current_index=2)

        handler = ProPresenterToolHandler(mock_client, presentation_uuid="abc", state=state)
        result = await handler.handle("trigger_presentation_slide", {"next_slide_index": 3})

        assert result["ok"] is False
        assert state.current_index == 2
        assert state.commanded_index == -1
