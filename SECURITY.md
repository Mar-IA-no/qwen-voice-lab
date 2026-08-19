# Security and privacy

Qwen Voice Lab is a local instrument. Its default network binding is `127.0.0.1`; remote access is expected through an authenticated SSH tunnel, a trusted private VPN interface, or a separately managed reverse proxy.

## Trust boundary

- The repository contains one explicitly licensed original synthetic starter voice. It contains no human reference recording, generated user render, model weight, API token, or deployment credential.
- Imported voices require an explicit consent field. Product operators remain responsible for obtaining and documenting permission from the speaker.
- Local files are created with owner-only permissions. Git ignores `data/`, model weights, environment files and every audio asset except the reviewed starter voice.
- A populated private archive is readable by every authenticated user of one installation. The MVP has one installation-wide token, not per-user roles; restrict tunnel, VPN or proxy access accordingly.
- The service has no paid provider, analytics client, telemetry endpoint, or remote fallback.
- File responses resolve under the configured voice, render, archive or project directory before being served. Long-form takes, finished WAVs and manifests additionally reject symlinked paths, are opened once with no-follow semantics, and must match their recorded SHA-256; serving and assembly consume authenticated byte snapshots rather than reopening mutable paths.
- Long-form ASR and forced alignment are local-only. Validator commands are explicit operator configuration; there is no remote transcription fallback.

## GPU boundary

Dedicated machines run Qwen directly and do not need an external scheduler. Shared deployments may opt into a configured launcher marker and Linux cgroup pattern before Qwen initializes. The CPU API may run as an unprivileged service account while narrowly validated commands admit and stop only the configured worker identity. Cancellation and API shutdown stop the exact transient unit before terminating its controller. [`gpu-priorityd`](https://github.com/Mar-IA-no/gpu-priorityd) is one compatible public controller. The mock engine remains available for CPU-only development.

The optional Qwen3-ASR/ForcedAligner validator uses a separate Python environment and process. On shared hardware its configured command must enter the same serial admission boundary as TTS; operators must not allow both model families to claim the device concurrently.

## Deployment notes

Non-loopback binding is rejected unless `QVL_ACCESS_TOKEN` contains at least 32 characters or the operator explicitly enables the unsafe unauthenticated override. Token login creates an HttpOnly, SameSite-strict cookie. Set `QVL_COOKIE_SECURE=true` whenever HTTPS terminates at the application-facing origin. A direct VPN deployment must bind to the machine's specific private VPN address, never `0.0.0.0`. The single-token layer does not replace TLS, rate limiting or per-user authorization on an untrusted network; exposing the raw application port there remains outside the supported deployment boundary.

Security reports can be filed privately through GitHub's security advisory flow for this repository.
