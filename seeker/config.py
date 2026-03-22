"""Configuration loader and dataclasses for all Seeker subsystems."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AudioConfig:
    """Hardware audio capture configuration."""

    device_name: str = "default"
    device_index: int | None = None
    sample_rate: int = 16_000
    channels: int = 1
    chunk_duration_ms: int = 32
    queue_max_size: int = 100
    input_gain_db: float = 0.0
    audio_file: str | None = None

    @property
    def chunk_frames(self) -> int:
        """Number of PCM frames per chunk."""
        return int(self.sample_rate * (self.chunk_duration_ms / 1000))

    @property
    def chunk_bytes(self) -> int:
        """Byte size of each chunk (16-bit mono)."""
        return self.chunk_frames * 2 * self.channels


@dataclass
class GeminiConfig:
    """Gemini Multimodal Live API configuration."""

    api_key: str = ""
    model: str = "models/gemini-2.5-flash-native-audio-latest"
    endpoint: str = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )
    target_tokens: int = 100_000
    reconnect_max_backoff_s: float = 8.0


@dataclass
class ProPresenterConfig:
    """ProPresenter 7 network API configuration."""

    host: str = "127.0.0.1"
    port: int = 50001
    protocol: str = "http"
    timeout_s: float = 2.0
    use_sequential_trigger: bool = True
    health_check_interval_s: float = 30.0
    ws_password: str = ""
    sermon_uuid: str = ""

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"


@dataclass
class OperatorConfig:
    """Operator HTTP control server configuration."""

    http_port: int = 8080
    http_host: str = "127.0.0.1"


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    file: str = "seeker.log"
    console: bool = True
    latency_tracking: bool = True


@dataclass
class PromptConfig:
    """Prompt template and manuscript configuration."""

    template: str = "prompts/active.txt"
    song_template: str = "prompts/v1.1_worship.txt"
    manuscript: str = ""
    mode: str = "sermon"
    anticipation_seconds: float = 1.0
    arrangement_pdf: str = ""


@dataclass
class SeekerConfig:
    """Top-level configuration aggregating all subsystem configs."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    propresenter: ProPresenterConfig = field(default_factory=ProPresenterConfig)
    operator: OperatorConfig = field(default_factory=OperatorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)


def _resolve_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} references with their environment values."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        return os.environ.get(env_key, "")
    return value


def _apply_dict(target: object, data: dict) -> None:
    """Apply a dict of values onto a dataclass instance, resolving env vars."""
    for key, value in data.items():
        if hasattr(target, key):
            if isinstance(value, str):
                value = _resolve_env_vars(value)
            setattr(target, key, value)


def load_config(path: str | Path) -> SeekerConfig:
    """Load configuration from a YAML file.

    Environment variables in the form ``${VAR_NAME}`` are resolved automatically.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    config = SeekerConfig()

    section_map = {
        "audio": config.audio,
        "gemini": config.gemini,
        "propresenter": config.propresenter,
        "operator": config.operator,
        "logging": config.logging,
        "prompt": config.prompt,
    }

    for section_name, section_obj in section_map.items():
        if section_name in raw and isinstance(raw[section_name], dict):
            _apply_dict(section_obj, raw[section_name])

    return config
