"""Deterministic lexical position tracker over the live transcript.

The realtime model is the *brain*; this tracker is the *inner ear*. It consumes
streaming input-transcription text (which every realtime provider emits
continuously, independent of turn segmentation) and maintains a cheap,
debuggable estimate of where the service is in the reference text:

  * In **song mode** the slides are verbatim lyrics and the arrangement gives
    the *expected* performance order — a prior, not a guarantee. Leaders
    repeat choruses and bridges on a whim ("one more time!") and written
    arrangements are sometimes wrong, so the tracker scores the transcript
    tail against a full candidate set (arrangement-next, repeat of the
    current section, every other section start) with structural priors. It
    can (a) tell the decision loop a boundary is imminent — the moment to
    make the model think, (b) propose a slide directly when one candidate
    wins by a clear margin, and (c) surface its scored hypotheses so
    ambiguous cases escalate to the model *with* the shortlist.
  * In **sermon mode** (no arrangement) content is paraphrased, so lexical
    evidence is weak; the tracker is used only for tick pacing, never for
    autonomous fires.

Everything is pure Python (difflib, no deps) and pure state (no I/O), so it is
unit-testable and provider-agnostic — the same tracker serves the Gemini path,
the OpenAI conductor loop, and a future full-duplex brain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, punctuation stripped."""
    return _WORD_RE.findall(text.lower())


