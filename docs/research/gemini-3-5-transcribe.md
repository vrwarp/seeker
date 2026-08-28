# Gemini 3.5 Transcribe — Research Briefing

*Research date: 2026-08-27 (launch day — Google announced both models 2026-08-26/27). Multi-pass web research: official docs deep-dive, SDK/cookbook source reading, community sweep, and four targeted follow-ups on the decision-critical gaps. Confidence is marked CONFIRMED (fetched verbatim from an official page or SDK source) / REPORTED (press or secondary source) / ANECDOTE (single community report) / UNCONFIRMED throughout. Companion docs: [gpt-live-pivot.md](../design/gpt-live-pivot.md) (the architecture this briefing evaluates against), [full-duplex-suitability.md](full-duplex-suitability.md) (the cascade "dark horse" this model partially revives).*

**The question this briefing answers:** the [launch post](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-5-transcribe/) advertises *"continuous, bidirectional streaming with sub-second latency … via the Live API using gemini-3.5-transcribe-live"* plus *"the model can delegate complex tasks … to other Gemini models via function calls."* Continuous streaming + function calls from live audio reads like Seeker's missing primitive. Is it?

**The answer, up front:** no — the function-calling half of that sentence is a **Gemini-macOS-app product feature, not a developer API capability**. The developer docs are explicit that `gemini-3.5-transcribe-live` supports *no* function calling, no Google Search, and not even system instructions; it is a pure ASR pipeline. It cannot be Seeker's decision brain. But the transcription half is real and architecturally interesting: it streams interim transcripts **continuously, without waiting for turn boundaries**, takes a 1,000-term custom vocabulary (lyrics!), eats Seeker's native 16 kHz PCM, and costs ~$0.80 per 90-min service. It is a serious candidate for Seeker's **perception plane** — and its batch sibling, with word-level timestamps, is the missing tool for the **eval harness**. The conductor architecture survives intact.

---

## Part A — What launched

