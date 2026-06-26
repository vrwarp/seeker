# Seeker — State of the Art for a Live-Audio Slide-Driving Agent
*Research briefing, 2026-06-26. Synthesized from 7 parallel web-research agents (273 sources), a freshness-verification pass, and an adversarial completeness critic. Freshness corrections are treated as authoritative over raw research where they conflict.*

---

## 0. Read this first — a correction to my own framing

The research was partly written against a prose description of Seeker rather than the code, and the verification/critic passes caught two things that change the recommendations:

1. **"Set the model to text-only so it silently observes" is impossible on native-audio Gemini.** Native-audio models only accept `responseModalities:["AUDIO"]`; TEXT is rejected. Seeker already runs `AUDIO` with a `Puck` voice and a real `_playback_loop` (it is *not* a silent observer today). The correct silence levers are (a) keep `AUDIO` + `scheduling:SILENT` tool responses and simply **don't play the audio back**, or (b) use Gemini **Proactive Audio** (accepting its "model decides when to talk" nondeterminism). Ignore any "use text modality" advice below.
2. **Seeker already uses the async tool feature people would tell you to switch for.** `gemini_session.py:264` already sets `behavior:NON_BLOCKING` + `scheduling:SILENT`. So the headline advantage of "alternatives with async tool calls" is partly already in hand. The real question is **firing precision**, not async-ness.

And the single most important strategic point from the critic:

> **The likely bottleneck is the missing control loop, not the model.** Seeker has no drift detection, no slide-state feedback to the model, no confidence gating, and it throws away the free `inputAudioTranscription` it already receives (`gemini_session.py` just logs `"Hearing: …"`). `get_current_slide_index()` and `/v1/status/slide` exist but are **never called**; `daemon.current_slide_index` is hardcoded `0`. Before A/B-testing gpt-realtime-2 vs Gemini 3.1, wiring a reconcile/interlock loop will probably fix more real-world misfires than any model swap.

---

## 1. Executive summary

- **There are now four credible cloud "brains"** for listen-and-fire-a-tool: Google **Gemini Live** (incumbent), **OpenAI gpt-realtime-2 / -mini**, **Amazon Nova 2 Sonic**, and **Azure Voice Live** (a managed wrapper, valuable mainly for its audio front-end). Anthropic Claude has **no** realtime speech-to-action API and is not a candidate for the audio-native path.
- **Pin your model string now.** The `gemini-live-2.5-flash-preview-native-audio-09-2025` snapshot was **removed March 19, 2026**. Target the stable alias `gemini-live-2.5-flash-native-audio`. Check `seeker/config.py` — this is a latent production break.
- **Your real cost is ~3× what the code comments imply.** Gemini 2.5 native-audio is **$3/M audio-in, $12/M audio-out** (text $0.50/$2.00), not ~$1/M.
- **Do not naively "upgrade" to Gemini 3.1 Flash Live.** It is synchronous-only on tools — it **regresses** the `NON_BLOCKING`/`SILENT` fire-and-forget behavior Seeker depends on. Validate tool-blocking behavior first.
- **The strongest model reconsiderations** are **Nova 2 Sonic** (best vendor speech-reasoning: 87 vs Gemini 2.5's 71 vs GPT-Realtime's 83 — and reasoning is exactly what position-tracking stresses) and **gpt-realtime-mini** (async + parallel tools, ~50× cheaper audio than gpt-realtime-2). Treat both as A/B candidates, not drop-ins.
- **The most controllable architecture is a cascade**, not a model swap: streaming ASR → a deterministic embedding/edit-distance **position sensor** → LLM only as a low-confidence tie-breaker. It's cheapest, debuggable, on-prem-capable, and removes the 15-min-cap/reconnect tax. Its one weakness (heavy paraphrase) is exactly where a retrieval layer + small-LLM fallback closes most of the gap.
- **You are not first to the use case** (Pewbeam, Loghema, WordCast Live), but every competitor uses ASR+verse-database matching. **Seeker's differentiation is native-audio + arbitrary slide-indexed manuscript following + ProPresenter tool control + human override.** Nobody surveyed combines all of these.

