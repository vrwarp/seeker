# Is Full-Duplex Worth It for Seeker? — A Decision Briefing

*As of 2026-06-26. Grounded in Seeker's actual code (`seeker/gemini_session.py`, `seeker/prompt_builder.py`) and the 2026 full-duplex / realtime-tool-calling literature.*

## 1. Verdict

**Full-duplex is a distraction for Seeker, not a win. Stay on the half-duplex realtime stack you already run and invest in firing precision, the silent-path bug, and long-session robustness instead.**

Full-duplex is *defined* by one capability — generating speech while still listening, with graceful barge-in — and Seeker never speaks, so 100% of that defining capability is dead weight. The thing Seeker actually needs (gap-free continuous listening + reliable, low-latency, silent structured tool calls) is a *streaming-tool-calling* property that is orthogonal to duplex-ness and is already delivered by your incumbent: `gemini_session.py:264` sets `scheduling:SILENT` and `prompt_builder.py:29` sets `behavior:NON_BLOCKING` on Gemini 2.5 Flash native-audio. The only true-full-duplex model that can fire silent structured tool calls (DuplexSLA) is a May-2026 research preprint with no deployable path and ~85.6% tool accuracy — *below* the half-duplex realtime leaders (~90% class). Adopting full-duplex would trade a working stack for research risk and weaker tool calling, which is Seeker's entire job.

## 2. What "full-duplex" actually means — and the distinction that matters

The 2026 survey *From Turn-Taking to Synchronous Dialogue* (arXiv 2509.14515) lays out a clean spectrum on the **duplex axis** (audio I/O concurrency):

- **Half-duplex / turn-based** — strict listen→think→speak cycles. Classic ASR→LLM→TTS cascades, and *every production realtime API*.
- **Pseudo-full-duplex** — time-division chunk-swapping that *feels* continuous but is fast turn-swapping (SyncLLM, OmniFlatten).
- **True full-duplex (TFD)** — the user channel and model channel modeled as parallel token streams at a fixed frame rate, every timestep (Moshi at 12.5 Hz; dGSLM dual-tower; Hertz-dev at 8 Hz).

The crucial unbundling: **full-duplex ≠ "always-listening."** Two separable properties get conflated:

| Property | What it solves | Does Seeker benefit? |
|---|---|---|
| **Simultaneous talk + listen, barge-in** (the *defining* full-duplex feature) | Managing the model's *own voice* overlapping the user's | **No.** Seeker has zero voices. Barge-in solves "two mouths at once"; Seeker has none. |
| **Continuous gap-free listening + act mid-stream** (separable, also present in some half-duplex stacks) | Firing an action before a turn would end | **Yes** — but Seeker *already has this* via NON_BLOCKING tools + aggressive VAD. |

**Which APIs Seeker could use are truly full-duplex?** None of the production ones. The "full-duplex" label on **Gemini Live, OpenAI gpt-realtime, Amazon Nova 2 Sonic, and ElevenLabs** is *marketing for barge-in on top of a VAD turn-detection loop* — architecturally half-duplex. Genuine TFD (parallel audio streams every frame) lives almost exclusively in open/research models: Moshi, Hertz-dev, dGSLM, SALMONN-omni, and the research-only DuplexSLA. **This duplex axis is orthogonal to everything Seeker cares about.**

## 3. The tool-calling reality — the decisive issue

Seeker's whole job is *firing the right slide index at the right instant*. So the gate is: **can the model emit reliable structured tool calls?** This is precisely where true-full-duplex models fail and half-duplex realtime APIs win.

