# Experimental per-block prosody runtime

## Decision

The LAB may switch Qwen voice-clone references per score block when, and only when, the selected identity has a complete local T/S/D/R profile that declares the requested language. `neutral` keeps the catalog voice reference. A functional block on an identity without a profile, or outside its declared languages, is rejected before queueing.

This is reference-conditioned orchestration, not a universal style transform. Profiles belong to an identity and cannot be transferred between voices.

## Private profile contract

Profiles live under ignored `data/prosody_profiles/*.json`. A profile declares:

- a stable ID and `experimental` or `canonical` status;
- stable voice IDs for exact catalog matching, or identity tags for explicitly configured portable fixtures;
- all four T/S/D/R reference files;
- the exact transcript, SHA-256 and provenance of every reference;
- supported languages and methodological notes.

Startup resolves every file below `data/`, verifies its hash and fails closed on incomplete, ambiguous or invalid profiles. The API exposes only profile ID, status, functions and notes; local paths remain private.

## Local experimental profiles

An operator may register a complete experimental profile for a locally available identity. Experimental status must remain visible in the API and UI and must not be promoted to canonical solely because all four files exist.

For every segment, the engine resolves `(voice identity, prosodic function)` before creating or reusing a Qwen clone prompt. The output concatenates each generated block and its exact `pause_after_ms`. Prompt caching is content-addressed by reference audio hash and reference-text hash.

## Next quality gate

A canonical profile requires the same carrier text recorded in one controlled session for all four functions, human transcript verification, preserved originals and blind identity/function evaluation. Activating an experimental profile does not waive those requirements.
