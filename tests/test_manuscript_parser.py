"""Tests for the manuscript parser and XML serializer."""

from __future__ import annotations

from seeker.manuscript_parser import Manuscript, SlideBlock, parse_markdown, parse_plain_text


class TestParsePlainText:
    def test_splits_on_double_newline(self, sample_manuscript_text):
        manuscript = parse_plain_text(sample_manuscript_text)
        assert len(manuscript.blocks) == 3
        assert manuscript.blocks[0].index == 0
        assert "Welcome" in manuscript.blocks[0].content
        assert manuscript.blocks[2].index == 2

    def test_custom_delimiter(self):
        text = "Block one---Block two---Block three"
        manuscript = parse_plain_text(text, delimiter="---")
        assert len(manuscript.blocks) == 3

    def test_empty_text(self):
        manuscript = parse_plain_text("")
        assert len(manuscript.blocks) == 0


class TestParseMarkdown:
    def test_splits_on_headings(self):
        md = "## Intro\nWelcome everyone.\n\n## Point 1\nFirst point.\n\n## Point 2\nSecond point."
        manuscript = parse_markdown(md)
        assert len(manuscript.blocks) >= 3

    def test_splits_on_horizontal_rule(self):
        md = "Block A\n\n---\n\nBlock B\n\n---\n\nBlock C"
        manuscript = parse_markdown(md)
        assert len(manuscript.blocks) == 3


class TestManuscriptXml:
    def test_xml_structure(self):
        m = Manuscript(
            blocks=[
                SlideBlock(index=0, content="Hello world"),
                SlideBlock(index=1, content="Second slide"),
            ]
        )
        xml = m.to_xml()
        assert "<presentation_manuscript>" in xml
        assert 'slide_block index="0"' in xml
        assert 'slide_block index="1"' in xml
        assert "<expected_content>" in xml
        assert "Hello world" in xml

    def test_xml_escapes_special_chars(self):
        m = Manuscript(blocks=[SlideBlock(index=0, content='Test <tag> & "quote"')])
        xml = m.to_xml()
        assert "&lt;tag&gt;" in xml
        assert "&amp;" in xml


class TestSlideBlockSectionLabel:
    def test_default_empty(self):
        block = SlideBlock(index=0, content="Hello")
        assert block.section_label == ""

    def test_set_value(self):
        block = SlideBlock(index=0, content="Hello", section_label="Chorus")
        assert block.section_label == "Chorus"


class TestManuscriptXmlSectionLabel:
    def test_xml_with_section_labels(self):
        m = Manuscript(
            blocks=[
                SlideBlock(index=0, content="Verse lyrics", section_label="Verse 1"),
                SlideBlock(index=1, content="Chorus lyrics", section_label="Chorus"),
            ]
        )
        xml = m.to_xml(mode="song")
        assert 'type="song"' in xml
        assert 'section_label="Verse 1"' in xml
        assert 'section_label="Chorus"' in xml

    def test_xml_omits_empty_section_label(self):
        m = Manuscript(blocks=[SlideBlock(index=0, content="Hello")])
        xml = m.to_xml()
        assert "section_label" not in xml

    def test_xml_omits_type_when_no_mode(self):
        m = Manuscript(blocks=[SlideBlock(index=0, content="Hello")])
        xml = m.to_xml()
        assert "type=" not in xml


class TestFromSlideInfos:
    def test_builds_from_slide_infos(self):
        from dataclasses import dataclass

        @dataclass
        class FakeSlide:
            index: int
            text: str
            group_name: str

        slides = [
            FakeSlide(index=0, text="Verse lyrics", group_name="Verse 1"),
            FakeSlide(index=1, text="Chorus lyrics", group_name="Chorus"),
        ]
        m = Manuscript.from_slide_infos(slides, title="Test Song")
        assert m.title == "Test Song"
        assert len(m.blocks) == 2
        assert m.blocks[0].content == "Verse lyrics"
        assert m.blocks[0].section_label == "Verse 1"
        assert m.blocks[1].section_label == "Chorus"

    def test_empty_slides(self):
        m = Manuscript.from_slide_infos([])
        assert m.title == ""
        assert len(m.blocks) == 0
