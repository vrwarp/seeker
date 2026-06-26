"""Tests for the slide-tracking state and trigger-safety policy."""

from __future__ import annotations

from seeker.tracking import TrackingState, evaluate_trigger


class TestEvaluateTrigger:
    def test_allows_normal_forward_advance(self):
        state = TrackingState(total_slides=10, current_index=3)
        decision = evaluate_trigger(state, 4)
        assert decision.allow is True
        assert decision.reason == "ok"
        assert decision.index == 4

    def test_rejects_negative_index(self):
        state = TrackingState(total_slides=10, current_index=3)
        decision = evaluate_trigger(state, -1)
        assert decision.allow is False
        assert decision.reason == "out_of_range"

    def test_rejects_index_at_or_beyond_total(self):
        state = TrackingState(total_slides=10, current_index=3)
        assert evaluate_trigger(state, 10).reason == "out_of_range"
        assert evaluate_trigger(state, 999).reason == "out_of_range"

    def test_bounds_skipped_when_total_unknown(self):
        # total_slides == 0 means "unknown"; large indices are allowed through.
        state = TrackingState(total_slides=0, current_index=3)
        assert evaluate_trigger(state, 500).allow is True

    def test_rejects_noop_for_current_slide(self):
        state = TrackingState(total_slides=10, current_index=3)
        decision = evaluate_trigger(state, 3)
        assert decision.allow is False
        assert decision.reason == "noop_already_current"

    def test_locality_rejects_large_jump_when_sequential(self):
        state = TrackingState(total_slides=50, current_index=3)
        decision = evaluate_trigger(state, 20, max_jump=5, allow_nonlinear=False)
        assert decision.allow is False
        assert decision.reason == "jump_too_large"

    def test_locality_allows_jump_within_window(self):
        state = TrackingState(total_slides=50, current_index=3)
        assert evaluate_trigger(state, 7, max_jump=5, allow_nonlinear=False).allow is True

    def test_locality_ignored_when_nonlinear_allowed(self):
        # Song / v1.1 scripture modes legitimately jump far (chorus return, re-read).
        state = TrackingState(total_slides=50, current_index=3)
        decision = evaluate_trigger(state, 40, max_jump=5, allow_nonlinear=True)
        assert decision.allow is True

    def test_locality_inert_when_no_max_jump(self):
        state = TrackingState(total_slides=50, current_index=3)
        assert evaluate_trigger(state, 40, max_jump=None, allow_nonlinear=False).allow is True

    def test_operator_override_suppresses_within_cooldown(self):
        state = TrackingState(
            total_slides=10, current_index=3, operator_override=True, override_at=100.0
        )
        decision = evaluate_trigger(state, 4, auto_yield_cooldown_s=5.0, now=102.0)
        assert decision.allow is False
        assert decision.reason == "operator_override"

    def test_operator_override_expires_after_cooldown(self):
        state = TrackingState(
            total_slides=10, current_index=3, operator_override=True, override_at=100.0
        )
        decision = evaluate_trigger(state, 4, auto_yield_cooldown_s=5.0, now=106.0)
        assert decision.allow is True
        assert decision.reason == "ok"

    def test_override_guard_inert_without_timestamp(self):
        state = TrackingState(
            total_slides=10, current_index=3, operator_override=True, override_at=None
        )
        assert evaluate_trigger(state, 4, auto_yield_cooldown_s=5.0, now=1.0).allow is True
