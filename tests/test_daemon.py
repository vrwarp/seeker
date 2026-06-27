"""Tests for the daemon's closed-loop reconcile / operator auto-yield logic."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from seeker.config import SeekerConfig
from seeker.daemon import SeekerDaemon
from seeker.tracking import TrackingState


def _daemon(observed_index, *, poll=0.01, cooldown=5.0) -> SeekerDaemon:
    config = SeekerConfig()
    config.propresenter.drift_poll_interval_s = poll
    config.propresenter.auto_yield_cooldown_s = cooldown
    daemon = SeekerDaemon(config)
    daemon._pp_client = MagicMock()
    daemon._pp_client.get_current_slide_index = AsyncMock(return_value=observed_index)
    return daemon


async def _run_reconcile_briefly(daemon: SeekerDaemon, duration: float = 0.05) -> None:
    task = asyncio.create_task(daemon._reconcile_loop())
    await asyncio.sleep(duration)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestReconcileLoop:
    @pytest.mark.asyncio
    async def test_detects_operator_override(self):
        daemon = _daemon(observed_index=7)
        daemon._tracking = TrackingState(total_slides=10, current_index=4, commanded_index=4)

        await _run_reconcile_briefly(daemon)

        assert daemon._tracking.operator_override is True
        assert daemon._tracking.current_index == 7  # re-anchored to where the operator went
        assert daemon.override_count >= 1

    @pytest.mark.asyncio
    async def test_agent_confirmed_change_is_not_an_override(self):
        # Observed slide equals what the agent commanded -> just adopt it, no override.
        daemon = _daemon(observed_index=7)
        daemon._tracking = TrackingState(total_slides=10, current_index=4, commanded_index=7)

        await _run_reconcile_briefly(daemon)

        assert daemon._tracking.operator_override is False
        assert daemon._tracking.current_index == 7
        assert daemon.override_count == 0

    @pytest.mark.asyncio
    async def test_stale_override_clears_after_cooldown(self):
        daemon = _daemon(observed_index=4, cooldown=5.0)
        # Override happened well outside the cooldown window; observed matches current.
        daemon._tracking = TrackingState(
            total_slides=10,
            current_index=4,
            commanded_index=4,
            operator_override=True,
            override_at=time.monotonic() - 100.0,
        )

        await _run_reconcile_briefly(daemon)

        assert daemon._tracking.operator_override is False

    @pytest.mark.asyncio
    async def test_tolerates_unavailable_slide_status(self):
        # get_current_slide_index returning None must not raise or change state.
        daemon = _daemon(observed_index=None)
        daemon._tracking = TrackingState(total_slides=10, current_index=4, commanded_index=4)

        await _run_reconcile_briefly(daemon)

        assert daemon._tracking.current_index == 4
        assert daemon._tracking.operator_override is False