**My recommendation:** spend the next cycle on the *control loop and an eval harness*, run a 3-way model A/B (Gemini 2.5 incumbent vs gpt-realtime-mini vs Nova 2 Sonic) on recorded sermons, and prototype the cascade in parallel as the deterministic/on-prem hedge. Decide architecture from measured fire-precision, not vendor latency headlines.

---

## 2. The three architectural paradigms

### (a) Native-audio LLM — *Seeker today*
Raw PCM → one model that reasons over acoustics + semantics → `trigger_presentation_slide(index)`.
- **Pros:** best paraphrase / tangent / out-of-order-scripture tolerance; simplest data path; manuscript primes directly into context.
- **Cons:** least debuggable (no transcript of *why* it fired); least controllable (model chooses when to call the tool); cost grows with accumulated context over a 60–90 min session; operational fragility (15-min cap, reconnect/resumption, the **`SILENT` narration-leak bug** where the model still sometimes speaks the tool call); fire latency is gated by the model's turn-taking, not by any published TTFA number.

### (b) Streaming ASR + fast text LLM / matcher — *the cascade*
Streaming ASR (immutable partials) → small text LLM or matcher decides position → your code fires the slide.
- **Pros:** fully code-controlled advancement ("advance-only, never skip >N"); readable transcript + explicit confidence for debugging; flat, predictable cost; hours-long sessions with no caps; **can run fully on-prem**. ASR latency is no longer the bottleneck (leaders ~150–330 ms).
- **Cons:** you build and tune the matching logic; a mis-tuned threshold fires a wrong slide just as visibly; paraphrase needs the retrieval layer to be robust.

### (c) ASR/audio + deterministic alignment, LLM as tie-breaker — *the hybrid*
A cheap always-on **position sensor** (embedding retrieval + edit-distance over the transcript, with a locality/position prior) emits a top-k of plausible slides; fire directly on high confidence + locality; otherwise hand the **top-k (2–5 slides, never the whole deck)** to an LLM.
- **Pros:** bounds the worst failure (a wild index jump) structurally; gives a confidence signal for free that also powers human-override/hold-on-tangent; combines lexical (verbatim scripture) + semantic (paraphrase) strengths; mostly local/cheap.
- **Cons:** most engineering to build; repeated text (chorus) still needs the position prior to disambiguate.

**Bet:** (c) is the strongest end state for reliability; (b) is the fastest route to on-prem + determinism; (a) stays as the paraphrase-robust comparison baseline and the easy cloud path. The cheapest immediate win is to **bolt the deterministic position sensor from (c) onto the existing (a)** using the input transcription Seeker *already receives*, and require both to agree before firing.

---

## 3. Models to try

> Prices are per **million tokens** unless noted. Latency figures are vendor/benchmark and **do not** measure "fire a tool at the right semantic moment" — treat as directional. ✅ confirmed by freshness pass; ⚠️ caveat.

