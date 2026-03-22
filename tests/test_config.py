"""Tests for the configuration loader."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from seeker.config import AudioConfig, PromptConfig, load_config


class TestAudioConfig:
    def test_chunk_frames(self):
        config = AudioConfig(sample_rate=16000, chunk_duration_ms=32)
        assert config.chunk_frames == 512

    def test_chunk_bytes_mono(self):
        config = AudioConfig(sample_rate=16000, chunk_duration_ms=32, channels=1)
        assert config.chunk_bytes == 1024

    def test_defaults(self):
        config = AudioConfig()
        assert config.sample_rate == 16000
        assert config.channels == 1
        assert config.chunk_duration_ms == 32


class TestLoadConfig:
    def test_load_valid_yaml(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                audio:
                  device_name: "Test Device"
                  sample_rate: 44100
                gemini:
                  api_key: "my-key"
                propresenter:
                  port: 9999
            """)
        )
        config = load_config(config_file)
        assert config.audio.device_name == "Test Device"
        assert config.audio.sample_rate == 44100
        assert config.gemini.api_key == "my-key"
        assert config.propresenter.port == 9999

    def test_env_var_resolution(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TEST_SEEKER_KEY", "resolved-key")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                gemini:
                  api_key: "${TEST_SEEKER_KEY}"
            """)
        )
        config = load_config(config_file)
        assert config.gemini.api_key == "resolved-key"

    def test_missing_file_raises(self):
        import pytest
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_empty_yaml(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        config = load_config(config_file)
        # Should return defaults
        assert config.audio.sample_rate == 16000


class TestPromptConfig:
    def test_defaults(self):
        config = PromptConfig()
        assert config.mode == "sermon"
        assert config.song_template == "prompts/v1.1_worship.txt"
        assert config.anticipation_seconds == 1.0

    def test_load_song_mode_yaml(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                prompt:
                  mode: "song"
                  song_template: "prompts/custom.txt"
                  anticipation_seconds: 1.5
            """)
        )
        config = load_config(config_file)
        assert config.prompt.mode == "song"
        assert config.prompt.song_template == "prompts/custom.txt"
        assert config.prompt.anticipation_seconds == 1.5
