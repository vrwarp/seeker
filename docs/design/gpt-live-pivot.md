# Seeker × GPT-Live: The Full-Duplex Pivot

*Design brief, 2026-07-11. Companion research: [OpenAI Realtime API & gpt-live](../research/openai-realtime-and-gpt-live.md). Supersedes the "stay on Gemini" verdict of [full-duplex-suitability.md](../research/full-duplex-suitability.md) (2026-06-26) — the premise of that verdict ("no production full-duplex model has tool calling") expired the day gpt-live shipped.*

---

## Part I — What Seeker actually is (the contract, stripped of implementation)

Seeker is not a voice assistant. It is a **silent, continuous, sparse-firing actuator**:

1. **Input:** one continuous PCM stream from the soundboard (60–120 min, never pauses, never "ends a turn").
2. **State:** a known reference text — a sermon manuscript (`<exposition_blocks>` semantic + `<scripture_blocks>` verbatim) or a worship song's slides + arrangement (wall-to-wall verbatim lyrics, heavily repeated sections).
3. **Output:** `trigger_presentation_slide(index)` calls to ProPresenter — roughly 30–90 fires per sermon, and one fire every ~10–20 *seconds* during a song. Nothing else. No speech, no text a human sees.
4. **Timing contract:** sermon fires may lag the semantic boundary by ~1 s and nobody notices; **song fires must land *before* the line is sung** (`anticipation_seconds ≈ 1.0`) or the congregation is reading the wrong lyrics.
5. **Authority contract:** a human operator always wins (`evaluate_trigger` guards + reconcile loop + auto-yield). The agent proposes; the guards dispose.

Everything else in the repo — audio capture, WebSocket plumbing, manuscript XML, prompt templates, the operator server — exists to serve that contract.

### The parts that are model-agnostic and must survive the pivot

- `tracking.evaluate_trigger` — pure trigger-safety policy (bounds / no-op / locality / operator-yield).
- The reconcile loop (`daemon._reconcile_loop`) — observed-slide feedback, override detection, auto-yield.
- `ProPresenterClient` / `ProPresenterToolHandler` — the actuator.
- `manuscript_parser` — reference-text structuring.
- Audio capture (`audio_capture.py`, `file_audio.py`) — modulo sample rate.
- Operator HTTP server and kill switch.

### The part that is the problem

`gemini_session.py` — more precisely, the **interaction model** it is forced into.

---

## Part II — Why worship songs fail: the half-duplex diagnosis

Gemini Live is *bidirectional* (audio flows both ways over one WebSocket) but **half-duplex at the model level**: the model's cognition is organized around **turns**, and turns are manufactured by **server-side VAD segmentation**. That single architectural fact produces every symptom observed in production:

**1. VAD-gated cognition — the model may only think at segment boundaries.**
With `automaticActivityDetection`, incoming audio buffers into an "activity" until the server detects `silenceDurationMs` (Seeker sets an aggressive 50 ms) of quiet. Only then does the model reason over the segment and — maybe — emit a tool call. Between boundaries, Seeker is architecturally blind: audio accumulates but no decision can happen.

- **Sermon:** a preacher breathes. Sentence gaps of 300–2000 ms arrive constantly, so the turn machine gets decision points every few seconds. Sluggish and jittery, but workable. ("Works OK for sermons.")
- **Worship:** lead vocal + band + congregation + reverb tails ≈ **zero 50 ms silences for minutes at a stretch**. The VAD either never closes the segment (no decision points → no tool calls → the slide stalls for an entire verse) or closes it at acoustically arbitrary dips that have nothing to do with musical phrase boundaries. Segmentation noise becomes trigger noise. ("Completely fails for worship songs.")

**2. The response monopoly — thinking blocks listening.**
When a turn *does* close, the model must produce a model-turn — and on native-audio Gemini that response is **audio** (the `AMEN` hack exists precisely because text-only output is rejected on native-audio models). While generating, new input is barge-in: generation aborts or input handling degrades. Under continuous singing, *every* response overlaps fresh input, so the model lives in a permanent barge-in pathology: perpetually interrupted, perpetually re-arming. Turn overhead (VAD close → inference → response → re-arm) creates dead zones exactly where lyric lines are flying past at one slide per 10–20 s.

