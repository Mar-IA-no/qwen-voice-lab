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

`[Ns]` accepts an integer or up to three decimal places and must follow speech. There is no editorial 60-second cap; available disk space and the output WAV format still limit assembly length. Headings, lists, emphasis, arrows, `^`, `[pause: ...]`, T/S/D/R tags, and unknown bracket directives are rejected with a line diagnostic. Project title, language, voice, seed, and sampling settings are metadata outside the body.

There is deliberately no legacy parser in the render path. Convert an old file once and review the report:

```bash
qvl migrate-editorial old.md --output canonical.md --report migration.json
```

The command refuses path collisions and existing outputs. Re-run with `--overwrite`
only after inspecting the previous canonical file and transformation report. Both
replacement files are staged before installation, and the previous pair is restored
if either install fails, so source and report cannot describe different generations.

The migrator is best-effort. It removes known emphasis/direction glyphs and maps standalone `^` to `[1.2s]`; every occurrence records its actual source line in the report. A human must review the result before creating a project. Unmigrated whitespace-delimited `/` cues are rejected by the strict compiler; URLs, fractions, and embedded slashes remain ordinary speech.

## Durable pipeline

Each source save creates an immutable revision. Exact unchanged speech keeps its stable segment ID and selected take; a pause-only revision therefore needs no TTS. Punctuation or spoken-text edits invalidate only the affected selection.

The visual editor uses ordered blocks with stable IDs. It can change spoken text, the silence after each block, an initial silence, a general speech speed, and an optional speed for one block. Speeds from 0.5 to 2 change speech tempo without changing pitch or authored pauses. Saving or restoring creates a new revision, including its selected takes and Beacon settings. Importing Markdown remains available for older projects. A changed text block needs a new take; changing timing reuses the existing audio.

Saving a revision and creating a run are mutually exclusive inside SQLite. A queued or running project rejects a new revision, while a run whose source pointer became stale is rejected before it enters the queue. Shutdown marks an interrupted active run failed before GPU-worker and model cleanup continue; startup remains the recovery floor for an abrupt process loss.

For every generated take the Lab records:

- raw and non-destructively trimmed WAVs plus SHA-256, threshold and padding used;
- voice/reference/text/model provenance;
- a seed derived from `SHA-256(project seed, stable segment ID, attempt)` and reset before inference;
- resolved talker and subtalker sampling controls;
- technical, content-integrity, and windowed identity reports.

Automatic generation stops after three attempts by default. A passing take is selected automatically; an exhausted, unavailable, or ambiguous result becomes `needs_review`. Manual takes have no display ceiling. Selecting a non-passing take requires a durable reason.

The identity metric is intentionally advisory until a voice/language calibration exists for the exact scorer and frozen model SHA-256. The Projects dashboard exposes the transcript, content metrics, block coverage, identity median/minimum and every window score so an internal register jump is reviewable. An uncalibrated or provenance-mismatched number is not a valid rejection threshold. An operator can register evidence-backed median/minimum thresholds with `POST /api/voices/{voice_id}/identity-calibrations`; `validator`, `validator_model_sha256`, and a nonblank notes field are required. Only an exact provenance match can make identity outliers trigger retries.

## Preview and final assembly

Preview is button-triggered and CPU-only. It concatenates selected trimmed takes and inserts sample-exact zero-valued pauses compiled from the current source. Raw takes are never modified. Final uses the same timeline builder, writes an immutable JSON manifest, then transcribes the full WAV to check ordered coverage and the ending. A failed or unavailable final audit needs review; approval by override requires a reason and creates a new immutable assembly.

The preview can target a complete revision or one block. The full preview downloads as a ZIP with the speech WAV, a timing manifest, and the score in Markdown and JSON. A partial preview cannot be downloaded as a complete score. Beacon is a separate local track in the browser, with shared play/pause/seek, an adjustable offset and volume, a three-second fade-in, and a short loop crossfade. It is not mixed into the exported speech WAV.

