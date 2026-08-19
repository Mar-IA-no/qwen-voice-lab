# Changelog — Qwen Voice Lab

## 2026-08-05 — Initial MVP

Created the standalone FastAPI and React product with local voice import, VoiceDesign samples, bilingual synthesis, controlled comparison, a serial queue, cancellation, metrics, a private archive and a deterministic CPU mock mode.

Qwen Base and VoiceDesign execution were validated on CUDA. Audio, models, local databases and generated renders remain outside Git.

## 2026-08-07 — Product hardening

Added safe host configuration, verifiable optional shared-GPU admission, private archive import, persistent recent-render recovery and direct WAV downloads.

VoiceDesign samples now require explicit, idempotent promotion before they enter the reusable catalog.

## 2026-08-07 — Per-block prosody runtime

Scored synthesis now resolves an independent T/S/D/R reference for every block when the selected identity has a complete local profile. `neutral` preserves the selected catalog reference; unsupported functional scores fail before queueing. Profile files remain private and are validated by SHA-256 at startup.

## 2026-08-07 — Public starter and repository hygiene

Added Amara Sol as an original synthetic CC0 starter voice with reproducible VoiceDesign metadata and an exact reference transcript. Fresh installations seed it automatically without embedding local catalog state.

Deployment-specific names, addresses, paths, inventories and private evaluation records were removed from the public tree. Shared-GPU integration is now fully configurable through environment variables.

## 2026-08-08 — Isolated shared-GPU worker

Added an optional shared-host adapter that can separate the persistent CPU web/API process from Qwen model residency. Dedicated installations continue to run Qwen directly by default; in adapter mode, worker preemption leaves the UI, catalog and downloads available.

Added explicit runtime states, preemption diagnostics, a relaunch cooldown, worker-window reuse, a systemd deployment template and CUDA-free scheduler contract tests. Dedicated-GPU execution remains backward compatible.

## 2026-08-09 — Authentication and worker lifecycle hardening

Added mandatory token authentication for remote bindings, an HttpOnly browser session, an unprivileged service deployment and narrowly validated admission/stop helpers for the local shared-GPU installation.

Running cancellation and API shutdown now stop the exact admitted transient unit. Readiness verifies cleanup configuration, local model paths and required modules before advertising a worker as configured; standby is visually distinct from an admitted GPU. Passive metadata replaces scheduler status invocation for manual cleanup. Automated coverage now includes retryable admission exits, ownership filtering and real non-CUDA `systemd-run` cleanup.

## 2026-08-09 — Human and agent manuals

Added a ten-page Spanish human guide in editable HTML and A4 landscape PDF, following the visual walkthrough pattern of the reference product guides while preserving the dark gold Voice Lab identity. Screenshots come from an isolated CPU mock catalog containing only the public starter and temporary synthetic examples; no local deployment, private identity or GPU was used.

Added a separate operational Markdown guide for agents covering discovery, authentication, voice lifecycle, authorized imports, scored synthesis, controlled comparison, job recovery, metrics, shared-worker states and retry boundaries. The repository README now links both manuals.


## 2026-08-10 — Public presentation and history curation

Rebuilt the repository landing page as a visual product document with a branded hero, capability matrices, architecture and lifecycle diagrams, interface captures, progressive technical disclosure and direct paths to the human and agent manuals.

Removed unpublished deployment-roadmap material and internal coordination language from the public tree. The public branch was republished from a curated root so removed references do not remain reachable through its ordinary commit history.

## 2026-08-16 — Multilingual Qwen language contract

Expanded synthesis, comparison, VoiceDesign and authorized-import contracts from Spanish and English to the six enabled Qwen languages: Spanish, English, Portuguese, French, Italian and German. The API advertises the complete set, translates stable language codes to Qwen's native labels, and the dashboard exposes the same choices.

Added contract and queue coverage for every enabled language. Existing Spanish and English scores, voices and prosody profiles remain compatible; multilingual support is a technical capability and still requires perceptual review for each identity and language.

Review hardening now enforces each functional prosody profile's declared languages while keeping neutral synthesis cross-lingual, exposes that readiness through the API and dashboard, clears mismatched VoiceDesign defaults on language changes, and keeps the six-language selector usable on mobile. Deployment guidance requires a verified pre-upgrade catalog snapshot when rollback may cross newly persisted multilingual voice or comparison records.

