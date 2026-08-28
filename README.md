# Seeker

**Automated worship & sermon slide synchronization** using the [OpenAI Realtime API](https://developers.openai.com/api/docs/guides/realtime) (pivoting to **gpt-live** full duplex when its API ships) and [ProPresenter 7](https://renewedvision.com/propresenter/). The legacy [Gemini Live](https://ai.google.dev/gemini-api/docs/multimodal-live) path remains available via `provider: gemini`.

Seeker listens to the live board feed, tracks position within a sermon manuscript or a worship song's lyrics + arrangement, and automatically fires ProPresenter slides at the right instant — silently, with a human operator always sovereign.

**Why the OpenAI pivot:** server-VAD turn segmentation (the Gemini interaction model) collapses on continuous worship audio — no silences means no decision points means stalled slides. Seeker now runs the Realtime API with `turn_detection: null` and owns its own decision clock (the *conductor*), with a `turn_mode: full_duplex` seam ready for gpt-live. Full story: [docs/design/gpt-live-pivot.md](docs/design/gpt-live-pivot.md).

---

## Architecture

```
┌──────────────┐  PCM (resampled  ┌────────────────────┐  WSS: append/commit  ┌──────────────────────┐
│  Soundboard  │ ───to 24 kHz)──▶ │  Python Daemon      │ ───response.create──▶│  OpenAI Realtime API │
│  (Aux Send)  │                  │  (asyncio)          │ ◀── tool calls ──────│  gpt-realtime-2.1    │
└──────────────┘                  │                     │ ◀── transcripts ─────│  (→ gpt-live next)   │
                                  │  ┌───────────────┐  │                      └──────────────────────┘
                                  │  │ Conductor      │  │   The daemon owns the decision clock:
                                  │  │ (decision clock)│  │   no VAD anywhere in the trigger path.
                                  │  └───────────────┘  │
                                  │  ┌───────────────┐  │
                                  │  │ PositionTracker│  │   Lexical inner ear: paces ticks,
                                  │  └───────────────┘  │   autofires verbatim lyric matches.
                                  │  ┌───────────────┐  │    HTTP REST
                                  │  │ Trigger guards │──│──────────────────▶ ┌─────────────────────┐
                                  │  └───────────────┘  │                     │  ProPresenter 7     │
                                  └────────────────────┘                     │  (Network API)      │
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
│   ├── v2.0_sermon_openai.txt        # OpenAI conductor regime: tool call or HOLD
│   ├── v2.0_worship_openai.txt       # Song mode: fire-early policy, arrangement-aware
│   ├── v1.0_baseline.txt             # Legacy Gemini prompts
│   ├── v1.1_baseline.txt
│   └── v1.1_worship.txt
├── docs/
│   ├── design/gpt-live-pivot.md      # The pivot design: diagnosis → architecture → gpt-live plan
│   └── research/                     # Research briefings (see Research section)
├── plan/                             # Original phase-by-phase build plan (Gemini era)
├── seeker/
│   ├── __init__.py
│   ├── cli.py                        # CLI: start, devices, test-pp, test-audio, version
│   ├── config.py                     # Typed dataclasses + YAML loader w/ env var resolution
│   ├── audio_capture.py              # PyAudio capture → async queue
│   ├── file_audio.py                 # File-based audio ingestion (ffmpeg) for replay/testing
│   ├── brain.py                      # RealtimeBrain protocol — the daemon is provider-blind
│   ├── openai_session.py             # OpenAI Realtime session: conductor clock, tools, rotation
│   ├── position_tracker.py           # Deterministic lexical tracker (tick pacing + autofire)
│   ├── gemini_session.py             # Legacy Gemini Live session
│   ├── manuscript_parser.py          # Multi-format parser (txt/md/docx/json/yaml/xml) → XML
│   ├── prompt_builder.py             # Template injection + per-provider tool declarations
│   ├── propresenter_client.py        # REST triggers + ToolHandler adapter
│   ├── tracking.py                   # Trigger-safety policy (bounds/no-op/locality/yield)
│   └── daemon.py                     # Orchestrator + operator HTTP server
└── tests/
    ├── conftest.py                   # Shared fixtures
    ├── test_config.py
    ├── test_daemon.py
    ├── test_manuscript_parser.py
    ├── test_openai_session.py
    ├── test_position_tracker.py
    ├── test_prompt_builder.py
    ├── test_propresenter_client.py
    └── test_tracking.py
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
# Sermon replay test: OpenAI conductor brain, xml manuscript, audio from file
seeker --verbose start --audio-file example.mp3 --manuscript sermon.xml --play-audio --speed 1.0

# Live worship set: lyrics pulled from ProPresenter, arrangement from a text file
seeker --verbose start --mode song --arrangement tests/arrangement_all_my_boast.txt

# Legacy Gemini path (unchanged behavior)
seeker --verbose start --provider gemini --manuscript sermon.xml --prompt prompts/v1.1_baseline.txt
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
- OpenAI API key with Realtime API access (`gpt-realtime-2.1`; Tier 3+ recommended for dense decision cadences)
- *(legacy path only)* Google Cloud project with Gemini API access (`gemini-2.5-flash-native-audio`)
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

## Research & Design

- **[Seeker × GPT-Live: The Full-Duplex Pivot](docs/design/gpt-live-pivot.md)** — the current design: why server-VAD segmentation fails worship audio, the conductor architecture on the OpenAI Realtime API, and the gpt-live upgrade plan.
- **[OpenAI Realtime API & GPT-Live briefing](docs/research/openai-realtime-and-gpt-live.md)** (2026-07-11) — GPT-Live launch facts and full-duplex claims; GA Realtime API deep-dive (`turn_detection: null` semantics, transcription, tools, session caps, cost math).
- **[Gemini 3.5 Transcribe briefing](docs/research/gemini-3-5-transcribe.md)** (2026-08-27, launch day) — the new `gemini-3.5-transcribe(-live)` models: why the advertised function calling is a macOS-app feature, not an API one (so the conductor stays); the continuous interim-transcript stream + `custom_vocabulary` as a perception-plane upgrade candidate; and the word-timestamped batch model as the eval-harness gold-timing source.
- [State of the Art for a Live-Audio Slide-Driving Agent](docs/research/state-of-the-art-slide-agent.md) — survey of alternative real-time/audio models (OpenAI gpt-realtime, Amazon Nova Sonic, open/self-hostable options), semantic-alignment techniques, voice-agent frameworks, prior art, and presentation-control/drift strategies, with prioritized next experiments for Seeker.
- [Is Full-Duplex Worth It for Seeker?](docs/research/full-duplex-suitability.md) — (2026-06-26, **verdict superseded by the GPT-Live launch** — its flip conditions were met; see the pivot design) suitability analysis of full-duplex speech models for a mostly-silent, tool-firing agent: the half-duplex vs full-duplex distinction, the tool-calling gap, a candidate-model table, and when the verdict would flip.
- [Frontier Design Briefing: Making "Duplex" Actually Work](docs/research/duplex-frontier-design.md) — generative design study of architectures for continuous-listen + sparse silent structured firing (action-channel, frozen-head, per-frame posterior, cascade-as-duplex), with ranked proposals, a staged build plan, and the highest-upside frontier bet.

---

## License

MIT
