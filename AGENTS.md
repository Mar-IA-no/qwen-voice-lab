# AGENTS.md — Qwen Voice Lab

## Product boundary

Qwen Voice Lab is a local-first web studio for synthetic voice design, authorized voice cloning, controlled comparison and scored TTS rendering with Qwen3-TTS.

## Required reading

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `SECURITY.md`
4. The latest entries in `BITACORA.md`

## Engineering rules

- Keep user recordings, generated renders, model weights, databases, environment files and private profile manifests out of Git.
- Never add a human voice reference without explicit, documented permission.
- Preserve exact reference transcripts and verify audio hashes before synthesis.
- Keep text-only development usable through the deterministic mock engine.
- A functional T/S/D/R score must fail explicitly when the selected identity has no complete prosody profile.
- Shared-GPU admission is optional and configurable; do not hard-code deployment-specific launchers, cgroup names, hosts or network addresses.
- Preserve backward compatibility for ordinary neutral synthesis and text clients.
- Direct CUDA is the product default. On a shared GPU, use the operator's configured admission process; [`gpu-priorityd`](https://github.com/Mar-IA-no/gpu-priorityd) is an optional compatible controller.
- Run backend tests, Ruff, the frontend production build and the packaged-static diff before publishing.

## Public-repository hygiene

- Public documentation and fixtures must use generic identities and paths.
- The only bundled audio is an explicitly licensed original synthetic starter voice.
- Do not commit deployment inventories, private evaluation results, internal hostnames, absolute workspace paths or private catalog metadata.