| Model | Approach | Tool calling | Audio price (in/out) | Context | On-prem? | Notes |
|---|---|---|---|---|---|---|
| **Gemini 2.5 Flash native-audio** *(incumbent)* | native S2S | ✅ NON_BLOCKING + SILENT/WHEN_IDLE/INTERRUPT | **$3 / $12** ✅ | 128k session | No | Only Gemini tier with true async tools. Pin to stable alias. `SILENT` narration-leak bug is real ⚠️ |
| **Gemini 3.1 Flash Live** | native S2S | ⚠️ **synchronous-only** (regression) | $3 / $12 ✅ | ~131k | No | Better instruction-following, but blocks on tool return → not a clean drop-in for fire-and-forget |
| **Gemini 3.5 Live Translate** | native S2S | n/a (translation-tuned) | — | — | No | Not for this task; listed for completeness |
| **OpenAI gpt-realtime-2** | native S2S, GPT-5-class reasoning | ✅ async + **parallel**, MCP | **$32 / $64** ✅ | 128k | No | Best reasoning; pricey; **no published latency for v2** ⚠️; WebSocket/WebRTC/SIP |
| **OpenAI gpt-realtime-mini** | native S2S | ✅ function calling | **$0.60 / $2.40** ✅ | 32k | No | ~50× cheaper audio than v2 — the cost-relevant OpenAI option for a one-way trigger task |
| **Amazon Nova 2 Sonic** | native S2S (Bedrock bidi stream) | ✅ async | **$3 / $12** ✅ | **1M** ✅ | No | **Best vendor speech-reasoning (87)**; only 4 regions; needs Bedrock streaming API + AWS auth |
| **Azure Voice Live** | managed wrapper over gpt-realtime/GPT-5.x | ✅ + VoiceRAG | model rate + add-ons | model-dep | No | Worth it for **noise suppression / echo cancel / end-of-turn** in a noisy sanctuary |
| **Qwen3-Omni / Qwen3.5-Omni** | open audio-native MoE | ✅ native "audio function call" | weights free (Apache-2.0) | large | **Yes (80–160GB VRAM)** | Closest open match to Gemini Live; hardware cost is the blocker ⚠️ |
| **Mistral Voxtral Mini 3B / Small 24B** | open audio LLM | ✅ "function-calling straight from voice" | weights free (Apache-2.0) | 32k | **Yes (~10GB VRAM)** | Most *deployable* audio-native + tools; Voxtral TTS (Mar 2026) completes the stack |
| **StepFun Step-Audio 2-mini / 2.5 Realtime** | open S2S | ✅ benchmarked voice tool calls | weights free (Apache-2.0) | — | **Yes (16–24GB)** | Most purpose-aligned open model for firing tools from speech ⚠️ (recall is *conversational*, not 90-min-silent) |
| **Phi-4-multimodal / Ultravox / Moshi** | open audio LLMs | Phi ✅ / Ultravox ✗ / Moshi ✗ | weights free (MIT/CC-BY) | small–med | Yes | Second-tier for this exact job; Moshi best mined for streaming STT |
| **Anthropic Claude** | text tool use only | ✅ (text) | — | — | — | **No realtime speech-to-action API**; only usable as the text-LLM in a cascade |

**How the top candidates slot into Seeker:**
- **gpt-realtime-mini** — near drop-in for the `connect → setup → stream → tool-call` loop; Python WebSocket-by-default matches Seeker's transport. Cheap enough to leave running. Validate it stays quiet through tangents.
- **Nova 2 Sonic** — biggest *quality* upside (reasoning + 1M context for the full manuscript), biggest *integration* cost (Bedrock `InvokeModelWithBidirectionalStream`, AWS auth, 4 regions). Worth a benchmark precisely because Gemini 2.5 is the weakest of the three on the reasoning the task stresses.
- **Voxtral Mini 3B / Step-Audio 2-mini** — the on-prem audio-native path on a single consumer GPU. The unproven part is "monitor for 90 minutes, fire rarely and precisely" — prototype that behavior first.
- **Gemini 3.1 Flash Live** — only after you confirm its synchronous tool calls don't stall your fire-and-forget trigger.

---

## 4. Semantic-alignment techniques (make tracking robust regardless of model)

The live version of Seeker's problem is a **solved research area** in Music Information Retrieval called **score following**. Mine it directly:

- **Online Time Warping (OLTW)** — streaming DTW; literally "track a live stream's position in a known reference in real time." Run over transcript tokens *or* embeddings. Off-the-shelf: **`pymatchmaker`** (ISMIR 2025).
- **JumpDTW** — DTW that may jump at block boundaries (= slide boundaries) with a penalty → natively handles "jumped back to re-read scripture" and "repeated the refrain."
- **Two-level HR/LR tracker (Brazier & Widmer)** — a coarse global estimator that re-anchors anywhere when confident, supervising a fast local matcher. **The best architectural template for Seeker's hazard profile** — mirror it with an embedding/LLM global estimator over a fast local matcher.
- **Switching-HMM (Nakamura)** — models arbitrary repeats/skips; gives a **position posterior** `P(slide | evidence)`. ~0.7 s avg recovery after a repeat/skip.
- **Embedding retrieval over per-slide chunks** (sentence-transformers + FAISS, <100 ms, local, free) — the most practical augmentation: returns top-k plausible slides; fire on high-confidence+locality, else constrain the LLM to 2–5 choices.
- **Edit-distance / approximate string matching** (`rapidfuzz`) — unbeatable on verbatim scripture quotes where embeddings are overkill. **Blend lexical + semantic over a locality window.**

