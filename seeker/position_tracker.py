"""Deterministic lexical position tracker over the live transcript.

The realtime model is the *brain*; this tracker is the *inner ear*. It consumes
streaming input-transcription text (which every realtime provider emits
continuously, independent of turn segmentation) and maintains a cheap,
debuggable estimate of where the service is in the reference text:

  * In **song mode** the slides are verbatim lyrics and the arrangement gives
    the expected performance order (including chorus repeats), so lexical
    matching is strong evidence. The tracker expands the arrangement into a
    linear *path* of slide indices, scores the transcript tail against nearby
    path entries, and can (a) tell the decision loop that a boundary is
    imminent — the moment to make the model think — and (b) propose the next
    slide directly when the singers are unambiguously into it.
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


@dataclass(frozen=True)
class TrackerProposal:
    """A tracker-initiated slide proposal (song mode only)."""

    index: int
    confidence: float
    reason: str  # "entered_next_block" | "finishing_current_block"


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

    def __post_init__(self) -> None:
        self._block_tokens: dict[int, list[str]] = {
            idx: _tokens(text) for idx, text, _ in self.blocks
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

    def propose(self) -> TrackerProposal | None:
        """Propose the next slide when the transcript is unambiguously inside it.

        Song mode only (requires an arrangement): verbatim lyrics make lexical
        entry into the next block decisive. Returns None when evidence is weak,
        ambiguous, or the path is exhausted.
        """
        if not self.arrangement:
            return None
        nxt = self.next_index
        if nxt is None or self._tokens_since_fire < self.min_evidence_tokens:
            return None

        next_block = self._block_tokens.get(nxt, [])
        opening = next_block[: min(len(next_block), self.tail_tokens)]
        entered = _entry_score(self._tail, opening, self.min_evidence_tokens)
        if entered < self.propose_threshold:
            return None

        # Ambiguity guard: if the *current* block explains the tail almost as
        # well (repeated lines within a section), hold.
        current = self.current_index
        current_score = (
            _best_window_similarity(self._tail, self._block_tokens.get(current, []))
            if current is not None
            else 0.0
        )
        if current_score >= entered - 0.05:
            return None

        return TrackerProposal(index=nxt, confidence=entered, reason="entered_next_block")