The editor can mark the current saved revision for handoff without requiring every block to have a take. This snapshots the revision and its selected take IDs. The handoff ZIP contains the score, a manifest naming any missing blocks, and the selected raw and trimmed WAV files. Later revisions do not change the marked handoff. Marking or exporting does not publish anything to Psicopompo.

Each take and finished assembly asset is opened once with no-follow semantics and copied into an authenticated in-memory snapshot. Its SHA-256 is verified over those exact bytes, and decoding or HTTP serving consumes that same snapshot. A mutable path is never reopened after verification, and altered WAV or manifest bytes fail closed.

Project audio lives below `data/projects/<project_id>/`. Back up the SQLite database and the complete `data/projects/` tree together; either one alone is insufficient for recovery.

An authorized local bundle can be imported with `qvl import-project-bundle BUNDLE --data-dir DATA --confirm-authorized`. The command verifies every file and hash before writing, preserves original recordings, and marks imported takes for human review. Reimporting an identical bundle leaves edited projects intact. The importer shares an immutable Beacon asset across imported projects on filesystems that support hardlinks.

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
QVL_VALIDATOR_STOP_COMMAND=
QVL_QWEN_ASR_MODEL=/absolute/models/Qwen3-ASR-0.6B
QVL_QWEN_ALIGNER_MODEL=/absolute/models/Qwen3-ForcedAligner-0.6B
QVL_VALIDATOR_SPEAKER_MODEL=/absolute/models/spkrec-ecapa-voxceleb
QVL_VALIDATOR_SPEAKER_MODEL_SHA256=<lowercase SHA-256 of the frozen speaker model>
QVL_VALIDATOR_DEVICE=cuda:0
```

On a shared GPU, `QVL_VALIDATOR_COMMAND` must use the same operator-controlled serial admission policy as TTS. Configure `QVL_VALIDATOR_STOP_COMMAND` when that admission process creates a detached unit, so timeout and API shutdown release the validator scope as well as its controller. The configured `QVL_VALIDATOR_DEVICE` is sent to both ASR and ForcedAligner (and defaults to `QVL_DEVICE`); it is never hard-coded by the worker. The worker uses the official `Qwen3ASRModel.from_pretrained(..., forced_aligner=...)` and `transcribe(..., return_time_stamps=True)` API. A local SpeechBrain ECAPA model produces reference-vs-take speaker scores over overlapping voiced windows on CPU. Scores remain advisory until calibrated for the exact voice/language/scorer/model hash. Audio remains local. The server refuses to enable validation without explicit worker and speaker-model provenance.

The current content gate retries when WER exceeds 0.12, token coverage is below 0.90, prefix/suffix coverage is below 0.80, or any canonical spoken block has less than 0.80 exact-token coverage under one monotonic alignment. Block-local coverage prevents a short missing or reordered paragraph from disappearing inside an acceptable global WER. A three-word phrase copied from the voice reference but absent from the requested text is also retryable reference leakage. These are operational defaults, not universal perceptual truth; changes require regression evidence.

## API sequence

1. `POST /api/projects` with canonical Markdown or structured blocks and project metadata.
2. `POST /api/projects/{id}/runs`; poll `GET /api/project-runs/{run_id}`.
3. Review `GET /api/projects/{id}/segments/{segment_id}/takes`; download the authenticated trimmed or raw take from `/api/takes/{take_id}/download[?raw=true]`.
4. Generate another take or select one; non-passing selection supplies `{ "override": true, "reason": "..." }`.
5. Edit text, pauses, or speech speed with `POST /api/projects/{id}/revisions`; restore a prior revision with `POST /api/projects/{id}/restore`.
6. `POST /api/projects/{id}/preview` for a CPU preview of the full score or one block. A full preview can be downloaded as a ZIP.
7. `POST /api/projects/{id}/assemblies` for final audio and transcript audit.
8. Download `/api/assemblies/{id}/download` and `/api/assemblies/{id}/manifest`.

## Deployment acceptance

The CUDA gate must use private, authorized assets outside Git and record model hashes, Git commit, settings, and output manifests. It includes all six advertised languages and the known Italian regression: the block containing “Senti delle voci?” must not jump identity or omit its following block, and the final expected words must be present. No CUDA smoke result is embedded in the public repository.

`scripts/run_private_longform_acceptance.py` exercises that technical gate against a live installation and writes WAVs, manifests, ASR/identity evidence and hashes only to the explicitly supplied non-Git directory. Its report intentionally leaves `human_listening_review` pending: a human listener, not the script, owns perceptual acceptance.

## Edit an existing locution without generation

When an operator configures a pinned collection, **Partitura** opens its
principal route as one script. Older projects remain in **Otros trabajos**,
with an explicit control to include the collection's source projects.
Existing project recordings and revisions remain unchanged.

1. Read the script and use **Ir a una etapa** or the voice/silence diagram to
   reach a block. Variants are listed separately and are added deliberately.
2. Change seconds of initial silence or a block's post-pause. Move, remove or
   add existing blocks. **Deshacer** and **Rehacer** operate on the local draft.
3. **Guardar versión** persists a new composition. A concurrent save preserves
   your draft and offers JSON export or reloading the saved version. Unsaved
   drafts are not automatically recovered after closing the browser.
4. **Escuchar todo**, select checkboxes and **Escuchar selección**, or use
   **Escuchar este bloque**. Save edits first. Montage uses existing audio on CPU;
   it does not synthesize, transcribe, align or produce a validated final.
5. Play, pause, seek or stop the prepared audio. Beacon has a separate switch
   and volume. Downloads contain voice WAV and the exact timing manifest.
6. **Historial de versiones** can restore a previous composition as a new
   version. Restoring the original catalog can include variants; the UI marks
   this explicitly. It does not delete subsequent versions.

Opening a page does not request a montage or autoplay. A listening action
prepares a saved snapshot and attempts playback; browsers may require pressing
**Reproducir** after preparation. Editing, selecting a different set, restoring
or leaving stops the current listen. Block positions become sample-exact for
the prepared selection; positions outside that selection remain estimates.

### Collection and API contract

Private JSON collections live below `data/score_workspaces/collections/` and use
`score-workspace-collection-v1`. The validated schema in `score_workspaces.py`
requires explicit source IDs and hashes, block order, `main`/`variant` roles,
stage titles, pauses and optional response markers. A catalog file is not a
selector for whichever project was updated last. Keep the collection files,
score database and derived previews with the original project/audio backup.

- `GET /api/score-workspaces` lists configured collections.
- `GET /api/score-workspaces/{id}` reads the saved composition.
- `GET /api/score-workspaces/{id}/revisions` lists its history.
- `POST .../revisions` sends `expected_revision_id`, `lead_in_ms` and ordered
  `{source_key, pause_after_ms}` blocks.
- `POST .../restore` sends `expected_revision_id` and the source `revision_id`.
- `POST .../preview` sends `revision_id` and optionally unique `source_keys`.
- Returned scoped URLs expose authenticated audio, download, manifest and Beacon.

Limits default to 256 blocks, 60 seconds per silence, 3600 seconds per preview,
256 KiB per write request and 16 MiB disk headroom. Preview limits are configurable
through `QVL_SCORE_PREVIEW_MAX_SOURCES`,
`QVL_SCORE_PREVIEW_MAX_DURATION_SECONDS` and
`QVL_SCORE_PREVIEW_DISK_HEADROOM_BYTES`. A busy montage returns 409; no job or
model is launched. Changing a pinned catalog or source requires explicit
operator resolution rather than silently rebasing an existing composition.

Listening and source validation are technical checks. They do not establish
perceptual quality, human approval or participant-protocol readiness.