**3. Reactive gating is structurally late for songs.**
To put the *next* slide up ~1 s early, the decisive evidence is the current line's closing words. The half-duplex path is: line ends → wait for a silence that music never provides → segment closes → inference (300–800 ms) → tool call → REST trigger. Best case, the trigger lands *after* the next line has begun. `anticipation_seconds` in the prompt is a wish, not a mechanism — **a reactive turn gate cannot anticipate**.

**4. The escape hatches are welded shut on Gemini.**
- Disabling VAD (`automaticActivityDetection.disabled: true`) hangs after `ActivityEnd` and dies with a 1011 keepalive timeout (documented in [duplex-frontier-design.md](../research/duplex-frontier-design.md) §1A) — there is no manual-clock mode that works.
- Native-audio rejects `responseModalities: ["TEXT"]` — silence must be faked by discarding audio, and the `SILENT` scheduling narration-leak bug means the tool call occasionally gets *spoken*.
- No client-triggered "think now" primitive exists: the server's VAD is the only clock, and it is the wrong clock for music.

**5. The compounding tax.** Forced per-turn utterances burn output tokens ($12/M audio-out) 100% of which are discarded; 15-minute connection caps force `goAway` resumption churn mid-song; sliding-window compression quietly erodes the model's memory of its own position across a 90-minute service.

### The diagnosis in one sentence

> Seeker needs **continuous perception with act-anytime output**; Gemini Live offers **segmented perception with act-at-boundary output**, the segmenter is speech-VAD that music defeats, and every workaround is either broken (manual VAD), forbidden (text-only), or a hack (AMEN, 50 ms silence, discard-the-audio).

The June research reached the right architectural framing ("listen-anytime / act-anytime transducer with a silent side channel") but concluded no production model offered it. That conclusion had a shelf life.

---

## Part III — What changed on July 6–8, 2026

Two releases in one week invalidate the "stay on Gemini" verdict (details + citations in the [research briefing](../research/openai-realtime-and-gpt-live.md)):

1. **GPT-Live (July 8, ChatGPT; API "soon").** OpenAI's first production **full-duplex** model: *"continuously processes input while generating output"* and *"can make interaction decisions many times per second: whether to speak, continue listening, pause, interrupt, **or invoke a tool**."* That sentence is Seeker's missing primitive, verbatim: act-anytime tool invocation over an unsegmented stream, with intonation-level perception. The June verdict's premise — "no production full-duplex model has tool calling" — is dead.

2. **gpt-realtime-2.1 / 2.1-mini (July 6, Realtime API).** Still turn-*capable*, but the GA Realtime API gives Seeker the one thing Gemini refuses: **`turn_detection: null` is a documented, working mode.** Audio appends forever with no server segmentation; commits make audio visible *without* triggering responses; `response.create` runs inference only when the client asks; output can be text-only. The server's VAD stops being the clock. **We become the clock.**

The two-stage pivot, one architecture: **conductor mode** on gpt-realtime-2.1 today (Seeker owns the decision clock), **full-duplex mode** the day gpt-live's API ships (the model owns the clock). Everything else — audio plane, tool plane, safety plane, tracker — is identical across the two.

---

## Part IV — Brainstorm: how full duplex dissolves each failure

Mapping Part II's failure modes onto the new capabilities:

| # | Half-duplex failure (Gemini) | Conductor on gpt-realtime (now) | gpt-live full duplex (soon) |
|---|---|---|---|
| 1 | VAD-gated cognition: music never closes a segment → no decision points | **No VAD anywhere.** Commit every ~0.8 s; decide on *our* cadence (≤2.5 s, immediately at tracker-sensed boundaries) — decision density is a config knob, not an acoustic accident | Model decides *many times per second*, natively, no commits or ticks at all |
| 2 | Response monopoly: turn responses block listening; perpetual barge-in under song | Appends are fire-and-forget and never pause during a response; a decision is ≤64 text tokens ("HOLD" or one tool call) — there is nothing to interrupt | Perception and action are parallel streams by construction |
| 3 | Reactive gate structurally late for songs (~1 s anticipation impossible) | Three attacks: (a) decision ticks fire the *instant* the tracker hears the current slide's ending; (b) the prompt makes "last line of current slide → fire next NOW" the policy; (c) the tracker autofires unambiguous verbatim entries with zero model latency | The model hears the build-up, the drum fill, the breath — intonation-level perception can anticipate the way a human operator does |
| 4 | Escape hatches welded shut (manual VAD hangs; TEXT rejected; no client "think now") | All three exist and are documented: `turn_detection: null`, `output_modalities: ["text"]`, `response.create` | The hatches become the architecture |
| 5 | Forced-utterance tax (AMEN audio, narration leak), 15-min caps, opaque context | Text-only decisions; no audio out at all (leak impossible); 60-min sessions with client-held state → cheap rotation; `retention_ratio` truncation protects the prompt cache | Same, plus no tick overhead |

