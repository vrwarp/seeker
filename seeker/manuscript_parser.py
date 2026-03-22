"""Phase 3 — Manuscript parser and XML serializer for Gemini prompt injection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape


@dataclass
class SlideBlock:
    """A single slide's expected spoken content."""

    index: int
    content: str
    section_label: str = ""  # "Verse 1", "Chorus", "Bridge", etc.


@dataclass
class Manuscript:
    """A structured sermon manuscript composed of ordered slide blocks."""

    title: str = ""
    blocks: list[SlideBlock] = field(default_factory=list)

    def to_xml(self, mode: str = "") -> str:
        """Serialize the manuscript to the XML format expected by the system prompt."""
        type_attr = f' type="{mode}"' if mode else ""
        lines = [f"<presentation_manuscript{type_attr}>"]
        for block in self.blocks:
            label_attr = f' section_label="{block.section_label}"' if block.section_label else ""
            lines.append(f'  <slide_block index="{block.index}"{label_attr}>')
            lines.append("    <expected_content>")
            lines.append(f"      {escape(block.content.strip())}")
            lines.append("    </expected_content>")
            lines.append("  </slide_block>")
        lines.append("</presentation_manuscript>")
        return "\n".join(lines)

    @classmethod
    def from_slide_infos(cls, slides: list, title: str = "") -> "Manuscript":
        """Build a Manuscript from a list of SlideInfo objects."""
        blocks = [
            SlideBlock(index=s.index, content=s.text, section_label=s.group_name)
            for s in slides
        ]
        return cls(title=title, blocks=blocks)


# ------------------------------------------------------------------
# Parsers for various input formats
# ------------------------------------------------------------------


def parse_plain_text(text: str, delimiter: str = "\n\n") -> Manuscript:
    """Parse a plain-text manuscript split by *delimiter* into slide blocks."""
    sections = [s.strip() for s in text.split(delimiter) if s.strip()]
    blocks = [SlideBlock(index=i, content=s) for i, s in enumerate(sections)]
    return Manuscript(blocks=blocks)


def parse_markdown(text: str) -> Manuscript:
    """Parse a Markdown manuscript, splitting on ``## `` headings or ``---`` rules."""
    # Split on headings or horizontal rules
    parts = re.split(r"(?:^|\n)(?:##\s+.*|---+)\s*\n", text)
    sections = [p.strip() for p in parts if p.strip()]
    blocks = [SlideBlock(index=i, content=s) for i, s in enumerate(sections)]
    return Manuscript(blocks=blocks)


def parse_docx(file_path: str | Path) -> Manuscript:
    """Parse a ``.docx`` manuscript, splitting on empty paragraphs."""
    from docx import Document

    doc = Document(str(file_path))

    sections: list[str] = []
    current: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            sections.append(text)

    blocks = [SlideBlock(index=i, content=s) for i, s in enumerate(sections)]
    return Manuscript(blocks=blocks)


def parse_structured(data: dict) -> Manuscript:
    """Parse a pre-structured dict (from JSON/YAML) into a Manuscript."""
    title = data.get("title", "")
    raw_blocks = data.get("blocks", [])
    blocks = [
        SlideBlock(index=b.get("index", i), content=b["content"])
        for i, b in enumerate(raw_blocks)
    ]
    return Manuscript(title=title, blocks=blocks)


def load_manuscript(path: str | Path) -> Manuscript:
    """Auto-detect format and load a manuscript from *path*."""
    path = Path(path)
    suffix = path.suffix.lower()

    text = ""
    if suffix == ".docx":
        return parse_docx(path)
    else:
        text = path.read_text(encoding="utf-8")

    if suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
        return parse_structured(data)
    if suffix == ".json":
        import json

        data = json.loads(text)
        return parse_structured(data)
    if suffix == ".md":
        return parse_markdown(text)

    # Default: plain text
    return parse_plain_text(text)
