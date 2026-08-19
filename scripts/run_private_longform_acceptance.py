#!/usr/bin/env python3
"""Run the private six-language long-form CUDA acceptance against a live Lab.

The selected voice and all outputs remain in the operator-provided output directory,
which must be outside Git. The report records observations; it does not make a human
perceptual GO/NO-GO decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TEXTS = {
    "es": "Escuchá el sonido con calma. Observá cómo cambia mientras respirás.",
    "en": "Listen to the sound calmly. Notice how it changes while you breathe.",
    "pt": "Escute o som com calma. Observe como ele muda enquanto você respira.",
    "fr": "Écoutez le son calmement. Observez comment il change pendant que vous respirez.",
    "de": "Höre dem Klang in Ruhe zu. Beobachte, wie er sich beim Atmen verändert.",
}
ITALIAN = """Ascolta il suono con calma e lascia che trovi il suo spazio.

[0.7s]

Senti delle voci?

[0.5s]

Riconosci strumenti o diversi strati sonori intorno a te?

[0.8s]

Continua ad ascoltare e rimani presente fino alla fine.
"""


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode()}") from exc

    def bytes(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base_url + path, timeout=60) as response:
            return response.read()


def wait_run(api: Api, run_id: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/project-runs/{run_id}")
        if run["status"] in {"complete", "needs_review", "failed"}:
            return run
        time.sleep(2)
    raise TimeoutError(f"run {run_id} exceeded {timeout}s")


def write_asset(path: Path, content: bytes) -> dict[str, Any]:
    path.write_bytes(content)
    return {
        "path": str(path),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def normalized_words(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def technical_checks(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_language = {case["language"]: case for case in cases}
    italian = by_language.get("it", {})
    italian_audit = italian.get("assembly", {}).get("audit", {})
    italian_transcript = normalized_words(str(italian_audit.get("transcript", "")))
    selected_italian_reports = [
        report
        for segment in italian.get("project", {}).get("segments", [])
        for take in italian.get("takes", {}).get(segment["id"], [])
        if take.get("id") == segment.get("selected_take_id")
        for report in take.get("quality_reports", [])
    ]
    checks = {
        "all_six_languages_present": set(by_language) == {"es", "en", "pt", "fr", "de", "it"},
        "all_runs_complete": all(case.get("run", {}).get("status") == "complete" for case in cases),
        "all_assemblies_created": all("assembly" in case and "wav" in case for case in cases),
        "all_final_audits_pass": all(
            case.get("assembly", {}).get("audit_status") == "pass" for case in cases
        ),
        "italian_question_present": "senti delle voci" in italian_transcript,
        "italian_following_block_present": (
            "riconosci strumenti o diversi strati sonori intorno a te" in italian_transcript
        ),
        "italian_final_words_present": "presente fino alla fine" in italian_transcript,
        "italian_identity_windows_recorded": any(
            report.get("identity_windows") for report in selected_italian_reports
        ),
    }
    return {"passed": all(checks.values()), "checks": checks}


def run_case(api: Api, voice_id: str, output: Path, language: str, markdown: str) -> dict:
    project = api.request(
        "POST",
        "/api/projects",
        {
            "title": f"Private CUDA acceptance · {language.upper()}",
            "voice_id": voice_id,
            "language": language,
            "project_seed": 20260819,
            "sampling": {"temperature": 0.7, "subtalker_temperature": 0.7},
            "markdown": markdown,
        },
    )
    run = wait_run(api, api.request("POST", f"/api/projects/{project['id']}/runs", {})["id"], 900)
    detail = api.request("GET", f"/api/projects/{project['id']}")
    takes = {
        segment["id"]: api.request(
            "GET", f"/api/projects/{project['id']}/segments/{segment['id']}/takes"
        )
        for segment in detail["segments"]
    }
    result: dict[str, Any] = {
        "language": language,
        "project": detail,
        "run": run,
        "takes": takes,
    }
    if all(segment.get("selected_take_id") for segment in detail["segments"]):
        assembly = api.request("POST", f"/api/projects/{project['id']}/assemblies", {})
        result["assembly"] = assembly
        result["wav"] = write_asset(
            output / f"{language}.wav", api.bytes(f"/api/assemblies/{assembly['id']}/download")
        )
        manifest = api.bytes(f"/api/assemblies/{assembly['id']}/manifest")
        result["manifest"] = write_asset(output / f"{language}.manifest.json", manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    api = Api(args.base_url)
    capabilities = api.request("GET", "/api/capabilities")
    cases = []
    for language, text in TEXTS.items():
        cases.append(run_case(api, args.voice_id, args.output_dir, language, text))
    cases.append(run_case(api, args.voice_id, args.output_dir, "it", ITALIAN))
    technical = technical_checks(cases)
    report = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "voice_id": args.voice_id,
        "capabilities": capabilities,
        "cases": cases,
        "technical_acceptance": technical,
        "human_listening_review": "pending",
    }
    (args.output_dir / "evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output_dir / "evidence.json")
    return 0 if technical["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
