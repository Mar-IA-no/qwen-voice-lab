from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .editorial import migrate_legacy_markdown


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o600)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def migrate_editorial(
    input_file: Path, output_file: Path, report_file: Path, *, overwrite: bool = False
) -> int:
    source = input_file.expanduser().resolve(strict=True)
    output = output_file.expanduser().resolve(strict=False)
    report = report_file.expanduser().resolve(strict=False)
    if len({source, output, report}) != 3:
        raise ValueError("input, output, and report paths must be different")
    paths = (source, output, report)
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left.exists() and right.exists() and left.samefile(right):
                raise ValueError("input, output, and report must not reference the same file")
    existing = [path for path in (output, report) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"refusing to overwrite: {', '.join(map(str, existing))}")
    migrated, changes = migrate_legacy_markdown(source.read_text(encoding="utf-8"))
    report_content = (
        json.dumps(
            {
                "schema_version": "qwen-voice-lab-editorial-migration-v1",
                "input": str(source),
                "output": str(output),
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    staged_output = _stage_text(output, migrated)
    try:
        staged_report = _stage_text(report, report_content)
    except Exception:
        staged_output.unlink(missing_ok=True)
        raise
    os.replace(staged_output, output)
    os.replace(staged_report, report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="qvl")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-editorial")
    migrate.add_argument("input", type=Path)
    migrate.add_argument("--output", type=Path, required=True)
    migrate.add_argument("--report", type=Path, required=True)
    migrate.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.command == "migrate-editorial":
        return migrate_editorial(args.input, args.output, args.report, overwrite=args.overwrite)
    return 2
