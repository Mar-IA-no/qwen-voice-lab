from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .editorial import migrate_legacy_markdown


def _stage_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o600)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _stage_text(path: Path, content: str) -> Path:
    return _stage_bytes(path, content.encode("utf-8"))


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
    output_backup = None
    report_backup = None
    output_installed = False
    report_installed = False
    try:
        output_backup = _stage_bytes(output, output.read_bytes()) if output.exists() else None
        report_backup = _stage_bytes(report, report.read_bytes()) if report.exists() else None
        os.replace(staged_output, output)
        output_installed = True
        os.replace(staged_report, report)
        report_installed = True
    except Exception as install_error:
        rollback_errors = []
        for target, backup, installed in (
            (output, output_backup, output_installed),
            (report, report_backup, report_installed),
        ):
            if not installed:
                continue
            try:
                if backup:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "migration install failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from install_error
        raise
    finally:
        for temporary in (staged_output, staged_report, output_backup, report_backup):
            if temporary:
                temporary.unlink(missing_ok=True)
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