## 2026-08-17 — Six-language release validation

Merged the multilingual contribution after independent review, documentation synchronization and successful pull-request and post-merge CI. The human guide, PDF and public screenshots now present the same Spanish, English, Portuguese, French, Italian and German contract as the API and dashboard.

A controlled CUDA smoke used the bundled CC0 Amara Sol identity, one neutral segment per language, the same seed and semantically equivalent short texts. The six outputs measured 8.88–11.12 seconds. After the initial load, five warm renders averaged approximately 5.90 seconds of generation and RTF 0.622; peak allocated VRAM was approximately 4.7 GiB. A separate Portuguese request then completed through the persistent web queue, isolated worker and downloadable job path.

These observations validate the six-language technical pipeline on the tested setup. They do not establish pronunciation quality, naturalness or cross-language identity continuity; those remain perceptual listening questions.

## 2026-08-19 — Reproducible long-form production

Added an additive Projects workflow while preserving ordinary synthesis jobs. Canonical editorial sources now contain only spoken paragraphs and standalone `[Ns]` pauses; legacy cues require a one-shot migration tool. Stable revisions reuse approved speech across pause-only edits.

Long-form generation preserves raw and trimmed takes, deterministic per-segment seeds, resolved talker/subtalker sampling settings and technical/content/identity evidence. Automatic retries are bounded, manual takes remain unlimited, and non-passing selections require a reason. CPU previews and final WAVs share a sample-exact timeline builder; final output receives a whole-transcript audit and immutable manifest.

Qwen3-ASR and ForcedAligner validation run from a separate local environment because their dependency pin conflicts with Qwen3-TTS 0.1.1. Public tests use deterministic mock evidence; private CUDA and perceptual acceptance remain deployment responsibilities.

Independent review hardening added dirty-editor protection, atomic source creation and active-run exclusion, restart reconciliation, cross-revision take reuse, fail-closed take asset verification, ordered transcript edge gates, exact validator/model calibration provenance, configurable validator devices, and bounded validator process termination. The legacy migrator now preserves inline speech and paragraph boundaries while reporting every lossy or symbolic transformation.

A second adversarial review pass closed same-project stale-response rollback with monotonic request generations, eliminated take verification TOCTOU through authenticated byte snapshots, made terminal run/project persistence atomic, reconciled all stale `generating` projects, preserved exact migration cue lines and occurrence counts, rejected unmigrated slash cues, and added rollback-safe paired CLI publication. Frontend race coverage now runs in CI alongside the Python gate.

A final integration audit made revision publication and run creation one transactional exclusion boundary, so a stale run cannot restore an older source pointer and an active run cannot accept a concurrent revision. Graceful shutdown now records an interrupted long-form run as failed, cancels an admitted worker when available and guarantees main-engine cleanup. Finished assembly WAV and manifest responses now authenticate the exact served bytes against their durable SHA-256 and fail closed after mutation or path substitution. Dedicated regressions cover both transaction orderings, a simultaneous two-thread race, active-run shutdown and altered assembly assets.

Post-merge acceptance hardening closed the remaining operator-facing gaps. Content validation now checks every canonical block under a monotonic alignment and detects phrases leaked from the voice reference, so short omissions, reorderings and reference contamination cannot hide inside an acceptable global WER. WER/CER use compiled Levenshtein distance to keep the public 100,000-character request boundary computationally bounded. Validator shutdown can invoke an installation-specific cleanup command after terminating its process group.

Take records now preserve the exact trim threshold and padding, and authenticated raw or trimmed WAVs have direct attachment endpoints. The Projects dashboard exposes every persisted sampling control, transcript, WER/CER, block coverage, alignment, identity windows, hashes, trim provenance, playback and direct take downloads. The private acceptance runner records six-language TTS/ASR/alignment/identity evidence, asserts the Italian question, following block and final words, and leaves perceptual listening explicitly to a human.

The public gate completed with 93 tests passing and two privileged systemd tests skipped in the ordinary suite; those two cleanup tests also passed when run explicitly. Ruff, Vitest, TypeScript, the packaged frontend byte comparison, both dependency locks, package build and npm audit completed successfully. The served installation was restarted and its unauthenticated private-network UI, static bundle, API, validator configuration and standby worker state responded normally without loading a model.