- **Most true-full-duplex models have ZERO function calling.** Moshi, Hertz-dev, dGSLM, Sesame CSM are speech-to-speech (or pure TTS) systems with no structured channel. Several **cannot emit text at all.** Industry guidance is blunt: "S2S models cannot call external tools mid-generation"; tool/RAG workflows belong on cascaded or realtime-API stacks. Moshi's "Inner Monologue" text stream is an internal scratchpad — repurposing it as a tool channel is *theoretically possible but unbuilt*, requiring original ML training plus a fragile free-text-to-intent parser.
- **The half-duplex realtime APIs lead decisively.** GPT-Realtime tops Full-Duplex-Bench-v3 tool use (Pass@1 0.60, Tool-Selection F1 0.876, Argument Accuracy 0.68). Gemini 3.1 Flash Live leads ComplexFuncBench Audio at ~90.8%. Nova 2 Sonic ships async tool calling + up to 1M-token context.
- **The single full-duplex model that closes the gap is research-only.** DuplexSLA (arXiv 2605.20755, May 2026, on a Step-Audio 2 mini 7B backbone) adds a dedicated, rate-limited **action channel** (≤10 text tokens / 160 ms chunk) emitting JSON tool calls as `<|toolcall_begin|>{...}<|toolcall_end|>` *without* taking the speaking floor — the existence proof that silent structured tool calls on a full-duplex architecture are real. But: no confirmed public checkpoint, self-hosted 7B, tuned for conversational turn-taking (not 90-min silent observation), ~85.6% accuracy. **It does not beat what Seeker already runs.**

**The on-the-nose evidence:** Full-Duplex-Bench-v3 documented a "silent worker" phenomenon — Gemini Live produced *no speech* in 22% of tool-use scenarios, and **86% of those silent cases still fired the tool correctly.** The benchmark frames this as a *chatbot failure mode*. For Seeker it is *exactly the desired behavior*, demonstrated in the wild on the incumbent family. (It also explains the known `SILENT` narration-leak bug: the speech path and tool path are decoupled, and current models suppress the speech path imperfectly.)

## 4. Candidate models

| Model | Truly full-duplex? | Tool calling | Can run silent? | Latency | On-prem? | Maturity | Seeker fit |
|---|---|---|---|---|---|---|---|
| **Gemini 2.5 Flash native-audio** *(incumbent)* | No (half-duplex + barge-in) | **Native; NON_BLOCKING + scheduling=SILENT** — purpose-built for fire-and-forget | Yes (in use: `AUDIO`-discard + SILENT); audio-only output, narration-leak bug | Sub-second fire | No (cloud) | **Production** | **Best available; already in use.** Don't switch. |
| **Gemini 3.1 Flash Live** | No | Best audio accuracy (~90.8%) **but SYNCHRONOUS-ONLY — no NON_BLOCKING/SILENT** | No clean async-silent path | ~200 ms class | No | Production | **Do NOT "upgrade."** Breaks the fire-and-forget design. |
| **OpenAI gpt-realtime / -2** | No (half-duplex + semantic VAD) | Native; **best raw accuracy in 2026 benchmarks**; async tools | **Yes — clean TEXT output** + `create_response=false` (no throwaway audio) | ~250–500 ms | No | Production | **Strongest alternative / 2nd source.** Cleaner silent path than Gemini. |
| **Amazon Nova 2 Sonic** (GA Dec 2025) | No (turn-based + VAD) | Native **async** tool calling; strong BFCL claim | Plausible (cross-modal text); not first-class — unverified | Sub-500 ms | No (AWS) | Production | Viable if on AWS; **1M context** great for long sermons. No duplex advantage. |
| **Step-Audio 2 mini** (Apache-2.0, 8B) | No (turn-based) | Native, audio-native, benchmarked | Plausible — *unverified it can fire without speech* | ~24 GB single GPU | **Yes** | Released weights | Best on-prem *omni* option; verify silent-fire empirically. |
| **Streaming-ASR → text-LLM cascade** (Kyutai STT / Unmute, Whisper-streaming) | N/A (not S2S) | **Best-in-class** (full text-LLM tool calling); TTS simply omitted | **Trivially** — no speech stage, no always-respond bias | STT ~300 ms + LLM decision | **Yes** | Production | **Dark-horse; most architecturally honest.** Deserves an A/B over full-duplex. |
| **DuplexSLA** (Step-Audio 2 backbone, 7B) | **Yes** | **Native action channel** (only TFD model with it); ~85.6% | **Yes** (action channel + `<vad_silence>` anchors) — but a dialogue posture, not a silent-observer mode | ~0.64 s tool delay | Yes (no confirmed weights) | **Research-only** | Conceptually best-matched; **not deployable.** Watch only. |
| **Moshi** (Kyutai) | **Yes** (canonical) | **None.** Inner-monologue ≠ tool API | No native silent mode; "trigger-happy" | ~200 ms audio | Yes (~24 GB) | Prod as chatbot | **Poor.** No tool calling = non-starter. |
| **Hertz-dev / dGSLM / Sesame CSM** | Yes (Hertz/dGSLM); CSM is TTS | **None** (base/textless/synthesis) | N/A | — | Yes | Research/component | **None.** Wrong category entirely. |
| **SALMONN-omni / Freeze-Omni / VITA-1.5** | TFD / engineered | None native | Conceptually close (control tokens / state head) | Real-time | Yes | Research | **Idea sources, not deployable.** Freeze-Omni's frozen-LLM + per-chunk state head is the right *template* if Seeker ever self-hosts — note it doesn't need the speaking side. |
| **ElevenLabs / Cartesia / Pipecat / LiveKit** | No (turn-taking / TTS-STT / frameworks) | Inherited from your LLM | Off-label / config | — | Some | Production | Plumbing or TTS, not a brain. Skip. |

