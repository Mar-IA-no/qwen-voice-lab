# Architecture contract — Qwen Voice Lab MVP

## Product boundary

Qwen Voice Lab owns voice identity creation, authorized reference import, local synthesis, score timing, controlled comparison, job history, and render metrics. It does not own conversational orchestration, participant recording, session persistence, production GPU scheduling, or paid speech providers.

## Runtime flow

```text
browser ── installation token → HttpOnly session
  └─ FastAPI contract (always-on unprivileged CPU process)
       ├─ SQLite metadata catalog
       ├─ owner-only local audio store
       └─ serial job queue
            ├─ mock diagnostic engine
            ├─ direct Qwen engine (dedicated GPU)
            └─ wrapped-worker proxy (shared GPU)
                 └─ priority wrapper → reusable Qwen worker
                      ├─ 1.7B Base: clone synthesis
                      └─ 1.7B VoiceDesign: original identity sample
```

The queue is the only route to generation. A job transitions through `queued → running → complete`, with `failed` and `cancelled` as terminal alternatives. Startup marks interrupted `queued` or `running` records as failed rather than replaying private work automatically.

## Model residency

The Qwen engine loads on the first job. Clone and VoiceDesign models never remain resident together: switching job kind unloads the current model, clears prompt tensors, runs garbage collection, empties the CUDA allocator, and then loads the other model.

On a dedicated GPU, the Qwen engine remains in the API process and an idle sweep unloads it after the configured interval. On shared infrastructure, `QVL_REQUIRE_GPU_WRAPPER=true` keeps FastAPI, the UI, SQLite and the serial queue outside CUDA. The first render starts a separate worker through `QVL_GPU_WRAPPER`; that worker independently requires both `QVL_GPU_WRAPPED=1` and a Linux cgroup matching `QVL_GPU_CGROUP_PATTERN` before it can load Qwen. It stays admitted and reuses the active model until `QVL_MODEL_IDLE_SECONDS` elapses, then exits and releases its scope.

Production preemption terminates only the worker. The current job becomes `failed` with an explicit diagnostic, the API remains reachable, and a cooldown prevents queued comparison jobs from relaunching the scheduler in a tight loop. A later explicit job requests a new worker automatically. Interrupted private jobs are never replayed automatically. Cancellation and API shutdown stop the exact admitted transient unit; passive metadata cleanup targets only units matching the configured name, cgroup pattern, repository path and worker module.

## Identity lifecycle

- **Clone:** multipart audio + explicit consent + optional exact transcript. The file is decoded, measured, hashed, and moved under `data/voices/<voice_id>/`.
- **Designed:** VoiceDesign creates a durable sample job from instruction + sample text. Completion does not alter the voice catalog. An explicit, idempotent promotion copies the chosen sample into a deterministic voice package with transcript, instruction, and content hash.
- **Bundled starter:** a reviewed original synthetic voice is copied from package data into an empty local catalog. Its audio hash, exact carrier transcript, VoiceDesign instruction and asset license ship together.
- **Delete:** removes the identity reference and catalog row. Existing rendered outputs remain for traceability.

No absolute storage path is returned by public API models. Catalog export contains metadata but no reference file path or audio.

## Private archive

The optional `data/archive/` tree holds prior references, manifests, experiments, segments, and finished locutions. Its API exposes stable IDs and relative paths only. Audio resolution rechecks that the selected file remains below the configured archive root. The archive is a listening and provenance surface; only identities explicitly registered in the voice catalog can be used for new synthesis.

## Score contract

A score contains 1–64 ordered segments. Every segment carries exact text, pause-after milliseconds, and a prosodic function (`T`, `S`, `D`, `R`, or `neutral`). The MVP renders pauses exactly and preserves labels durably. For an identity with a complete private profile, Qwen selects the corresponding reference prompt per block; `neutral` uses the selected catalog reference. Unsupported functional scores fail before queueing.

Prosodic readiness belongs to a voice identity, not to a universal preset. Every functional voice requires four identity-preserving variants and perceptual validation before the engine may switch references per segment. The API and UI expose this capability explicitly without publishing private profile paths.

## Comparison contract

A comparison expands into 2–5 ordinary synthesis jobs with identical text, language, and seed. Each job records model label, device, model-load time, generation time, first-audio time, output duration, RTF, peak allocated VRAM, bytes, and SHA-256.

## Privacy boundary

The application binds to loopback by default, requires a token for non-loopback binding, accepts no remote provider configuration, and stores local audio with mode `0600`. Authentication is installation-wide rather than per-user. Git ignores the data tree and model formats. Only the reviewed synthetic starter asset is versioned.