**Hazard → mechanism map:**
| Hazard | Mechanism |
|---|---|
| Paraphrase | embedding/semantic retrieval (or LLM) |
| Skipped sections | jump-with-penalty (JumpDTW) / HMM skip transitions |
| Jump back to re-read | non-monotonic JumpDTW / global re-anchor |
| Long tangent / silence | a "no-match" state + confident global re-acquisition — **do not force-advance** |
| **Repeated text / chorus** | a **locality/position prior** — identical text is undisambiguable by content alone; you need "where were we just now" |

Two cheap, high-value moves regardless of paradigm: **(1)** prime the ASR/recognizer with the manuscript + scripture proper nouns as biasing context (EMNLP 2025 "Do Slides Help?" — slide text cuts WER ~34%, most on domain terms); **(2)** keep a **position posterior** and only allow advancing to nearby/plausible slides — high+local → auto-fire, ambiguous → hold/ask, sustained no-match → freeze.

*Avoid classic forced alignment (MFA, WhisperX, ctc-forced-aligner) as the live driver — it assumes verbatim, complete text and is offline/batch. Use it only to pre-time a rehearsal.*

---

## 5. Frameworks & infra

- **Two OSS leaders:** **Pipecat** (BSD-2, frame-based, most provider-agnostic) and **LiveKit Agents** (Apache-2.0, WebRTC-first, strongest transport + turn detection).
- **For Seeker's narrow profile** (server-side raw PCM in, *no talk-back*, just fire a tool), most of what frameworks sell — VAD, semantic endpointing, barge-in, jitter-buffered TTS playout, browser SDKs — **is unnecessary.** A thin custom daemon over a raw WebSocket is legitimate, arguably optimal.
- **The genuine value-add if you adopt one** is **model portability** (swap Gemini / gpt-realtime-2 / Nova Sonic behind one interface), reconnection/resumption plumbing around the 15-min caps, and clean tool routing — **not** conversational turn-taking. If adopting, **Pipecat** fits best (provider breadth, lightweight WebSocket transport). The **OpenAI Agents SDK realtime** path is a good fit *if* you try OpenAI (Python defaults to server-side WebSocket, matching Seeker).
- **Transport is settled in Seeker's favor:** raw WebSocket is correct — Seeker has no lossy last-mile and no TTS to pace, so WebRTC's advantages don't apply.
- **Turn detection** (Pipecat Smart Turn v3 ~12 ms CPU; Silero VAD) is SOTA but solves listener-endpointing Seeker doesn't do. Only relevant if you ever gate actions on speaker pauses.
- **Avoid Vocode** (unmaintained since ~Nov 2024). **TEN Framework** is credible but optimizes full-duplex conversation Seeker doesn't need.

---

## 6. Prior art — who else does this

- **Church startups (2025–26): Pewbeam, Loghema (ex-LogosAI), WordCast Live, faith.tools.** All do **ASR → verse/lyric-database matching**. None publicly uses a frontier **native-audio** model. All are **scripture/song-DB driven**, not arbitrary-manuscript/deck following. Vendor numbers (Pewbeam "~80 ms", WordCast "sub-280 ms / 99.2%", Loghema "0.5 s / 98%") are **self-reported marketing** — Pewbeam's own site only claims "under two seconds," and discloses no ProPresenter integration.
- **General position-tracking prior art: PromptSmart VoiceTrack** (patented voice-following teleprompter) — study its **hold-on-improvise / resume-on-return** UX. Plus theater **synchronized-captioning** patents (USPTO 10,692,497).
- **Mainstream tools do not do live voice advance at all:** ProPresenter automates only via timeline/timecode/schedule/manual; PowerPoint Speaker Coach/Cameo are rehearsal-only; Keynote/reveal.js auto-advance is time-based; Logos Proclaim transcribes post-hoc.
- **Seeker's defensible niche:** native-audio frontier model + arbitrary slide-indexed **manuscript** following + ProPresenter tool control + human override + session reconnect/resumption. **No surveyed product combines all of these.**