### The architecture: two planes, one arbiter, three clocks

```
                        ┌─────────────────────────────────────────────┐
   Soundboard PCM ────▶ │ stream_audio: resample → append (never stops)│
                        └──────────────┬──────────────────────────────┘
                                       │ continuous, unsegmented
                     ┌─────────────────▼──────────────────┐
                     │  OpenAI Realtime session (GA WS)    │
                     │  turn_detection: null, text-only    │
                     └───┬──────────────────────────┬─────┘
     transcription per   │                          │ tool calls
     commit (whisper) ───▼───┐             ┌────────▼─────────┐
   ┌──────────────────────────┐            │ trigger_presenta…│
   │ PositionTracker (lexical)│            └────────┬─────────┘
   │ • near_boundary → tick!  │                     │
   │ • verbatim entry → fire  │            ┌────────▼─────────┐
   └────────────┬─────────────┘            │ evaluate_trigger  │  ◀── operator
                │ autofire (song)          │ (bounds/no-op/    │      override,
                └─────────────────────────▶│  locality/yield)  │      reconcile
                                           └────────┬─────────┘      loop
                                                    ▼
                                              ProPresenter REST
```

- **Perception plane** — audio appends continuously; every commit yields a streaming transcript (`gpt-realtime-whisper`, $0.017/min) that feeds the deterministic `PositionTracker`. Enabling transcription also lets the server swap audio tokens for text in context — the perception plane *reduces* long-session cost.
- **Decision plane** — the realtime model, ticked by the **conductor**: commit cadence ~0.8 s; decision at latest every 2.5 s *while new audio exists* (dead air costs nothing), immediately (≥0.9 s spacing) when the tracker senses a boundary. A decision is one tool call or `HOLD`.
- **Arbiter** — every fire from every source (model tool call, tracker autofire, human) flows through the unchanged `evaluate_trigger` guards and reconcile loop. The model is *primary* (it hears music, semantics, delivery); the tracker is the *inner ear* (verbatim lyric confirmation + tick pacing); the operator is *sovereign*.
- **Three clocks, one seam** — `turn_mode: conductor` (ours), `server_vad` (A/B baseline that reproduces the Gemini failure for honest comparison), `full_duplex` (gpt-live: no commits, no ticks; the model pushes tool calls). The daemon cannot tell the difference: all brains emit the same events through the same `RealtimeBrain` protocol.

### The arrangement is a prior, not a path

Real services break written arrangements constantly: the leader repeats the chorus or bridge "one more time," cuts a verse, tags the ending, or the arrangement sheet is simply wrong. A tracker that walks a fixed expanded path goes blind at exactly those moments — so the tracker does not walk a path; it weighs **hypotheses**.

On every transcript update the tracker scores the tail against a full candidate set, each with a structural prior:

| Hypothesis | Meaning | Prior |
|---|---|---|
| `current` | still inside the on-screen slide (never proposed; rivals must beat it) | 1.00 |
| `arrangement_next` | the planned next slide | 1.00 |
| `repeat_section` | first slide of the section just sung ("one more time") | 0.92 |
| `section_jump` | first slide of any other section (wrong arrangement, called jumps, tags) | 0.80 |

Score = lexical evidence × prior. The tracker **autofires only on a clear win**: evidence ≥ 0.9 *and* a score margin ≥ 0.08 over every rival. The margin rule has a beautiful property for twin sections with identical words (Chorus 1 / Chorus 2, the classic killer): when one twin is arrangement-consistent, the prior separates them and the plan wins; when priors tie, the margin is zero and the tracker *refuses to guess* — it escalates to the model with the scored shortlist as a `[TRACKER]` hint ("lyrics match slide 4 'Chorus 1' (0.91) and slide 9 'Chorus 2' (0.91) — decide by ear"), because which repeat you're in is audible (band energy, key change, stripped-down turnaround) even when it isn't textual.

Three more layers close the loop:

