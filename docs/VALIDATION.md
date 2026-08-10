# Validation

## Automated contract

The continuous-integration workflow runs:

- Ruff over Python source and tests;
- the backend API and queue suite;
- a clean npm install;
- TypeScript and Vite production compilation;
- a reproducibility check against the packaged frontend;
- Python wheel and source-distribution builds.

Coverage includes consent enforcement, remote authentication, path privacy, archive resolution, scored pauses, per-function reference selection, rejection of unsupported prosody, VoiceDesign promotion idempotence, controlled comparisons, download semantics, GPU admission verification, retryable scheduler exits, restricted metadata cleanup and the absence of paid voice providers. A scheduler fixture also verifies lazy worker admission, reuse within one window, preemption isolation, API survival and launch cooldown without requiring CUDA.

An explicit root-only integration suite uses real `systemd-run` scopes with a fake non-CUDA worker. It proves that cancellation and FastAPI shutdown leave the admitted transient unit inactive. This test is opt-in through `QVL_RUN_SYSTEMD_TESTS=1` and is not run in ordinary CI.

## Bundled starter

The Amara Sol WAV is checked against SHA-256 `60a5788e3bd9f23a6ff10a684dc4b0c9d618eb9ea00163e6e7158954f915ea38` before catalog seeding. The API exposes its metadata and audio without revealing a local filesystem path. The exact VoiceDesign instruction and carrier transcript ship beside the asset.

## Real-engine scope

Qwen3-TTS Base cloning, VoiceDesign generation and multi-block profile switching have completed on a CUDA workstation through the same queue used by the web application. These smokes establish end-to-end execution; they do not establish a universal hardware floor or perceptual quality threshold.

Perceptual promotion remains a human decision. Experimental profiles must not be described as canonical solely because they load, render or produce distinct hashes.