---

## 7. Presentation-control / actuator + drift correction

This ties directly to Seeker's biggest gap (no drift detection).

- **Trigger by absolute index against a specific UUID** so model drift never accumulates (vs repeated next/previous). The documented official path is `POST /v1/presentation/{uuid}/trigger/{index}`. **⚠️ Seeker's code currently does `GET /v1/presentation/{uuid}/{index}/trigger`** (index *before* `/trigger`, and GET not POST) — different shape and verb. **Verify against the venue's exact ProPresenter build** (`openapi.propresenter.com`); the API churned hard through the 21.x line in late 2025.
- **Close the loop with the chunked status stream:** `GET /v1/status/slide` (and `/v1/presentation/slide_index`) with `chunked=true` **push** a new index on every change — including a human manual advance. This is the **drift-correction + human-override sensor** Seeker is missing. Seeker even has `get_current_slide_index()` for `/v1/status/slide` already — it's just never called.
- **Defend against known ProPresenter index pitfalls:** the active-presentation index can return garbage; focused-vs-active-vs-announcement-layer ambiguity can report the wrong presentation; triggering an index in a *non-active* presentation does **not** focus it. Filter these so they don't masquerade as drift.
- **Human-in-the-loop seam: Bitfocus Companion.** The agent presses a button via Companion's HTTP/TCP/OSC API; the operator presses the *same physical Stream Deck button* → identical override path. Best of all: **auto-yield** — when Seeker *sees* a human-driven slide change on the status stream, pause auto-advance and re-anchor, rather than fighting the operator. (Note: the v21 beta broke the Companion ProPresenter module in Nov 2025 — version-test.)
- **Ready-made tool layer:** `alxpark/propresenter-mcp` wraps all **231 endpoints / 27 groups** as MCP tools — useful even just to mine exact paths and the string-vs-int index quirks.
- **Other backends:** OBS WebSocket v5 (scene-based, clean closed loop, <10 ms localhost) is the architectural template to copy; **reveal.js** is the most agent-native if Seeker ever renders its own deck (`Reveal.slide(h,v,f)` + `slidechanged` postMessage = absolute set + readback in one channel); PowerPoint COM / Keynote AppleScript are platform-locked, poll-only fallbacks.

**The reconcile pattern (copy this):** (1) absolute "go to N" command, (2) an event-push readback channel to compare intended vs actual, (3) a shared control surface so human and agent issue identical commands. Prefer push over polling; keep the link on localhost/wired LAN.

---

## 8. Concrete next experiments for Seeker (prioritized)

