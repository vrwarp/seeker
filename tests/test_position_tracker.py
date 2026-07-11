"""Tests for the deterministic lexical position tracker."""

from __future__ import annotations

from seeker.position_tracker import PositionTracker, detect_repeat_cue

# A miniature worship song: two verses and a chorus that repeats.
BLOCKS = [
    (0, "Amazing grace how sweet the sound\nthat saved a wretch like me", "Verse 1"),
    (1, "I once was lost but now am found\nwas blind but now I see", "Verse 1"),
    (2, "My chains are gone I've been set free\nmy God my Savior has ransomed me", "Chorus"),
    (3, "And like a flood His mercy reigns\nunending love amazing grace", "Chorus"),
    (4, "The Lord has promised good to me\nHis word my hope secures", "Verse 2"),
    (5, "He will my shield and portion be\nas long as life endures", "Verse 2"),
]

ARRANGEMENT = ["Verse 1", "Chorus", "Verse 2", "Chorus"]


def make_tracker(**kwargs) -> PositionTracker:
    return PositionTracker(blocks=BLOCKS, arrangement=ARRANGEMENT, **kwargs)


class TestPathExpansion:
    def test_arrangement_expands_to_slide_path(self):
        tracker = make_tracker()
        assert tracker.path == [0, 1, 2, 3, 4, 5, 2, 3]

    def test_numbered_arrangement_entries_are_tolerated(self):
        tracker = PositionTracker(
            blocks=BLOCKS,
            arrangement=["1. Verse 1", "2) Chorus", "3. Verse 2", "4. Chorus"],
        )
        assert tracker.path == [0, 1, 2, 3, 4, 5, 2, 3]

    def test_unknown_sections_contribute_no_slides(self):
        tracker = PositionTracker(
            blocks=BLOCKS,
            arrangement=["Intro", "Verse 1", "Instrumental", "Chorus"],
        )
        assert tracker.path == [0, 1, 2, 3]

    def test_no_arrangement_falls_back_to_deck_order(self):
        tracker = PositionTracker(blocks=BLOCKS, arrangement=None)
        assert tracker.path == [0, 1, 2, 3, 4, 5]


class TestProposal:
    def test_proposes_next_slide_when_lyrics_enter_it(self):
        tracker = make_tracker()
        tracker.feed("that saved a wretch like me")  # ending of slide 0
        tracker.feed("I once was lost but now am found")  # opening of slide 1

        proposal = tracker.propose()
        assert proposal is not None
        assert proposal.index == 1
        assert proposal.confidence >= tracker.propose_threshold

    def test_no_proposal_without_evidence(self):
        tracker = make_tracker()
        tracker.feed("something entirely different is being sung here")
        assert tracker.propose() is None

    def test_no_proposal_in_sermon_mode(self):
        tracker = PositionTracker(blocks=BLOCKS, arrangement=None)
        tracker.feed("I once was lost but now am found")
        assert tracker.propose() is None

    def test_chorus_return_is_proposed_after_verse_two(self):
        tracker = make_tracker()
        # Walk the tracker to the end of Verse 2 (path position 5 → slide 5).
        for idx in (1, 2, 3, 4, 5):
            tracker.anchor(idx)
        tracker.feed("as long as life endures")
        tracker.feed("my chains are gone I've been set free")

        proposal = tracker.propose()
        assert proposal is not None
        assert proposal.index == 2  # back to the first chorus slide

    def test_min_evidence_gate_after_anchor(self):
        tracker = make_tracker(min_evidence_tokens=6)
        tracker.feed("I once was lost but now am found")
        tracker.anchor(1)  # fire consumed the evidence
        tracker.feed("my chains")  # only two tokens since the fire
        assert tracker.propose() is None


class TestAnchor:
    def test_anchor_prefers_upcoming_occurrence_of_repeated_slide(self):
        tracker = make_tracker()
        # Move into Verse 2 (path position 4).
        for idx in (1, 2, 3, 4):
            tracker.anchor(idx)
        # Chorus slide 2 appears at path positions 2 and 6; from position 4
        # the upcoming occurrence (6) must win.
        tracker.anchor(2)
        assert tracker.current_index == 2
        assert tracker.next_index == 3
        tracker.anchor(3)
        assert tracker.next_index is None  # end of the arrangement

    def test_anchor_falls_back_to_nearest_occurrence_behind(self):
        tracker = make_tracker()
        for idx in (1, 2, 3, 4, 5, 2, 3):
            tracker.anchor(idx)
        # Operator jumps back to Verse 2 — only occurrence is behind us.
        tracker.anchor(4)
        assert tracker.current_index == 4
        assert tracker.next_index == 5


