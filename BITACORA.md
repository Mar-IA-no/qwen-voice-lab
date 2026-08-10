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