1. **Build an eval harness first.** Without it every choice is a guess. Collect labeled real sermon recordings; define metrics: **fire-offset (s) vs the correct moment, false-fires/hour, wrong-index rate, tangent-recovery time.** Note: none of the cited model benchmarks measure hour-long monitoring with rare precise firing — you must measure it yourself. *(Effort: M. Unblocks everything.)*
2. **Wire the control loop using signal you already have.** Consume `/v1/status/slide` (chunked) → update `daemon.current_slide_index`; feed observed index back to the model; **auto-yield** on human override. Add a deterministic position sensor over the **input transcription Seeker already receives** and require model+sensor agreement before firing. *Hypothesis: this cuts real-world misfires more than any model swap. (Effort: M.)*
3. **Constrain the tool schema.** Replace `trigger_presentation_slide(arbitrary_index)` with `advance_to_next()` / `jump_to_scripture(ref)` / `hold()`, and restrict the index space to a locality window. *Hypothesis: structurally eliminates the worst failure (a wild jump), model-agnostic. (Effort: S.)*
4. **3-way model A/B on recorded sermons:** Gemini 2.5 (incumbent, pinned alias) vs **gpt-realtime-mini** vs **Nova 2 Sonic**. Measure fire-precision + cost-per-sermon (with manuscript primed + sliding-window compression), not vendor latency. *Hypothesis: Nova/Realtime's better reasoning improves out-of-order/paraphrase firing. (Effort: M per model.)*
5. **Prototype the cascade as the on-prem/determinism hedge:** Parakeet (or Kyutai STT for its semantic end-of-turn) → embedding+edit-distance matcher with a locality prior → small-LLM fallback only on low confidence. *Hypothesis: matches cloud accuracy on this constrained task while being cheaper, debuggable, and offline. (Effort: L.)*
6. **Harden silence + fix the narration leak.** Confirm tool audio is never played; add explicit system-instruction guardrails against narrating tool calls (the `SILENT` bug is real on 2.5). Decide silence strategy explicitly: AUDIO+SILENT-and-discard vs Proactive Audio vs cascade. *(Effort: S.)*
7. **Make activation safe.** `daemon.activate()` currently fires real slides `[0,1,0]` on the live presentation as a "pre-flight test" — visibly jumps slides if run mid-service or against the wrong deck. Add a dry-run / presentation-targeting verification. *(Effort: S, but it's a live-embarrassment risk.)*
8. **Treat worship/song mode as a first-class, harder case.** It already exists (predictive ~1 s anticipation, non-linear chorus jumps, PDF arrangement parsing) and is **wall-to-wall repeated text** — the locality-prior problem in its purest form. If it's in scope, it should drive model selection (predictive timing) as much as sermon mode. *(Effort: M.)*

---

## 9. Open questions & freshness caveats

**Decision-critical questions to resolve before committing a direction:**
1. Is the problem the **model** or the **missing control loop**? (Experiments 1–2 answer this.)
2. What is the **cost of each failure mode**? A 2-s-late advance is mild and self-correcting; a wrong-index jump is jarring. The firing policy (eager vs threshold, confidence gates) depends entirely on this asymmetry.
3. Does deployment need to be **offline/on-prem**? Flaky sanctuary wifi during a live service is a hard dependency. If yes, this **forces the cascade** and demotes the entire cloud-model question — resolve it first.
4. Which **audio source + pre-processing** (lapel vs board send vs FOH matrix; denoise/AEC before streaming)? In a worship setting this moves accuracy **more than the LLM choice** and was under-weighted by the research.
5. Is **worship/song mode** in scope for this decision? (It changes model requirements.)

**Freshness caveats (couldn't fully verify / may change fast):**
- gpt-realtime-2 latency figures (1.12–2.33 s) are **unverified for v2** — carried over from an older model. Benchmark before relying on a sub-second budget.
- Vendor latency headlines (Pewbeam "80 ms", "sub-280 ms", TTFA numbers) measure audio synthesis or marketing, **not tool-emission-at-the-right-moment**, which is gated by the model's turn-taking. Honest end-to-end *fire* latency is unknown for **all** options.
- Open audio-native single-models are **built for short turns**; "stay silent for 90 min, fire rarely and precisely" is **largely undemonstrated** for them.
- Sliding-window context compression (Seeker sets `targetTokens=100000`) silently degrades the model's memory of its position over a long sermon — the compression↔position-memory interaction is **unexamined** and is a correctness risk, not a free lunch.
- ProPresenter endpoint paths/behaviors must be verified against the **venue's exact build** (the code's path shape disagrees with the documented API, and the 21.x line churned).

---

### Source anchors (representative; full set ~273)
OpenAI: developers.openai.com/api/docs/models/gpt-realtime-2, .../gpt-realtime-mini, openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api · Google: ai.google.dev/gemini-api/docs/{pricing,live-session,live-api/tools,deprecations} · AWS: aws.amazon.com/blogs/aws/introducing-amazon-nova-2-sonic… · Open models: github.com/QwenLM/Qwen3-Omni, mistral.ai/news/voxtral, github.com/stepfun-ai/Step-Audio2 · ASR: deepgram.com/learn/introducing-flux…, gradium.ai/content/stt-api-benchmark-2026… · Alignment: github.com/pymatchmaker/matchmaker, MaViLS (Interspeech 2024), "Do Slides Help?" (EMNLP 2025) · Frameworks: Pipecat, LiveKit Agents · ProPresenter: openapi.propresenter.com, github.com/alxpark/propresenter-mcp · Prior art: pewbeam.com, loghema, wordcast live, PromptSmart VoiceTrack.
