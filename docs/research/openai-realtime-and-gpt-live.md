# OpenAI Realtime API & GPT-Live — Research Briefing

*Research date: 2026-07-11 (two web-research passes: GPT-Live launch coverage; Realtime API GA documentation deep-dive). Confidence is marked CONFIRMED / RUMORED / SPECULATION throughout. Companion design doc: [gpt-live-pivot.md](../design/gpt-live-pivot.md).*

---

## Part A — GPT-Live: the full-duplex model (announced 2026-07-08)

### What it is — CONFIRMED

- **GPT-Live** (models **GPT-Live-1** and **GPT-Live-1 mini**) was announced by OpenAI on **July 8, 2026** ([openai.com/index/introducing-gpt-live](https://openai.com/index/introducing-gpt-live/)) and now powers ChatGPT Voice, replacing Advanced Voice Mode. GPT-Live-1 is default for Go/Plus/Pro; mini for Free. Not yet in Business/Enterprise workspaces.
- **The API is not available yet.** OpenAI's complete statement: *"We also plan to bring them to the API soon"* — a notify-me signup form, no model IDs, no pricing, no timeline. ([apidog analysis](https://apidog.com/blog/gpt-live-api/))
- It launched **two days after** gpt-realtime-2.1 / 2.1-mini hit the Realtime API (July 6) — the realtime line remains turn-based; GPT-Live is a separate, genuinely full-duplex architecture.

### The full-duplex claims — CONFIRMED (OpenAI's own language)

Quotes reproduced consistently across [TechCrunch](https://techcrunch.com/2026/07/08/openai-releases-new-voice-models-for-more-natural-live-conversations/), [MarkTechPost](https://www.marktechpost.com/2026/07/08/openai-releases-gpt-live-and-gpt-live-1-mini-full-duplex-voice-models-that-delegate-deeper-reasoning-to-gpt-5-5/), [SiliconANGLE](https://siliconangle.com/2026/07/08/openai-launches-gpt-live-voice-model-series-ahead-broad-gpt-5-6-release/), [MLQ](https://mlq.ai/news/openai-launches-gpt-live-1-a-full-duplex-voice-model-that-listens-and-speaks-simultaneously/):

> *"Built on a full-duplex architecture, meaning it can listen and speak at the same time."*
>
> *"Instead of processing a sequence of separate messages, GPT-Live continuously processes input while generating output."*
>
> The model *"can make interaction decisions many times per second: whether to speak, continue listening, pause, interrupt, **or invoke a tool**."*

**Why this matters for Seeker:** that last sentence is precisely the missing primitive — sub-second act-anytime decisions (including tool calls) over an unsegmented stream. No VAD, no turn boundaries, no response monopoly. It also interprets **intonation**, not just words — delivery-sensitive perception, which is the signal content-matching can never see (relevant to chorus-repeat disambiguation and pre-line anticipation).

Additional confirmed details:

- Two-layer split: the full-duplex speech model handles the live channel and **delegates deeper reasoning to GPT-5.5** in the background (*"decoupling conversational tempo from reasoning depth"* — [kie.ai](https://kie.ai/blog/gpt-live-full-duplex-voice-model-deep-dive)). Three intelligence levels: Instant / Medium / High.
- Evals: preferred over Advanced Voice Mode in **75.7%** of matched conversations (mini 69.2%); GPQA 84.2% via delegation vs 45.3% for AVM ([The Decoder](https://the-decoder.com/chatgpt-can-now-listen-and-talk-at-the-same-time-making-ai-conversations-seem-more-human/)).
- Duplexness verified by testers: it interrupts and backchannels *while the user is still talking* (Simon Willison's preview notes, [HN 745-point thread](https://news.ycombinator.com/item?id=48834405)); claimed better isolation of the speaker from background noise ([Android Authority](https://www.androidauthority.com/openai-gpt-live-voice-model-3685616/)).
- **Latency: no official figures** ("under 200 ms" is single-outlet RUMOR). **Singing/music as *input* was not addressed in any coverage** — an honest open question for worship audio; must be tested the day API access lands.

### What the GPT-Live API will look like — SPECULATION (clearly marked)

- No official statement that GPT-Live joins the Realtime API, replaces it, or arrives as a new surface ([apidog](https://apidog.com/blog/gpt-live-vs-gpt-realtime/)).
- Community expectation: session semantics shift "toward continuous bidirectional events"; server VAD/turn-detection events may disappear entirely; *"architecturally, GPT-Live looks like what the Realtime API's next generation wants to be."*
- gpt-realtime-2.1 shipping days earlier with the turn-based event model **unchanged** suggests GPT-Live arrives as a distinct session type rather than a drop-in model swap (inference, not confirmed).

**Design consequence for Seeker:** we cannot code against the gpt-live API today; we *can* structure Seeker so the only thing gpt-live changes is *who owns the decision clock*. That is exactly what the `turn_mode` seam in `openai_session.py` does (`conductor` today → `full_duplex` later).

---

## Part B — The Realtime API (GA), as it exists today

*Everything below is CONFIRMED against developers.openai.com (GA interface; the beta interface was shut down 2026-05-12; gpt-4o-realtime-preview models were shut down 2026-05-07).*

### The capability Gemini Live does not have

With **`turn_detection: null`** the server never segments audio and never decides anything on Seeker's behalf ([VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad)):

1. `input_audio_buffer.append` — stream forever; appends are fire-and-forget, never paused by an active response (no half-duplex dead zone). 15 MiB max per append event; small frames recommended.
2. `input_audio_buffer.commit` — turns the buffer into a user audio item in the conversation and clears the buffer. *"…will trigger input audio transcription (if enabled), but **it will not create a response from the model**."* Committing is how audio becomes visible to inference; uncommitted audio is invisible.
3. `response.create` — inference happens **only when the client asks** (in manual mode). *"…the client must manually trigger model response."*
4. Tool calls happen **only inside a Response** — so with manual triggering, tool timing is deterministic: commit → decide → (maybe) fire.

This is the documented, supported inversion of control that Gemini's `automaticActivityDetection.disabled: true` promises but breaks (hang + 1011 keepalive). One middle mode exists (server VAD with `create_response: false`) but still starves on continuous music — not usable for worship.

### Session model

- Transport for Seeker: **WebSocket** — `wss://api.openai.com/v1/realtime?model=…`, `Authorization: Bearer` (server-side key; docs recommend WebSocket "when your server already receives raw audio from a media pipeline"). WebRTC/SIP exist for browser/telephony; irrelevant here.
- **Audio input: PCM16, 24 kHz, mono only** (`{"type": "audio/pcm", "rate": 24000}`; docs: "Only a 24kHz sample rate is supported"). G.711 variants exist for telephony. → Seeker resamples its 16 kHz capture.
- `session.update` (GA nested shape): `session.type: "realtime"`, `instructions`, `output_modalities: ["text"]` (text XOR audio — **text-only output is first-class**, killing the AMEN pathology), `audio.input.{format, noise_reduction, transcription, turn_detection}`, `tools`, `tool_choice`, `max_output_tokens` (1–4096 or `inf`), `reasoning.effort` (2/2.1 models), `truncation`. Everything except `voice`/`model` updatable mid-session.
- **Hard 60-minute session cap** (verbatim: "The maximum duration of a Realtime session is 60 minutes"), **no resume** — a dropped socket loses the server conversation. 90–120-min services therefore need **session rotation** (community-standard pattern: open B before killing A, re-prime B with a small state item). Seeker's position state is tiny and client-held, which makes rotation nearly free.
- `input_audio_buffer.clear` discards uncommitted audio (useful at rotation).

### Turn detection modes (for completeness)

| Mode | Mechanism | Fit for worship |
|---|---|---|
| `server_vad` | volume threshold + `silence_duration_ms` (default 500 ms), auto-commit + optional auto-response, `idle_timeout_ms` | Fails — continuous music has no silences (the Gemini failure, reproduced) |
| `semantic_vad` | turn-completion model, `eagerness` low/medium/high (max timeouts 8/4/2 s) | Fails — still a turn segmenter |
| **`null` (manual)** | client commits and triggers responses | **The fix** — no VAD anywhere in the trigger path |

### Input transcription (the perception plane)

- Per-session config: `audio.input.transcription = {model, language, prompt, delay}`; events `conversation.item.input_audio_transcription.delta` / `.completed` per committed item. Runs **asynchronously through the /audio/transcriptions endpoint** — guidance, not exactly what the S2S model heard.
- **`gpt-realtime-whisper`** (2026-05-07): "natively streaming and designed for realtime sessions", tunable `delay: minimal|low|medium|high|xhigh`, **$0.017/min flat**. Dedicated transcription sessions (`session.type: "transcription"`) use the same machinery with no response generation at all — the cheapest continuous-lyrics channel imaginable.
- GA also **swaps audio tokens for transcript text in context** when a transcript is available — enabling transcription *reduces* long-session context cost.
- Caveat: Whisper-family singing transcription degrades with melisma/instrumentation — but Seeker fuzzy-matches against *known* lyrics, a far easier problem than open transcription.

### Function calling

- Tools: flat `{type: "function", name, description, parameters: <JSON Schema>}`. `tool_choice` supports `auto`/`none`/`required`/force-specific — per-response overrides allowed.
- Arguments stream (`response.function_call_arguments.delta` → `.done`); the completed call appears in `response.output_item.done` / `response.done.output[]` with `call_id`.
- Results return via `conversation.item.create {type: "function_call_output", call_id, output}` — and a follow-up `response.create` is **optional**: for fire-and-forget slide triggers, inject the output and don't ask the model to react. (The narration-leak class of bug is structurally impossible: output is text-only, and nothing plays it.)
- Realtime models do **not** support structured outputs — the tool schema *is* the structured channel.
- MCP servers can be attached (`tools: [{type: "mcp", …}]`) with server-side execution — a future option to expose ProPresenter as MCP.

### Out-of-band responses

`response.create {response: {conversation: "none", metadata: {...}, input: [...item_references...]}}` runs analyses **in parallel** with the main conversation without writing to it ("Only one Response can write to the default Conversation at a time, but otherwise multiple Responses can be created in parallel"). OpenAI's own cookbook demonstrates per-commit out-of-band processing. → Seeker can run "sanity audit: what section are we in?" probes without ever perturbing the decision stream.

### Models & pricing (per 1M tokens; audio in = 600 tok/min, audio out = 1,200 tok/min)

| Model | Context | Text in/cached/out | Audio in/cached/out | Notes |
|---|---|---|---|---|
| **gpt-realtime-2.1** (2026-07-06) | 128k | $4 / $0.40 / $24 | $32 / $0.40 / $64 | flagship; `reasoning.effort`; −25% p95 latency |
| **gpt-realtime-2.1-mini** | — | $0.60 / $0.06 / $2.40 | $10 / $0.30 / $20 | the cost-relevant tier |
| gpt-realtime (GA 2025-08-28) | 32k | $4/$0.40/$16 | $32/$0.40/$64 | prior gen |
| **gpt-realtime-whisper** | 16k | — | **$0.017/min** | streaming STT; rate-limited in audio-min/min |

Rate limits (gpt-realtime-2.1): Tier 1 40k TPM → insufficient for dense polling; Tier 3 (800k) comfortable.

### Cost math for a 90-minute service (why the conductor is affordable)

- Raw audio input: 54k tokens → $1.73 (2.1) / $0.54 (mini) if billed once.
- Polling re-bills retained context per response — **prompt caching is the whole game**: cached audio input is $0.40/M vs $32/M (98.75% discount). With a lean `truncation: {type: retention_ratio, retention_ratio: 0.8, token_limits: {post_instructions: ~8k}}` budget (retention-ratio truncation exists specifically to amortize truncations and protect the cache prefix):
  - ~5 s cadence, ~10k tokens/response, ~97% cached → **≈$14–17/service on 2.1, ≈$6 on mini** (vs ≈$346 uncached — the naive design is unworkable, the cache-aware one is fine).
  - Adaptive cadence (Seeker ticks only when new audio exists, faster near boundaries) cuts this further.
- Transcription plane: 90 min × $0.017 = **$1.53 flat**.

### SDK note

`openai-python` has first-class GA realtime support (`client.realtime.connect(...)`), and the Agents SDK has a `RealtimeAgent` layer (opinionated, conversational). Seeker keeps its raw-`websockets` pattern (matches the existing Gemini session, zero new dependencies, full control of the event loop); the SDK remains an option if the event surface churns.

---

## Part C — The verdict this research forces

The June 2026 briefings concluded "full-duplex is a distraction" because **no production full-duplex model had tool calling** and the only research example (DuplexSLA) underperformed a cascade. Both premises are now obsolete:

1. **GPT-Live is a production, tool-calling, full-duplex model** — sub-second interaction decisions including tool invocation over unsegmented audio — sitting one API release away.
2. **The GA Realtime API already ships the control-plane inversion Seeker needs** (`turn_detection: null` + manual `response.create` + text-only output + streaming transcription), documented and supported — the thing Gemini's API promises and breaks.

The pivot is therefore two-staged with one architecture: build the conductor on gpt-realtime-2.1 now (removes VAD from the trigger path, fixes worship), and let gpt-live take the decision clock when its API ships (`turn_mode: full_duplex`).
