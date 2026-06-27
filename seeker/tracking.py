"""Slide-tracking state and trigger-safety policy.

This module holds the small amount of shared state needed to turn Seeker's
"fire and forget" slide triggering into a self-correcting closed loop:

  * ``TrackingState`` is the single source of truth for what slide is *actually*
    on screen (observed from ProPresenter), what the agent last *commanded*, and
    whether a human operator has taken manual control.
  * ``evaluate_trigger`` is a pure function that decides whether a model-proposed
    slide trigger should be allowed. It makes the worst failure modes — a wild
    out-of-range index, a redundant re-fire, or fighting the operator — structurally
    impossible, independent of which model is driving.

Keeping the decision logic pure (no I/O) makes it cheap to unit-test and reuse
from both the tool handler (egress) and the reconcile loop (ingress).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrackingState:
    """Shared, mutable view of where the presentation actually is.

    Updated from two sides: the tool handler records what the agent *commanded*,
    and the reconcile loop records what is *observed* on screen and flags operator
    overrides.
    """

    total_slides: int = 0
    current_index: int = 0          # last OBSERVED actual on-screen slide
    commanded_index: int = -1       # last index the agent asked ProPresenter to show
    commanded_at: float | None = None
    operator_override: bool = False  # a human moved the slide; auto-advance is yielding
    override_at: float | None = None


@dataclass(frozen=True)
class TriggerDecision:
    """Outcome of evaluating a proposed slide trigger."""

    allow: bool
    index: int
    reason: str  # ok | out_of_range | noop_already_current | jump_too_large | operator_override


def evaluate_trigger(
    state: TrackingState,
    requested_index: int,
    *,
    max_jump: int | None = None,
    allow_nonlinear: bool = True,
    auto_yield_cooldown_s: float = 0.0,
    now: float = 0.0,
) -> TriggerDecision:
    """Decide whether a model-proposed slide trigger should fire.

    Guards, in priority order:

    1. **Operator override / auto-yield** — if a human recently took manual control,
       suppress agent triggers until the cooldown elapses so the agent does not
       fight the operator.
    2. **Bounds** — never trigger an index outside ``[0, total_slides)``. This is the
       single most important guard: it makes a hallucinated "go to slide 999"
       impossible. (Skipped when ``total_slides`` is unknown / 0.)
    3. **No-op** — ignore a trigger for the slide already on screen, avoiding a
       visible redundant re-fire.
    4. **Locality** — when a maximum jump is configured *and* non-linear jumps are
       not expected for this mode (plain sequential sermon), reject jumps larger
       than ``max_jump``. Song mode and v1.1 scripture tracking pass
       ``allow_nonlinear=True`` so legitimate chorus/scripture jumps are kept.

    ``now`` and ``auto_yield_cooldown_s`` use the same monotonic clock the daemon
    feeds in; both default such that the override guard is inert when unused.
    """
    if (
        state.operator_override
        and state.override_at is not None
        and (now - state.override_at) < auto_yield_cooldown_s
    ):
        return TriggerDecision(False, requested_index, "operator_override")

    if requested_index < 0 or (
        state.total_slides > 0 and requested_index >= state.total_slides
    ):
        return TriggerDecision(False, requested_index, "out_of_range")

    if requested_index == state.current_index:
        return TriggerDecision(False, requested_index, "noop_already_current")

    if (
        max_jump is not None
        and not allow_nonlinear
        and abs(requested_index - state.current_index) > max_jump
    ):
        return TriggerDecision(False, requested_index, "jump_too_large")

    return TriggerDecision(True, requested_index, "ok")
