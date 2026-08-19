from __future__ import annotations

import argparse
import json
from pathlib import Path

from .editorial import migrate_legacy_markdown


def migrate_editorial(input_file: Path, output_file: Path, report_file: Path) -> int:
    migrated, changes = migrate_legacy_markdown(input_file.read_text(encoding="utf-8"))
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(migrated, encoding="utf-8")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        json.dumps(
            {
                "schema_version": "qwen-voice-lab-editorial-migration-v1",
                "input": str(input_file),
                "output": str(output_file),
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="qvl")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-editorial")
    migrate.add_argument("input", type=Path)
    migrate.add_argument("--output", type=Path, required=True)
    migrate.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "migrate-editorial":
        return migrate_editorial(args.input, args.output, args.report)
    return 2