## 5. Where full-duplex could help Seeker — honestly

**Where it does NOT help (the bulk of it):**
- Barge-in, overlap handling, backchannels, floor-yielding, turn-taking naturalness — all conversational UX features for a model that talks. Inapplicable.
- Conversational audio latency (Moshi ~200 ms, Hertz ~120 ms) — these are *speech round-trip* numbers, not tool-fire latency. Seeker's perceived latency is gated by *semantic decision time*, identical across duplex modes.
- The latency argument collapses entirely: full-duplex would only shave turn-*finalization* delay (a few hundred ms), and Seeker already runs `silenceDurationMs:50` (gemini_session.py:117) — far below Google's recommended 500–800 ms — so it **already fires mid-monologue.**

**The narrow places it *could* matter (none decisive today):**
- **Mid-monologue firing with no VAD gate** — a pure turn-gated model only "decides" at end-of-turn, so a 90-second un-paused monologue could starve it of decision points. But this is achievable on the existing half-duplex stack via manual VAD (`activityStart`/`activityEnd`) or `turn_detection=null` + manual triggering. **Control-plane tweak, not a model switch.**
- **On-prem dialogue + action in one model** — only relevant *if* Seeker ever needs to both speak and act locally. Today the cascade wins this scenario, not a full-duplex model.
- **Future worship "call-and-response"** — if Seeker ever needs to overlap acoustic event detection (audience response, worship transition) *while* acting on a separate channel in a way a turn-gated scheduler genuinely cannot — that is a real concurrent-listen-and-act need. Not in scope now.

**Active risk that makes full-duplex worse, not neutral:** full-duplex dialogue models are documented as "trigger-happy," trained to *always respond* and to mistake backchannels for response cues — the exact opposite of "stay silent 90 minutes, fire rarely." The architecture would fight the use case.

## 6. Conditions that would flip the verdict

Full-duplex becomes worth adopting only if **all of these mature simultaneously:**

1. **A DuplexSLA-style action-channel model ships open weights + a first-class, validated mostly-silent-observer mode + ≥95% tool-call accuracy on disfluent single-speaker audio over 60–90 min sessions** — i.e., it beats the incumbent on *reliability*, not just latency.
2. **Seeker's requirements change so it must actually SPEAK** — give the operator spoken cues, verbally confirm an override, talk back to a presenter — at which point barge-in/overlap stop being dead weight.
3. **A true concurrent listen-and-act-on-two-channels need emerges** that a turn-gated scheduler genuinely cannot express (e.g., detect a verbal cue *while* firing).
4. **A hard on-prem / zero-egress mandate** *and* the only option meeting latency + tool-reliability happens to be a full-duplex action-channel model (note: today the **cascade** wins this, not full-duplex).
5. **Empirical proof** that frame-level token emission materially cuts mid-monologue fire latency for long un-paused speech in a way manual-VAD chunking on the existing stack cannot — currently unproven, since it's a control-plane tweak.

