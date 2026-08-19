# Long-form production

The Projects workflow makes narration editable and reproducible without replacing the original `POST /api/jobs` studio. Speech is generated as persistent per-block takes; pauses are assembled later on CPU.

## Canonical editorial source

A project body contains only spoken paragraphs and standalone pauses:

```markdown
First, I invite you to simply listen.

[1s]

To let yourself be carried by the sound.

[0.7s]
```

`[Ns]` accepts an integer or up to three decimal places, must follow speech, and is limited to 60 seconds. Headings, lists, emphasis, arrows, `^`, `[pause: ...]`, T/S/D/R tags, and unknown bracket directives are rejected with a line diagnostic. Project title, language, voice, seed, and sampling settings are metadata outside the body.

There is deliberately no legacy parser in the render path. Convert an old file once and review the report:

```bash
qvl migrate-editorial old.md --output canonical.md --report migration.json
```

The command refuses path collisions and existing outputs. Re-run with `--overwrite`
only after inspecting the previous canonical file and transformation report; both
replacement files are staged before they are installed.

The migrator is best-effort. It removes known emphasis/direction glyphs and maps standalone `^` to `[1.2s]`; a human must review the result before creating a project.

## Durable pipeline

Each source save creates an immutable revision. Exact unchanged speech keeps its stable segment ID and selected take; a pause-only revision therefore needs no TTS. Punctuation or spoken-text edits invalidate only the affected selection.

For every generated take the Lab records:

- raw and non-destructively trimmed WAVs plus SHA-256;
- voice/reference/text/model provenance;
- a seed derived from `SHA-256(project seed, stable segment ID, attempt)` and reset before inference;
- resolved talker and subtalker sampling controls;
- technical, content-integrity, and windowed identity reports.

Automatic generation stops after three attempts by default. A passing take is selected automatically; an exhausted, unavailable, or ambiguous result becomes `needs_review`. Manual takes have no display ceiling. Selecting a non-passing take requires a durable reason.

The identity metric is intentionally advisory until a voice/language calibration exists for the exact scorer and frozen model SHA-256. It can expose a register jump through per-window scores, but an uncalibrated or provenance-mismatched number is not a valid rejection threshold. An operator can register evidence-backed median/minimum thresholds with `POST /api/voices/{voice_id}/identity-calibrations`; `validator`, `validator_model_sha256`, and a nonblank notes field are required. Only an exact provenance match can make identity outliers trigger retries.

## Preview and final assembly

Preview is button-triggered and CPU-only. It concatenates selected trimmed takes and inserts sample-exact zero-valued pauses compiled from the current source. Raw takes are never modified. Final uses the same timeline builder, writes an immutable JSON manifest, then transcribes the full WAV to check ordered coverage and the ending. A failed or unavailable final audit needs review; approval by override requires a reason and creates a new immutable assembly.

Project audio lives below `data/projects/<project_id>/`. Back up the SQLite database and the complete `data/projects/` tree together; either one alone is insufficient for recovery.

## Local validator environment

Qwen3-TTS 0.1.1 and Qwen3-ASR 0.0.6 pin different Transformers patch releases. They must not share one Python environment. Create the validator environment independently:

```bash
cd validator
uv sync
```

Then configure an installation-specific GPU admission command. A dedicated-GPU example is:

```dotenv
QVL_VALIDATOR_ENABLED=true
QVL_VALIDATOR_COMMAND=/absolute/repo/validator/.venv/bin/python /absolute/repo/validator/worker.py
QVL_QWEN_ASR_MODEL=/absolute/models/Qwen3-ASR-0.6B
QVL_QWEN_ALIGNER_MODEL=/absolute/models/Qwen3-ForcedAligner-0.6B
QVL_VALIDATOR_SPEAKER_MODEL=/absolute/models/spkrec-ecapa-voxceleb
QVL_VALIDATOR_SPEAKER_MODEL_SHA256=<lowercase SHA-256 of the frozen speaker model>
QVL_VALIDATOR_DEVICE=cuda:0
```

On a shared GPU, `QVL_VALIDATOR_COMMAND` must use the same operator-controlled serial admission policy as TTS. The configured `QVL_VALIDATOR_DEVICE` is sent to both ASR and ForcedAligner (and defaults to `QVL_DEVICE`); it is never hard-coded by the worker. The worker uses the official `Qwen3ASRModel.from_pretrained(..., forced_aligner=...)` and `transcribe(..., return_time_stamps=True)` API. A local SpeechBrain ECAPA model produces reference-vs-take speaker scores over overlapping voiced windows on CPU. Scores remain advisory until calibrated for the exact voice/language/scorer/model hash. Audio remains local. The server refuses to enable validation without explicit worker and speaker-model provenance.

The current content gate retries when WER exceeds 0.12, token coverage is below 0.90, or prefix/suffix coverage is below 0.80. These are operational defaults, not universal perceptual truth; changes require regression evidence.

## API sequence

1. `POST /api/projects` with canonical Markdown and project metadata.
2. `POST /api/projects/{id}/runs`; poll `GET /api/project-runs/{run_id}`.
3. Review `GET /api/projects/{id}/segments/{segment_id}/takes`.
4. Generate another take or select one; non-passing selection supplies `{ "override": true, "reason": "..." }`.
5. Edit pauses by saving a new source revision.
6. `POST /api/projects/{id}/preview` for CPU preview.
7. `POST /api/projects/{id}/assemblies` for final audio and transcript audit.
8. Download `/api/assemblies/{id}/download` and `/api/assemblies/{id}/manifest`.

## Deployment acceptance

The CUDA gate must use private, authorized assets outside Git and record model hashes, Git commit, settings, and output manifests. It includes all six advertised languages and the known Italian regression: the block containing “Senti delle voci?” must not jump identity or omit its following block, and the final expected words must be present. No CUDA smoke result is embedded in the public repository.
