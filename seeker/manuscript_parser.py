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
    block_type: str = "standard"  # "standard", "scripture", or "exposition"
    reference: str = ""


@dataclass
class Manuscript:
    """A structured sermon manuscript composed of ordered slide blocks."""

    title: str = ""
    blocks: list[SlideBlock] = field(default_factory=list)
    version: str = "1.0"

    def to_xml(self, mode: str = "") -> str:
        """Serialize the manuscript to the XML format expected by the system prompt."""
        type_attr = f' type="{mode}"' if mode else ""
        lines = [f"<presentation_manuscript{type_attr}>"]
        
        if self.version == "1.1":
            scripture_blocks = [b for b in self.blocks if b.block_type == "scripture"]
            exposition_blocks = [b for b in self.blocks if b.block_type in ("exposition", "standard")]
            
            if scripture_blocks:
                lines.append("  <scripture_blocks>")
                for block in scripture_blocks:
                    ref_attr = f' reference="{block.reference}"' if block.reference else ""
                    lines.append(f'    <block index="{block.index}"{ref_attr}>')
                    lines.append("      <verbatim_text>")
                    # Indent the content cleanly
                    content_lines = block.content.strip().split('\n')
                    for line in content_lines:
                        lines.append(f"        {escape(line.strip())}")
                    lines.append("      </verbatim_text>")
                    lines.append("    </block>")
                lines.append("  </scripture_blocks>")
                if exposition_blocks:
                    lines.append("")
                    
            if exposition_blocks:
                lines.append("  <exposition_blocks>")
                for block in exposition_blocks:
                    lines.append(f'    <block index="{block.index}">')
                    lines.append("      <expected_content>")
                    content_lines = block.content.strip().split('\n')
                    for line in content_lines:
                        lines.append(f"        {escape(line.strip())}")
                    lines.append("      </expected_content>")
                    lines.append("    </block>")
                lines.append("  </exposition_blocks>")
        else:
            for block in self.blocks:
                label_attr = f' section_label="{block.section_label}"' if block.section_label else ""
                lines.append(f'  <slide_block index="{block.index}"{label_attr}>')
                lines.append("    <expected_content>")
                content_lines = block.content.strip().split('\n')
                for line in content_lines:
                    lines.append(f"      {escape(line.strip())}")
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


def parse_xml(text: str) -> Manuscript:
    """Parse an already XML-formatted manuscript."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return parse_plain_text(text)

    blocks = []
    
    scripture_blocks_el = root.find("scripture_blocks")
    exposition_blocks_el = root.find("exposition_blocks")
    
    if scripture_blocks_el is not None or exposition_blocks_el is not None:
        version = "1.1"
        if scripture_blocks_el is not None:
            for block in scripture_blocks_el.findall("block"):
                index = int(block.get("index", "0"))
                reference = block.get("reference", "")
                v_text_el = block.find("verbatim_text")
                content = v_text_el.text.strip() if v_text_el is not None and v_text_el.text else ""
                blocks.append(SlideBlock(index=index, content=content, block_type="scripture", reference=reference))
                
        if exposition_blocks_el is not None:
            for block in exposition_blocks_el.findall("block"):
                index = int(block.get("index", "0"))
                e_content_el = block.find("expected_content")
                content = e_content_el.text.strip() if e_content_el is not None and e_content_el.text else ""
                blocks.append(SlideBlock(index=index, content=content, block_type="exposition"))
    else:
        version = "1.0"
        for slide in root.findall("slide_block"):
            index = int(slide.get("index", "0"))
            section_label = slide.get("section_label", "")
            expected = slide.find("expected_content")
            content = expected.text.strip() if expected is not None and expected.text else ""
            blocks.append(SlideBlock(index=index, content=content, section_label=section_label, block_type="standard"))
            
    return Manuscript(blocks=blocks, version=version)


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
    if suffix == ".xml":
        return parse_xml(text)

    # Default: plain text
    return parse_plain_text(text)