## 7. Recommendation & next steps

**Do not migrate Seeker to a full-duplex model.** The duplex axis is a capability mismatch; the tool-calling axis (which Seeker lives or dies on) favors the half-duplex stack you already run. Prioritized:

1. **Stay on Gemini 2.5 Flash native-audio.** It is the *only* Gemini tier with true async (NON_BLOCKING + scheduling=SILENT) tools. **Validate before any version bump:** confirm Gemini 3.1 Flash Live really regresses to synchronous-only tools (the freshness check and your own `config.py:48` flag this; reports indicate it does). A 3.1 "upgrade" for its 90.8% accuracy would *break* the fire-and-forget design. This is the single most decision-gating fact — verify it definitively.

2. **Highest-ROI near-term fix — kill the narration-leak class of bug structurally.** Your live path is `responseModalities=["AUDIO"]` + discard (gemini_session.py:100); native-audio cannot emit text-only, so the `SILENT` narration-leak is intrinsic. **Cheap experiment:** spike **OpenAI gpt-realtime with TEXT output + `create_response=false` + `turn_detection=null`** as a silent listener. It eliminates throwaway audio and the narration-leak entirely, and brings best-in-class raw tool accuracy. Higher ROI than any full-duplex or version migration.

3. **A/B the cascade dark-horse before anything full-duplex.** Streaming ASR → strong text-LLM (native tool calling) → *no TTS*. It removes the always-respond bias, maximizes tool reliability + reasoning over the manuscript XML, and enables on-prem. The sharpest open question: *does any architecture beat this on correct-index fire rate, false-fire rate during tangents/self-corrections, and on-prem feasibility?* If not, full-duplex shouldn't be in the conversation at all.

4. **Treat firing *precision*, not async-ness, as the real frontier.** Pre-emptive mid-utterance firing trades latency for self-correction safety — and Full-Duplex-Bench-v3 shows early calls "lock in" stale parameters when a speaker self-corrects (even GPT-Realtime fails self-correction often). A preacher who rephrases mid-sentence is exactly this case. Given Seeker's false-fire-during-tangent cost is far worse than a slightly-late fire, consider firing *later* (at a confirmed semantic boundary), not earlier — making the SHANKS/speculative "fire before turn-end" thread a likely *anti-feature* here. This interacts directly with your existing bounds/no-op/locality guards and the reconcile loop (`tracking.py`, `daemon.py`).

5. **Investigate the under-examined correctness risks that dwarf duplex-ness:** (a) whether sliding-window compression (`targetTokens=100000`, gemini_session.py:123) silently erodes the model's memory of its slide position over a long sermon — likely a bigger threat to fire accuracy than model choice; (b) multi-speaker/diarization robustness on real church-PA audio (preacher + worship + congregation + music bleed) — full-duplex models are trained on clean 2-party conversation and are *untested* on this; (c) actual token cost over a 60–90 min continuous-listen duty cycle, which matters for the cascade-vs-native-audio call far more than any full-duplex consideration.

6. **Watch, don't adopt:** DuplexSLA is the only research direction that aligns with Seeker's silent-actor pattern. If it ships open weights with a validated listen-only mode and ≥95% tool accuracy, revisit. Until then it is a signal, not a tool.

**Bottom line:** the right framing for Seeker is "never stops listening + acts mid-stream + reliable silent structured tool calls" — which your existing half-duplex Gemini Live setup already provides, and which gpt-realtime or a cascade provide as credible alternatives. Future gains come from firing precision, the silent-path hardening, and long-session robustness — *not* from switching to a Moshi-class architecture.

*Note on freshness/uncertainty: the Gemini 3.1 synchronous-only-tools claim, DuplexSLA's self-reported 85.6%/0.64 s figures, and several 2026 benchmark numbers are reported-not-independently-verified; the benchmark tasks are disfluent multi-step dialogue, not Seeker's single-speaker monologue-tracking, so transferability of exact percentages is uncertain. The architectural conclusion (full-duplex = mismatch) is robust regardless.*