class TestAdLibRepeats:
    """The arrangement is a prior, not a path: leaders repeat sections at will."""

    def test_unplanned_chorus_repeat_is_proposed_after_arrangement_ends(self):
        # Arrangement ends on the chorus, but the leader sings it once more.
        tracker = PositionTracker(blocks=BLOCKS, arrangement=["Verse 1", "Chorus"])
        for idx in (1, 2, 3):
            tracker.anchor(idx)  # now on the final chorus slide; path exhausted
        assert tracker.next_index is None

        tracker.feed("unending love amazing grace")  # chorus ending
        tracker.feed("my chains are gone I've been set free")  # chorus restarts

        proposal = tracker.propose()
        assert proposal is not None
        assert proposal.index == 2  # first slide of the current section, again
        assert proposal.reason == "repeat_section"

    def test_tag_ending_supports_multiple_repeats(self):
        tracker = PositionTracker(blocks=BLOCKS, arrangement=["Verse 1", "Chorus"])
        for idx in (1, 2, 3):
            tracker.anchor(idx)
        # First ad-lib repeat.
        tracker.feed("my chains are gone I've been set free my God my Savior")
        first = tracker.propose()
        assert first is not None and first.index == 2
        tracker.anchor(2)
        # Continue within the repeated chorus…
        tracker.feed("and like a flood His mercy reigns")
        again = tracker.propose()
        assert again is not None and again.index == 3
        tracker.anchor(3)
        # …and the leader calls it one more time.
        tracker.feed("my chains are gone I've been set free my God my Savior")
        second = tracker.propose()
        assert second is not None and second.index == 2

    def test_wrong_arrangement_yields_to_what_is_sung(self):
        # Deck has a Bridge the arrangement forgot; leader sings it after the
        # chorus instead of the planned Verse 2.
        blocks = BLOCKS + [
            (6, "You break every chain and set the captive free", "Bridge"),
        ]
        tracker = PositionTracker(
            blocks=blocks, arrangement=["Verse 1", "Chorus", "Verse 2"]
        )
        for idx in (1, 2, 3):
            tracker.anchor(idx)
        assert tracker.next_index == 4  # the (wrong) plan says Verse 2

        tracker.feed("you break every chain and set the captive free")

        proposal = tracker.propose()
        assert proposal is not None
        assert proposal.index == 6
        assert proposal.reason == "section_jump"

    def test_identical_twin_sections_produce_no_autonomous_proposal(self):
        # Two distinct deck sections with identical words: lexical evidence
        # cannot pick one, so the tracker must defer (the model decides by ear).
        blocks = [
            (0, "Verse words that are unique here", "Verse 1"),
            (1, "Sing it out sing it loud forever", "Chorus 1"),
            (2, "Sing it out sing it loud forever", "Chorus 2"),
        ]
        tracker = PositionTracker(
            blocks=blocks, arrangement=["Verse 1", "Chorus 1", "Chorus 2"]
        )
        # From the bridge-less verse, both choruses are plausible next stops:
        # Chorus 1 is arrangement-next, Chorus 2 a jump — the prior separates
        # them, so the arrangement-consistent twin wins with a usable margin.
        tracker.feed("sing it out sing it loud forever")
        proposal = tracker.propose()
        assert proposal is not None and proposal.index == 1

        # But once priors tie (both are jumps from Chorus 2's end), the twins
        # are indistinguishable and the tracker holds.
        tracker.anchor(1)
        tracker.anchor(2)  # now on Chorus 2, arrangement exhausted
        tracker.feed("sing it out sing it loud forever")
        assert tracker.propose() is None
        movers = [h for h in tracker.hypotheses() if h.reason != "current"]
        assert movers and movers[0].evidence >= tracker.propose_threshold

    def test_hypotheses_expose_scored_candidates(self):
        tracker = make_tracker()
        tracker.feed("my chains are gone I've been set free")
        hyps = tracker.hypotheses()
        assert hyps[0].index == 2
        assert hyps[0].evidence > 0.9
        reasons = {h.reason for h in hyps}
        assert "current" in reasons and "section_jump" in reasons

    def test_label_for(self):
        tracker = make_tracker()
        assert tracker.label_for(2) == "Chorus"
        assert tracker.label_for(99) == ""


class TestRepeatCue:
    def test_detects_leader_cues(self):
        assert detect_repeat_cue("ONE more time, church!") == "one more time"
        assert detect_repeat_cue("let's sing it again") == "let's sing it again"
        assert detect_repeat_cue("this is the last time") == "last time"

    def test_ignores_ordinary_lyrics(self):
        assert detect_repeat_cue("I am born again in Your love") is None
        assert detect_repeat_cue("my chains are gone") is None


class TestBoundary:
    def test_near_boundary_when_current_block_ending_heard(self):
        tracker = make_tracker()
        tracker.feed("that saved a wretch like me")
        assert tracker.near_boundary() is True

    def test_not_near_boundary_on_unrelated_text(self):
        tracker = make_tracker()
        tracker.feed("the pastor is welcoming everyone to the service")
        assert tracker.near_boundary() is False

    def test_transcription_noise_is_tolerated(self):
        tracker = make_tracker()
        # ASR heard "wretch" as "rich" and dropped "that".
        tracker.feed("saved a rich like me")
        assert tracker.near_boundary() is True