- **Spoken cues.** "One more time!", "sing it again", "last time" almost always precede an ad-lib repeat. The transcript-side cue detector triggers an immediate decision tick plus a hint telling the model to expect the current section again — advance warning no arrangement can give. (The model also hears these cues natively; the hint makes them load-bearing.)
- **Self-healing anchoring.** `anchor()` prefers the upcoming occurrence of a fired slide but falls back to the nearest anywhere, so off-plan fires (from the model, tracker, or operator) re-position the plan pointer instead of derailing it; after the arrangement is exhausted, repeat and jump hypotheses keep tracking through tag endings indefinitely.
- **Prompt policy.** The worship prompt now states outright: the arrangement is a plan, the singing is the truth; repeats are normal; musical repeat cues (unresolved endings, drum turnarounds, key changes) predict them; never refuse a slide because "the arrangement says otherwise."

Division of labor at the moment of an ad-lib: the tracker catches verbatim, unambiguous re-entries instantly; the model resolves everything delivery-dependent; the operator remains sovereign; and content in the deck that matches nothing stays a HOLD. A missing-from-deck section (leader sings something with no slide) scores low everywhere and correctly holds the current slide.

### Ideas considered and where they landed

- **Tracker-only cascade (no realtime model)** — cheapest, most deterministic; but sermons are wall-to-wall paraphrase and songs still need judgment (ad-libbed repeats, "sing it again", spoken interludes). *Landed as:* the tracker is fused, not sovereign; it autofires only unambiguous verbatim entries in song mode and otherwise just paces ticks.
- **Dedicated transcription session** (second WS, `session.type: "transcription"`, `delay: minimal`) — tighter transcript latency than per-commit transcription, at trivial cost. *Landed as:* documented upgrade path; v1 keeps one socket for simplicity since commits already run at ~0.8 s.
- **Out-of-band audit probes** (`response.create {conversation: "none", input: [item_references…]}`) — parallel "where are we, really?" checks that never perturb the decision stream. *Landed as:* future hardening hook (the API explicitly supports parallel out-of-band responses); not needed for v1.
- **Forcing the tool every tick** (`tool_choice: required` + a `hold()` function) — makes every decision structured. *Rejected for v1:* `HOLD` as text is cheaper (no arguments to stream) and `tool_choice: auto` lets the model stay silent; revisit if HOLD discipline drifts.
- **Dual-fed A/B rotation** (open session B, feed both, cut over) — zero-gap rotation. *Landed as:* documented enhancement; v1 rotates via reconnect at a quiet moment (~50 min), which costs a ~1–2 s blind spot once per service, mitigated by client-held state re-priming.
- **Prosodic boundary sensor** (June frontier doc's top bet) — still valid and now *cheaper to justify*: gpt-live's intonation-level perception may deliver it for free; test before building it locally.
- **MCP-wrapped ProPresenter** — the API can call MCP servers directly. *Rejected:* Seeker's guards (`evaluate_trigger`) must sit between model and projector; server-side tool execution would bypass the safety plane.

---

## Part V — What was implemented (code map)

| Piece | File | Role |
|---|---|---|
| Brain protocol | `seeker/brain.py` | `RealtimeBrain` + `ToolHandler` — daemon is provider-blind |
| **OpenAI session** | `seeker/openai_session.py` | GA WebSocket lifecycle; conductor (commit/tick cadence, boundary ticks); tool dispatch + `function_call_output`; streaming-transcription fusion; tracker autofire; state-item injection; rotation ahead of the 60-min cap; reconnect with client-held re-priming; `LinearResampler` (16→24 kHz); usage stats |
| Position tracker | `seeker/position_tracker.py` | Arrangement→path expansion; suffix/prefix entry scoring; `near_boundary`; `propose` with ambiguity guard; `anchor` with repeat-aware locality |
| Config | `seeker/config.py` | `provider` switch (default **openai**), `OpenAIConfig` (model, turn_mode, cadences, transcription, truncation, rotation, autofire) |
| Prompts | `prompts/v2.0_worship_openai.txt`, `prompts/v2.0_sermon_openai.txt` | Written for the decision-tick regime: tool call or `HOLD`; fire-early policy for songs; scripture override rules preserved |
| Tools | `seeker/prompt_builder.py` | OpenAI-schema tool declarations (lowercase JSON Schema) alongside Gemini's |
| Daemon | `seeker/daemon.py` | Provider-selected brain; tracker construction (song: slides+arrangement); brain-supervised task set; operator overrides now notify the brain |
| Legacy | `seeker/gemini_session.py` | Unchanged behavior, now conforms to the brain protocol |
| Tests | `tests/test_openai_session.py`, `tests/test_position_tracker.py`, `tests/test_config.py` | 35 new tests: resampler, setup payloads, tool dispatch/dedupe, conductor guards, autofire fusion, override re-anchoring, config |

**Decision flow, song mode (today):** singers finish the last line of slide N → (a) tracker hears the ending (`near_boundary`) → boundary tick → model (which has heard the *audio*, including the band) fires N+1; or (b) singers are already into N+1's first words → tracker autofires N+1 directly (zero model latency); whichever lands first — both routed through `evaluate_trigger`, the loser rejected as `noop_already_current`. `[STATE]` items keep the model's world model synced to the actual deck, including operator overrides.

**Decision flow, gpt-live (later):** identical, minus ticks: set `turn_mode: full_duplex`, point `model` at gpt-live, and the model pushes `trigger_presentation_slide` whenever its per-frame decision says so. The tracker remains as the deterministic seatbelt and the [STATE] channel remains the ground truth feed.

---

## Part VI — The gpt-live upgrade plan (day the API ships)

1. **Point and flip:** `openai.model: gpt-live-…`, `turn_mode: full_duplex`. If OpenAI ships a new session type or event names, the changes are confined to `openai_session.py` (`_dispatch` + `send_setup`); the daemon, guards, tracker, prompts, and tests above the protocol are untouched.
2. **Day-one test matrix (in priority order):**
   - **Music input**: no coverage of singing/music input exists in any GPT-Live reporting — this is the load-bearing unknown. Feed recorded worship (board mix + vocals-only stems); measure fire-offset vs gold timings.
   - Tool-call latency distribution mid-stream (no coverage of API latency exists yet; "sub-200 ms" is single-outlet rumor).
   - Silence discipline over 60+ min (full-duplex chat models are trained to *talk*; Seeker needs the "silent worker" posture — the prompt + text-only modality should enforce it, verify empirically).
   - Backchannel suppression (it backchannels in ChatGPT; confirm `output_modalities: ["text"]` or equivalent kills this).
3. **Keep the conductor as the fallback** — `turn_mode` is per-service config; a worship set can run full_duplex while sermons stay conductor (or vice versa) during the validation window.
4. **Watch items:** GPT-Live delegation (background GPT-5.5 calls) as a possible future "deep reasoning on demand" hook for ambiguous sermon moments; whether session caps change; pricing.

---

## Part VII — Cost, risks, open questions

**Cost (90-min service, from the research briefing):** conductor on 2.1 ≈ **$14–17** with healthy prompt caching (cached audio input $0.40/M vs $32/M — the `retention_ratio` truncation config exists to protect exactly this); on 2.1-mini ≈ **$6**; perception plane $1.53 flat. The uncached naive design would be ~$346 — cache-consciousness is not optional, which is why truncation is configured server-side rather than via client-side item deletion (deletes bust the prefix).

**Risks, honestly:**
- *Whisper on singing* — melisma and band bleed degrade open transcription; Seeker only needs fuzzy matching against known lyrics, and the tracker's thresholds (0.82 propose / 0.9 autofire) demand strong evidence. If the transcript is trash for a passage, the system degrades to model-only — which still beats Gemini because decisions keep happening.
- *Realtime-model music comprehension* — gpt-realtime-2.1 is speech-tuned too; whether it tracks sung lyrics better than Gemini's VAD-gated path is an empirical bet (the mechanism — decisions keep flowing regardless of segmentability — is the part we control, and it removes the *architectural* failure).
- *TPM* — dense polling needs Tier 3+ (≈120k TPM at 5 s cadence); adaptive cadence and mini both reduce this.
- *Rotation blind spot* — ~1–2 s once per ~50 min; mitigate by rotating during instrumental/tangent moments (tracker knows), or adopt dual-fed rotation later.
- *60-min cap could surprise* — the exact kill behavior is undocumented; the reconnect path treats any close as recoverable.

**Open questions to instrument (unchanged from the June docs, still true):** fire-offset/false-fire metrics on recorded services; the cost asymmetry between late and wrong; whether the audio front-end (board send quality) still dominates model choice. The eval harness remains the highest-ROI unbuilt tool — the conductor's `stats` block (ticks, fires by source, cached-token ratio) is the first data feed for it.

**Bottom line:** the pivot stops fighting the model's interaction pattern and starts owning it. Today that means Seeker conducts a turn-capable model at whatever cadence worship demands; the day gpt-live's API opens, the same architecture lets a genuinely full-duplex model conduct itself — and the only thing Seeker gives up is the metronome.
