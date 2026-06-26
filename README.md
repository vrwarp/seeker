# Seeker

**Automated sermon slide synchronization** using the [Gemini Multimodal Live API](https://ai.google.dev/gemini-api/docs/multimodal-live) and [ProPresenter 7](https://renewedvision.com/propresenter/).

Seeker listens to a live pastor's audio feed, semantically tracks position within a sermon manuscript, and automatically advances ProPresenter slides — all in under one second of latency.

---

## Architecture

```
┌──────────────┐    PCM Audio     ┌──────────────────┐    WSS / Base64     ┌─────────────────────┐
│  Soundboard  │ ──────────────▶  │  Python Daemon    │ ──────────────────▶ │  Gemini Live API    │
│  (Aux Send)  │                  │  (asyncio)        │ ◀────────────────── │  (native-audio)     │
└──────────────┘                  │                   │    Tool Calls       └─────────────────────┘
                                  │  ┌─────────────┐  │
                                  │  │ Audio Queue  │  │
                                  │  └─────────────┘  │
                                  │                   │
                                  │  ┌─────────────┐  │    HTTP REST
                                  │  │ Orchestrator │──│──────────────────▶ ┌─────────────────────┐
                                  │  └─────────────┘  │                     │  ProPresenter 7     │
                                  └──────────────────┘                     │  (Network API)      │
                                                                           └─────────────────────┘
```

---

## Project Structure

```
seeker/
├── pyproject.toml                    # Package config, deps, CLI entry-point
├── README.md
├── .gitignore
├── config.example.yaml               # Copy → config.yaml to get started
├── prompts/
│   └── v1.0_baseline.txt             # System prompt with 7 behavioral rules
├── plan/
│   ├── 00-overview.md                # Architecture overview & phase index
│   ├── 01-phase-audio-ingestion.md   # Phase 1: Audio capture pipeline
│   ├── 02-phase-gemini-websocket.md  # Phase 2: Gemini Live API integration
│   ├── 03-phase-semantic-tracking.md # Phase 3: Prompt engineering & manuscript parsing
│   ├── 04-phase-propresenter-control.md # Phase 4: ProPresenter REST control
│   └── 05-phase-reliability-orchestration.md # Phase 5: Reliability & operator UX
├── seeker/
│   ├── __init__.py
│   ├── cli.py                        # CLI: start, devices, test-pp, test-audio, version
│   ├── config.py                     # Typed dataclasses + YAML loader w/ env var resolution
│   ├── audio_capture.py              # Phase 1: PyAudio capture → async queue
│   ├── gemini_session.py             # Phase 2: WSS lifecycle, tool-call dispatch, reconnection
│   ├── manuscript_parser.py          # Phase 3: Multi-format parser (txt/md/docx/json/yaml) → XML
│   ├── prompt_builder.py             # Phase 3: Template injection + setup payload builder
│   ├── propresenter_client.py        # Phase 4: REST triggers + ToolHandler adapter
│   └── daemon.py                     # Phase 5: TaskGroup orchestrator + operator HTTP server
└── tests/
    ├── conftest.py                   # Shared fixtures
    ├── test_config.py
    ├── test_manuscript_parser.py
    ├── test_prompt_builder.py
    └── test_propresenter_client.py
```

---

## Quick Start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your API key, audio device, and ProPresenter settings

# 3. List audio devices
seeker devices

# 4. Test ProPresenter connection
seeker test-pp

# 5. Run
seeker start --manuscript sermon.txt
```

---

## Example Commands

```bash
# Start the daemon with an audio track, an xml manuscript following the v1.1 baseline, and the v1.1 baseline prompt
seeker --verbose start --audio-file example.mp3 --manuscript sermon.xml --prompt prompts/v1.1_baseline.txt --play-audio --speed 1.0
```

---

## Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.9+ | Mature async ecosystem, strong audio/ML library support |
| Async Runtime | `asyncio` | Native coroutine support for concurrent I/O tasks |
| Audio Capture | `PyAudio` | Cross-platform hardware audio capture |
| WebSocket Client | `websockets` | Lightweight, asyncio-native WSS client |
| HTTP Client | `aiohttp` | Non-blocking HTTP for ProPresenter REST calls |
| Configuration | YAML / `.env` | Operator-friendly configuration files |
| Operator Control | Local HTTP server (`aiohttp`) | Kill-switch and status endpoints for Stream Deck / Companion |

---

## CLI Commands

```
Usage: seeker [OPTIONS] COMMAND [ARGS]

Commands:
  start        Start the daemon with a manuscript
  devices      List available audio input devices
  test-audio   Capture and playback a short audio clip
  test-pp      Test ProPresenter connectivity
  version      Show version information

Options:
  --config PATH    Path to config.yaml (default: ./config.yaml)
  --verbose        Enable debug logging
```

---

## Operator HTTP API

When the daemon is running, a local HTTP server is exposed for operator controls:

| Endpoint | Method | Action |
|----------|--------|--------|
| `/api/status` | `GET` | Returns daemon state, current slide, latency stats |
| `/api/activate` | `POST` | Activates streaming (exits dormancy) |
| `/api/deactivate` | `POST` | Gracefully stops streaming, returns to dormancy |
| `/api/kill` | `POST` | **Emergency kill-switch** — immediately severs Gemini connection |
| `/api/health` | `GET` | Liveness probe |

These endpoints can be mapped to an **Elgato Stream Deck** via **Bitfocus Companion** for hardware operator controls.

---

## Prerequisites

- Python 3.9+
- Google Cloud project with Gemini API access (`gemini-2.5-flash-native-audio`)
- ProPresenter 7.9+ with Network API enabled
- `portaudio` system library (`brew install portaudio` on macOS)
- Audio interface providing a dedicated aux/matrix send from the soundboard
- (Optional) Elgato Stream Deck + Bitfocus Companion for hardware operator controls

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check seeker/ tests/
```

---

## Design Documents

See the [plan/](plan/) directory for the full technical design:

| Phase | Document | Description |
|-------|----------|-------------|
| 0 | [Overview](plan/00-overview.md) | Architecture, tech stack, phase index |
| 1 | [Audio Ingestion](plan/01-phase-audio-ingestion.md) | Hardware audio capture, resampling, queue backpressure |
| 2 | [Gemini WebSocket](plan/02-phase-gemini-websocket.md) | WSS lifecycle, session setup, context compression |
| 3 | [Semantic Tracking](plan/03-phase-semantic-tracking.md) | Manuscript parsing, prompt engineering, tool schema |
| 4 | [ProPresenter Control](plan/04-phase-propresenter-control.md) | REST triggers, state tracking, drift detection |
| 5 | [Reliability & Orchestration](plan/05-phase-reliability-orchestration.md) | Concurrency, fault tolerance, operator UX, CLI |

---

## Research

- [State of the Art for a Live-Audio Slide-Driving Agent](docs/research/state-of-the-art-slide-agent.md) — survey of alternative real-time/audio models (OpenAI gpt-realtime, Amazon Nova Sonic, open/self-hostable options), semantic-alignment techniques, voice-agent frameworks, prior art, and presentation-control/drift strategies, with prioritized next experiments for Seeker.

---

## License

MIT