def _similarity(a: list[str], b: list[str]) -> float:
    """Token-sequence similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()


def _best_window_similarity(tail: list[str], block: list[str]) -> float:
    """Best similarity between *tail* and any same-length window of *block*.

    Lyric slides hold several lines; the transcript tail should be compared to
    the best-aligned span, not the whole slide.
    """
    if not tail or not block:
        return 0.0
    width = min(len(tail), len(block))
    probe = tail[-width:]
    if len(block) <= width:
        return _similarity(probe, block)
    best = 0.0
    for start in range(len(block) - width + 1):
        score = _similarity(probe, block[start : start + width])
        if score > best:
            best = score
    return best


def _entry_score(tail: list[str], opening: list[str], min_k: int) -> float:
    """How strongly the tail's *suffix* matches the block's *prefix*.

    "The last k words heard are the first k words of the next block" is the
    signature of having crossed into it. Trying every k avoids diluting the
    score with stale tokens from the previous block still sitting in the tail.
    """
    best = 0.0
    k_max = min(len(tail), len(opening))
    for k in range(min(min_k, k_max), k_max + 1):
        score = _similarity(tail[-k:], opening[:k])
        if score > best:
            best = score
    return best


# Spoken cues that reliably precede an ad-lib repeat of the current section.
# Kept deliberately narrow — a false positive only costs one decision tick.
_REPEAT_CUE_RE = re.compile(
    r"\b(one more time|sing (?:that|it) again|let's sing (?:that|it) again|"
    r"do it again|one more|last time)\b",
    re.IGNORECASE,
)


def detect_repeat_cue(text: str) -> str | None:
    """Return the matched leader cue ("one more time", …) if *text* contains one."""
    m = _REPEAT_CUE_RE.search(text)
    return m.group(0).lower() if m else None


@dataclass(frozen=True)
class Hypothesis:
    """One scored explanation of what the singers are currently singing."""

    index: int
    label: str
    reason: str  # "current" | "arrangement_next" | "repeat_section" | "section_jump"
    evidence: float  # raw lexical evidence in [0, 1]
    prior: float  # structural plausibility weight
    score: float  # evidence * prior


@dataclass(frozen=True)
class TrackerProposal:
    """A tracker-initiated slide proposal (song mode only)."""

    index: int
    confidence: float  # raw lexical evidence for the proposed slide
    reason: str  # hypothesis reason that won
    margin: float = 1.0  # score gap over the best rival hypothesis
    label: str = ""


@dataclass
class PositionTracker:
    """Tracks position in the reference text from streaming transcript tokens."""

    # (block_index, block_text, section_label) for every slide, in deck order.
    blocks: list[tuple[int, str, str]] = field(default_factory=list)
    # Section labels in performance order (song mode); None/empty = deck order.
    arrangement: list[str] | None = None
    # How many trailing transcript tokens to match against.
    tail_tokens: int = 16
    # Similarity needed before the tracker will *propose* a slide itself.
    propose_threshold: float = 0.82
    # Similarity that marks "the current block's ending has been heard".
    boundary_threshold: float = 0.72
    # Minimum transcript tokens heard inside a block before proposing the next.
    min_evidence_tokens: int = 4

    # Structural priors: how plausible each transition is before hearing a
    # word. The arrangement's next slide is the plan; repeating the section
    # just sung is the most common ad-lib; any other section start covers a
    # wrong arrangement or a called jump ("go to the bridge").
    PRIOR_CONTINUE = 1.0
    PRIOR_REPEAT = 0.92
    PRIOR_JUMP = 0.80

    def __post_init__(self) -> None:
        self._block_tokens: dict[int, list[str]] = {
            idx: _tokens(text) for idx, text, _ in self.blocks
        }
        self._labels: dict[int, str] = {idx: label for idx, _, label in self.blocks}
        # Consecutive blocks sharing a label form a section; each section's
        # first slide is a candidate jump/repeat target.
        self._sections: list[tuple[str, list[int]]] = []
        for idx, _, label in self.blocks:
            if self._sections and self._sections[-1][0] == label:
                self._sections[-1][1].append(idx)
            else:
                self._sections.append((label, [idx]))
        self._section_of: dict[int, int] = {
            idx: s for s, (_, indices) in enumerate(self._sections) for idx in indices
        }
        self._path: list[int] = self._expand_path()
        self._path_pos: int = 0
        self._tail: list[str] = []
        self._tokens_since_fire: int = 0

    # ------------------------------------------------------------------
    # Path construction
    # ------------------------------------------------------------------

    def _expand_path(self) -> list[int]:
        """Expand the arrangement into a linear sequence of slide indices.

        Arrangement entries name sections ("Chorus 1"); each section owns one
        or more consecutive slides. Unknown entries (e.g. "Intro",
        "Instrumental") contribute no slides — they are timing gaps, not
        targets. Without an arrangement the path is simply deck order.
        """
        if not self.arrangement:
            return [idx for idx, _, _ in self.blocks]

        by_label: dict[str, list[int]] = {}
        for idx, _, label in self.blocks:
            by_label.setdefault(self._norm_label(label), []).append(idx)

        path: list[int] = []
        for entry in self.arrangement:
            # Tolerate numbered-list formatting ("3. Chorus 1").
            entry = re.sub(r"^\s*\d+[.)]\s*", "", entry).strip()
            indices = by_label.get(self._norm_label(entry))
            if indices:
                path.extend(indices)
        return path or [idx for idx, _, _ in self.blocks]

    @staticmethod
    def _norm_label(label: str) -> str:
        return " ".join(_tokens(label))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def path(self) -> list[int]:
        return list(self._path)

    @property
    def current_index(self) -> int | None:
        if not self._path:
            return None
        return self._path[min(self._path_pos, len(self._path) - 1)]

    @property
    def next_index(self) -> int | None:
        pos = self._path_pos + 1
        return self._path[pos] if pos < len(self._path) else None

    # ------------------------------------------------------------------
    # Evidence ingestion
    # ------------------------------------------------------------------

    def feed(self, transcript_text: str) -> None:
        """Consume a streaming transcript fragment."""
        new = _tokens(transcript_text)
        if not new:
            return
        self._tail.extend(new)
        self._tokens_since_fire += len(new)
        if len(self._tail) > self.tail_tokens:
            self._tail = self._tail[-self.tail_tokens :]

    def anchor(self, slide_index: int) -> None:
        """Re-anchor after ANY confirmed slide change (model, tracker, operator).

        Prefers the nearest occurrence of *slide_index* at/after the current
        path position (so a repeated chorus resolves to the upcoming repeat,
        not the one already sung), falling back to the nearest anywhere.
        """
        ahead = [i for i in range(self._path_pos, len(self._path)) if self._path[i] == slide_index]
        if ahead:
            self._path_pos = ahead[0]
        else:
            anywhere = [i for i, idx in enumerate(self._path) if idx == slide_index]
            if anywhere:
                self._path_pos = min(anywhere, key=lambda i: abs(i - self._path_pos))
        self._tokens_since_fire = 0

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def near_boundary(self) -> bool:
        """True when the tail of the *current* block has been heard.

        This is the "make the model think NOW" signal: the singers are
        finishing the on-screen slide, so the next slide decision is imminent.
        """
        current = self.current_index
        if current is None:
            return False
        block = self._block_tokens.get(current, [])
        if not block:
            return False
        ending = block[-min(len(block), self.tail_tokens // 2) :]
        return _best_window_similarity(self._tail, ending) >= self.boundary_threshold

    def label_for(self, index: int) -> str:
        """Section label of a slide ("" when unknown)."""
        return self._labels.get(index, "")

    def hypotheses(self) -> list[Hypothesis]:
        """Score every plausible explanation of the current transcript tail.

        The arrangement is treated as a *prior*, not a path: worship leaders
        repeat choruses and bridges on a whim, and the written order is
        sometimes simply wrong. Candidates, by structural plausibility:

          * current       — still inside the on-screen slide (never proposed;
                            exists so rivals must beat it)
          * arrangement_next — the planned next slide
          * repeat_section — first slide of the section just sung
                             ("one more time")
          * section_jump  — first slide of any other section (wrong
                            arrangement, called jumps, tag endings)

        Sorted by score (evidence × prior), descending.
        """
        current = self.current_index
        candidates: dict[int, tuple[str, float]] = {}

        nxt = self.next_index
        if nxt is not None and nxt != current:
            candidates[nxt] = ("arrangement_next", self.PRIOR_CONTINUE)

        if current is not None:
            sec = self._section_of.get(current)
            if sec is not None:
                repeat_start = self._sections[sec][1][0]
                if repeat_start != current and repeat_start not in candidates:
                    candidates[repeat_start] = ("repeat_section", self.PRIOR_REPEAT)

        for _, indices in self._sections:
            start = indices[0]
            if start != current and start not in candidates:
                candidates[start] = ("section_jump", self.PRIOR_JUMP)

        scored: list[Hypothesis] = []
        for index, (reason, prior) in candidates.items():
            block = self._block_tokens.get(index, [])
            opening = block[: min(len(block), self.tail_tokens)]
            evidence = _entry_score(self._tail, opening, self.min_evidence_tokens)
            scored.append(
                Hypothesis(
                    index=index,
                    label=self.label_for(index),
                    reason=reason,
                    evidence=evidence,
                    prior=prior,
                    score=evidence * prior,
                )
            )

        # The incumbent: how well the on-screen slide still explains the tail.
        # Uses window similarity (we may be mid-block, not at its opening).
        if current is not None:
            evidence = _best_window_similarity(self._tail, self._block_tokens.get(current, []))
            scored.append(
                Hypothesis(
                    index=current,
                    label=self.label_for(current),
                    reason="current",
                    evidence=evidence,
                    prior=self.PRIOR_CONTINUE,
                    score=evidence * self.PRIOR_CONTINUE,
                )
            )

        scored.sort(key=lambda h: h.score, reverse=True)
        return scored

    def propose(self) -> TrackerProposal | None:
        """Propose a slide when the transcript unambiguously entered one.

        Song mode only (requires an arrangement): verbatim lyrics make lexical
        entry decisive. The proposal carries its *margin* over the best rival
        hypothesis (including "we're still on the current slide") so the
        caller can demand a clear win before firing autonomously — near-ties
        (identical chorus repeats, twin sections) are the model's call, made
        by ear.
        """
        if not self.arrangement:
            return None
        if self._tokens_since_fire < self.min_evidence_tokens:
            return None

        hyps = self.hypotheses()
        movers = [h for h in hyps if h.reason != "current"]
        if not movers:
            return None
        top = movers[0]
        if top.evidence < self.propose_threshold:
            return None

        rivals = [h for h in hyps if h.index != top.index]
        margin = top.score - rivals[0].score if rivals else top.score
        if margin <= 0:
            return None  # the on-screen slide (or a twin) explains it as well

        return TrackerProposal(
            index=top.index,
            confidence=top.evidence,
            reason=top.reason,
            margin=margin,
            label=top.label,
        )