Two dedicated speech-to-text models "based on Gemini's audio understanding" — CONFIRMED via the [Gemini API changelog](https://ai.google.dev/gemini-api/docs/changelog) (entry dated 2026-08-26, status GA; the blog says "public preview" — discrepancy noted, models page badges them "New Stable"):

| | `gemini-3.5-transcribe` | `gemini-3.5-transcribe-live` |
|---|---|---|
| API surface | **Interactions API** (unary POST, `client.interactions.create`) | **Live API** (WebSocket, `client.aio.live.connect`) |
| Audio limit | 1 hr/request (30 min with diarization or word timestamps) | **10 min/session** |
| Diarization | Yes — up to 8 speakers, attribution for 3+ "experimental" | **No** |
| Word-level timestamps | Yes (`word_info` annotations: `text`, `speaker`, `start_offset`, `end_offset`) | **No** — utterance-level events only |
| Custom vocabulary | Up to 1,000 terms ("best results … up to 100") | Same |
| Modes | `verbatim` (default) / `smart` | `VERBATIM` (default) / `SMART` |
| WER (Google-cited, Artificial Analysis) | 2.6% avg, 5.04% FLEURS | 4.0% avg, 5.50% FLEURS |
| Paid price (per 1M tokens) | $2.00 audio-in / $12.00 text-out ≈ **~$0.005/min blended** | $3.50 audio-in / $21.00 text-out ≈ **~$0.009/min blended** |
| Free tier | "Free of charge" | "Free of charge" |

- Tokenization — CONFIRMED from the [pricing page](https://ai.google.dev/gemini-api/docs/pricing) footnote: *"25 audio tokens per second for input and 175 text tokens per minute for output."* (Note: differs from the general Gemini audio rate of 32 tok/s — the transcribe models are billed on their own meter.)
- Latency — REPORTED: *"time to final transcription improves by 70%"* vs Chirp 3 (Google); ~0.40 s end-of-speech → final transcript (press). Interim-update cadence: unpublished.
- Model card ([models/gemini-3.5-transcribe](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe)) — CONFIRMED not supported: *"Caching, code execution, file search, **function calling**, image generation, thinking, batch API, flex inference, priority inference."*
- 85+ languages, automatic detection and mid-stream code-switching; BCP-47 `language_codes` config.
- Not on Vertex AI as of launch day (Vertex release notes have no entry); enterprise path is "Gemini Enterprise Agent Platform (public preview)."
- Rate limits: **no public numbers** for either model — the [rate-limits page](https://ai.google.dev/gemini-api/docs/rate-limits) defers to the login-gated AI Studio dashboard. Google staff position (July 2026 forum): Live API limits are enforced as **TPM, not guaranteed concurrent sessions**. At 25 tok/s one stream is 1,500 input tokens/min — TPM will never be Seeker's binding constraint; undocumented free-tier concurrency policy might be.
- Try it instantly: [aistudio.google.com/live?model=gemini-3.5-transcribe-live](https://aistudio.google.com/live?model=gemini-3.5-transcribe-live).

---

## Part B — `gemini-3.5-transcribe-live`: the Live API surface

*Everything in this part is CONFIRMED verbatim from [live-api/live-transcribe](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe), the [Live API reference](https://ai.google.dev/api/live), and `google-genai` SDK source unless marked.*

### The interaction model — and why it dodges the VAD-cognition trap (for perception only)

The docs draw a hard line between the Live API's two modes:

> *"Live Transcription operates as a dedicated, low-latency speech recognition pipeline rather than a conversational agent."*

| Feature | Live Agent | Live Transcription |
|---|---|---|
| Interaction style | "Turn-based dialogue with pause detection and interruptions." | **"Continuous stream processing as the speaker talks."** |
| Response modality | audio + text (`["AUDIO"]`) | text only (`["TEXT"]`) |
| Supported features | "Function calling, Google Search, system instructions." | "Speech biasing (`custom_vocabulary`), language detection, manual & hybrid VAD, Smart transcription." |

This is the load-bearing fact. The 2025-era Gemini failure ([pivot doc Part II](../design/gpt-live-pivot.md)) was **VAD-gated cognition**: the model could only *think* at segment boundaries, and music never provides them. A transcription pipeline doesn't have that problem *for transcripts*: **interim hypotheses stream continuously while speech is active, with no turn boundary required.** VAD here gates only *finalization*, not perception. The trap is dodged — but note carefully: it is dodged because there is no cognition to gate. The moment you want a decision (a tool call), Google routes you back to turn-based Live Agent models (Part C).

### Connection and setup

- Endpoint: `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${API_KEY}` — the same BidiGenerateContent socket `gemini_session.py` already speaks.
- SDK: Python `client.aio.live.connect(model, config)` (`google-genai` ≥ 2.19/2.20 for the `mode` enum); JS `ai.live.connect({model, config, callbacks})`.
- Full config surface (there is no `tools`, no `system_instruction`):

```python
config = types.LiveConnectConfig(
    response_modalities=["TEXT"],                      # required; TEXT only
    input_audio_transcription=types.AudioTranscriptionConfig(
        language_codes=["en-US"],                      # [] = auto-detect, 85+ languages
        custom_vocabulary=["Gemini", "Kubernetes"],    # ≤1,000 terms, best ≤100
        mode="VERBATIM",                               # default; or "SMART"
    ),
    realtime_input_config=types.RealtimeInputConfig(   # optional: VAD control
        automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
    ),
)
```

Raw WebSocket setup: `{"setup": {"model": "models/gemini-3.5-transcribe-live", "generationConfig": {"responseModalities": ["TEXT"]}, "inputAudioTranscription": {...}, "realtimeInputConfig": {...}}}`.

- Ephemeral tokens work and can be constraint-locked to this model (`client.auth_tokens.create` with `live_connect_constraints`) — irrelevant for Seeker's server-side daemon, noted for completeness.

### Audio input

- *"Raw 16-bit PCM at 16kHz (mono, little-endian)"*, MIME `audio/pcm;rate=16000`, recommended ~100 ms chunks. **This is exactly Seeker's capture format** — no `LinearResampler`, no 24 kHz upsample (the general Live API resamples other rates if needed, but 16 kHz is native).
- Send: `await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))` — fire-and-forget appends, same shape as today's `realtimeInput`.

### Transcript events — interim vs. final (the part the tracker cares about)

Two fields on `server_content`:

- `interim_input_transcription.text` — *"low-latency, speculative partial hypotheses updated while the speaker is actively talking … occur rapidly with minimal delay"* / SDK docstring: *"subject to frequent updates."*
- `input_transcription.text` — *"the finalized transcript emitted when the speaker pauses, the turn completes, or speech is finalized … the model's authoritative transcription of that speech segment."* Also: *"sent independently of the other server messages and there is no guaranteed ordering."*

Interim semantics — CONFIRMED against four independent production integrations (LiveKit agents, Pipecat, Soniox's comparison harness, and a macOS dictation app's design doc, all reading the same events): **each interim is a full rolling rewrite of the current utterance, not an append-only delta.** Prefixes can change between interims; the final can differ from the last interim (especially in SMART mode); a final clears the interim state. Consequences for `PositionTracker`:

1. Feed the tracker the **whole current interim string** and treat it as replaceable; re-anchor on finals. Do not diff interims into deltas.
2. Use **`VERBATIM`** (the default). SMART mode *"removes filler words and formats intent-aware text"* — i.e., it rewrites, which is poison for verbatim lyric matching (community ANECDOTE: SMART also deletes semantic content, e.g. *"I hesitated to check it, I should have verified"* → *"I should have verified"*).
3. Defensive pattern from LiveKit: promote a pending interim to a final on turn-complete/disconnect — they evidently don't trust a server final to always arrive.

### The three VAD strategies (and which one Seeker would use)

1. **Automatic (default)** — server VAD detects speech start/stop; a final is emitted when the speaker pauses. Under wall-to-wall worship audio, finals may simply not arrive for minutes (Soniox observed continuous speech finalizing in *"long turns that can span several utterances"* bounded by ~2 s silences). **Interims keep flowing regardless** — and interims are what the tracker eats.
2. **Hybrid** — automatic VAD stays on; the client sends `audio_stream_end` at moments of its choosing: *"The server treats `audio_stream_end` as an immediate turn finalization prompt, bypassing the default server-side silence wait time"* … *"The client can resume sending audio data at any time."* This is a genuine client-driven "finalize now" primitive — Pipecat sends it repeatedly on every client-detected pause within one session, and streaming continues. If Seeker wants authoritative finals on its own cadence (e.g. at tracker-sensed slide boundaries), this is the documented lever. Unquantified risk: forcing turn breaks mid-phrase may degrade quality (Soniox: aggressive endpointing *"degrades the transcript, since the model loses the surrounding context"*).
3. **Manual / push-to-talk** — `automatic_activity_detection.disabled: true` + explicit `activity_start`/`activity_end`. Documented for this model, and *this model's* docs frame it as supported (walkie-talkie use). But: finals are only documented to arrive after `activity_end`; `audio_stream_end` is explicitly forbidden in this mode; and the 2025 **ActivityEnd hang / 1011 keepalive bug** ([pivot doc §II.4](../design/gpt-live-pivot.md), two forum threads on the 2.5-era model) has **no confirmed fix** — both threads remain open/unanswered as of 2026-07-31 and no changelog entry mentions it. UNCONFIRMED whether the new model shares the bug. Avoid: Seeker doesn't need this mode.

**Recommended posture for Seeker: mode 1, consume interims, ignore final cadence; optionally graduate to mode 2 if finals prove more accurate than interims for anchoring.**

### Session limits — the 10-minute problem

- CONFIRMED, verbatim from the Limitations section: *"Session duration: Live transcription sessions support continuous streaming for up to 10 minutes."* Tighter than the general Live API's 15-min audio-only cap, and the page mentions **nothing** about session resumption, context-window compression, or GoAway for this model — checked twice, including the plain-text `.md.txt` rendering.
- The general Live API has all three (`session_resumption` handles valid 2 h, `GoAway.timeLeft` ~60 s warning, sliding-window compression), and the LiveKit plugin reports transcribe-live sessions do get a GoAway at ~10 min and simply reconnects in ~0.1 s. Whether a resumption handle extends a transcribe-live *session* is UNCONFIRMED — nothing rejects it, nothing documents it. Empirical test required.
- **This matters much less for a transcription sidecar than it did for the decision brain.** The 2025 pain was losing the *model's positional memory* across resumption churn. A transcription session holds no state Seeker cares about — the tracker *is* the memory. Rotation is cheap: reconnect (~0.1 s per LiveKit), or overlapped dual-connection rotation (open B, feed both for 2 s, drop A) for a genuinely zero-gap seam. ~12 rotations per 2 h service.
- ANECDOTE worth watching: *"randomly exactly 20 seconds of silence will lead to a 403"* (HN, launch day, single report). Sermons contain prayers and long pauses; the reconnect path must treat any close as recoverable (Seeker's already does).

### Minimal streaming loop (verbatim-adjacent, from the docs + cookbook `Get_started_transcribe.ipynb`)

```python
async with client.aio.live.connect(model="gemini-3.5-transcribe-live", config=config) as session:

    async def pump_audio():                       # ~100 ms chunks, 16 kHz PCM16 mono
        while True:
            chunk = await audio_queue.get()
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

    async def pump_transcripts():
        async for msg in session.receive():
            sc = msg.server_content
            if sc and sc.interim_input_transcription:
                tracker_feed(sc.interim_input_transcription.text, final=False)
            if sc and sc.input_transcription:
                tracker_feed(sc.input_transcription.text, final=True)
```

---

## Part C — The function-calling claim, investigated

The blog sentence that motivated this briefing:

> *"The model can delegate complex tasks (such as image generation and file analysis) to other Gemini models via function calls."* — immediately followed by: *"**Currently available in the Gemini macOS app.**"*

- CONFIRMED: that qualifier is the whole story. The delegation feature is the Gemini macOS app's dictation surface calling sibling models in the background (*"summarize local files, repurpose text across apps, or generate images right at your cursor — using just your voice"*). The [DeepMind model page](https://deepmind.google/models/gemini-audio/ai-transcription/) lists the capability with no API surface; **no developer doc exposes any tool mechanism for either transcribe model**.
- CONFIRMED: the live-transcribe docs put function calling on the Live-Agent side of the feature table (above); the model card lists function calling under *not supported*; the [Live API tools page](https://ai.google.dev/gemini-api/docs/live-api/tools)'s model-support table (`tools`, mid-stream `tool_call` messages, `send_tool_response`, `NON_BLOCKING` + `INTERRUPT`/`WHEN_IDLE`/`SILENT` scheduling) covers only Gemini 3.1 Flash Live (*"synchronous only"*) and Gemini 2.5 Flash Live (*"synchronous and asynchronous"*) — transcribe-live is absent. The batch model's Interactions API request shape (`generation_config.transcription_config`) has no tools field either. Whether passing `tools` hard-errors or is silently ignored is undocumented (trivial to test; changes nothing).
- Press coverage split on this exactly as you'd expect: outlets restating the blog said "function calling"; the one outlet that read the docs listed it unsupported.

**So the all-Gemini alternative is unchanged.** A tool-calling Gemini brain still means a Live *Agent* model — the same turn-based, VAD-gated interaction model the pivot diagnosed, now with extra 2026 baggage: 3.1 Flash Live is **synchronous-only** tool calling (a regression vs 2.5's `NON_BLOCKING`+`SILENT` — the fire-and-forget seam Seeker's legacy path depends on; `config.py` already warns about exactly this), an open issue reports its tool calling "hit-or-miss," its `inputTranscription` **no longer streams incrementally** (arrives only at utterance end — a March 2026 regression thread), plus open VAD bugs (`silence_duration_ms` ignored, dead VAD for the first 10–17 s, turn thrashing). The act-anytime tool primitive still does not exist at Google. It still lives where the pivot found it: OpenAI's conductor mode today, gpt-live when its API ships.

---

## Part D — `gemini-3.5-transcribe` (batch) and the Interactions API

The batch sibling matters to Seeker for one reason: **it is the eval-harness tool the project has never had.** ([Pivot doc Part VII](../design/gpt-live-pivot.md): "The eval harness remains the highest-ROI unbuilt tool.")

- Surface — CONFIRMED: `POST https://generativelanguage.googleapis.com/v1beta/interactions`, or `client.interactions.create()` (`google-genai` ≥ 2.3.0). Upload audio via the Files API, pass `{"type": "audio", "uri": ..., "mime_type": ...}`; plain transcript in `interaction.output_text`.
- Word-level ground truth — CONFIRMED: with `transcription_config: {"mode": {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]}}` (docs nest the options under `mode`; the cookbook puts them at `transcription_config` top level — both shapes appear in first-party sources, test which is canonical), the response carries per-word annotations: `{"type": "word_info", "text": "Hello", "speaker": "spk_1", "start_offset": "0.100s", "end_offset": "0.450s"}`.
- Limits: 1 hr/request, **30 min when word timestamps or diarization are on** → chunk recorded services; up to 8 speakers.
- Cost: ~$0.005/min blended → a 90-min service ≈ **$0.45** for a word-timestamped gold transcript. Free tier: free.

**What this buys:** run every recorded service through it once; align the word-timestamped transcript against the slide deck to produce gold fire-timings; every conductor/tracker change thereafter gets scored on fire-offset and false-fire rate against real worship audio. This is buildable this week and carries zero architectural risk.

---

## Part E — Scored against Seeker's requirements

Against the checklist the pivot established ([Part I/II](../design/gpt-live-pivot.md)), for the role it can actually play (perception plane), with the decision-brain verdict inline:

| # | Requirement | `gemini-3.5-transcribe-live` |
|---|---|---|
| 1 | Continuous ingestion, no VAD stall | ✅ **for transcripts** — interims stream continuously; VAD gates only finalization. (The 2025 failure was VAD-gated *cognition*; there is no cognition here to gate.) |
| 2 | Client-drivable or act-anytime decision points | ❌ as a brain — no tools, no system prompt, no decisions. ✅ as perception: `audio_stream_end` is even a client-driven "finalize now" clock if wanted. |
| 3 | Silent tool calls from live audio | ❌ — Live-Agent-only feature; the macOS-app delegation is not in the API. |
| 4 | Streaming transcripts for the tracker | ✅ — this is the whole product. Interims are rolling rewrites (feed whole-string, `VERBATIM` mode); finals authoritative but music-sparse under default VAD. |
| 5 | Latency (≤1 s budget; ~1 s song anticipation) | ✅ likely — "sub-second" claimed, ~0.40 s to *final* reported; interim cadence unpublished, expected faster than today's 0.8 s commit-paced whisper transcripts. Measure. |
| 6 | 2 h session survival | ⚠️ 10-min cap, resumption undocumented — but a transcription sidecar is stateless, so reconnect/overlap rotation is cheap (~0.1 s observed). Not the liability it was for the brain. |
| 7 | Manuscript context + mid-session state injection | ❌/n.a. — no system instructions at all. The deck lives with the brain and tracker, not here. |
| 8 | Lyric/scripture vocabulary priming | ✅ **best-in-class primitive**: `custom_vocabulary` up to 1,000 terms. Load the song's distinctive lyric words, section labels, book-of-the-Bible names per service. Whisper's `prompt` field has no equivalent term-biasing contract. |
| 9 | Silence/HOLD discipline | n.a. — it only ever emits transcripts. (Watch the 20 s-silence 403 ANECDOTE.) |
| 10 | Audio format | ✅ exactly 16 kHz PCM16 mono — Seeker's capture format, resampler-free. |
| 11 | Cost | ✅ ~$0.78/90-min service ($0.45 audio-in + ~$0.33 text-out) vs $1.53 for the whisper perception plane. Free tier exists for prototyping. |
| 12 | Rate limits | ⚠️ unpublished; TPM trivially low (1,500/min); free-tier concurrency policy unknown. |
| 13 | Guards stay client-side | ✅ trivially — it can't execute anything. |
| 14 | Singing/music robustness | ❓ **the load-bearing unknown, again** — zero coverage of music input anywhere (same open question as gpt-live and whisper). WER 4.0% is speech benchmarks. Only a bake-off answers this. |

---

## Part F — Verdict and recommended actions

**The verdict in one sentence:** Google shipped half of Seeker's missing primitive — continuous unsegmented *perception* — but kept the other half (act-anytime *action*) locked in turn-based Live Agent models, so the conductor architecture stands, and this model competes only for the perception plane and the eval bench, where it is genuinely strong.

Mapped onto the pivot's planes: **decision plane — no change** (OpenAI conductor today, gpt-live later; an all-Gemini stack is still architecturally broken for worship, and 3.1's synchronous-only tools make it *worse* than the 2.5 legacy path). **Perception plane — real contender**: interim-streaming transcription with lyric-term biasing, at native 16 kHz, at half the whisper cost, with a plausibly faster transcript cadence than commit-paced whisper — which directly tightens the tracker's autofire/boundary-tick loop, the mechanism song mode lives on. **Eval bench — adopt now**: word-timestamped batch transcription is the gold-timing source the harness has been missing.

Priority order:

1. **Build the eval harness on `gemini-3.5-transcribe` (batch).** Recorded services → 30-min chunks → word-timestamped verbatim transcripts (~$0.45/service) → gold fire-timings. No architecture touched; every later experiment (including #2 and any future gpt-live test) becomes measurable.
2. **Bake off the perception plane.** Stream recorded worship (board mix + vocals-only stems) through a transcribe-live session and measure: interim latency/cadence; lyric WER with and without `custom_vocabulary`; whether finals ever arrive mid-song under default VAD; behavior at 10:00 (GoAway? close code?), whether `session_resumption` config is accepted; the 20 s-silence 403. Compare tracker performance (fed whole-interim strings) against today's whisper-commit feed on the same audio. Free tier makes this a $0 experiment.
3. **If the bake-off wins: add a transcription-sidecar seam, not a new brain.** A small `GeminiTranscribeFeed` (own WebSocket, own reconnect/rotation, ~12 rotations per service, overlapped-handoff for zero-gap) that tees the audio queue and feeds `tracker.feed()`/hint escalation, alongside the OpenAI session. Keep OpenAI-side transcription enabled regardless — it also serves context economics (audio→text token swap) in the decision session; the sidecar is an upgrade to the tracker's ear, not a replacement of that. The `RealtimeBrain` protocol is untouched; this slots in beside a brain, not as one.
4. **Do not** revisit `provider: gemini` as the brain on the strength of this launch. The blocking facts of 2025 all still stand in 2026: turn-gated cognition, no working manual-VAD escape (hang unfixed as far as any public source shows), no text-injection channel, and now synchronous-only tool calls on the current Live Agent model.

**Open questions to instrument (day-one unknowns):** interim cadence and latency distribution on music; lyric-WER delta from `custom_vocabulary` (and its practical term budget — 100 vs 1,000); mid-song finalization behavior; 10-min-cap semantics (GoAway timing, resumption acceptance, rotation seam loss); silence-handling bugs over long prayers; free-tier vs Tier-1 concurrency for the dual-connection rotation pattern.
