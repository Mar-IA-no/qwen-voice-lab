from __future__ import annotations

import pytest

from qwen_voice_lab.cli import migrate_editorial
from qwen_voice_lab.editorial import (
    EditorialError,
    compile_markdown,
    migrate_legacy_markdown,
    reconcile_segments,
)


def test_compiler_accepts_only_spoken_paragraphs_and_standalone_pauses() -> None:
    blocks = compile_markdown(
        "First line of one thought.\ncontinues here.\n\n[1.2s]\n\nSecond thought.\n"
    )
    assert [row.text for row in blocks] == [
        "First line of one thought. continues here.",
        "Second thought.",
    ]
    assert [row.pause_after_ms for row in blocks] == [1200, 0]


@pytest.mark.parametrize(
    "source",
    [
        "[1s]\n\nSpeech.",
        "Speech.\n\n[0s]",
        "Speech.\n\n[61s]",
        "Speech. ^",
        "**Speech.**",
        "# Metadata",
        "Speech.\n\n[pause: 1.2s]",
        "Speak [1s] now.",
        "Speak *softly*.",
        "Read [this](https://example.test).",
        "Use `quietly`.",
    ],
)
def test_compiler_rejects_ambiguous_or_legacy_notation(source: str) -> None:
    with pytest.raises(EditorialError):
        compile_markdown(source)


def test_pause_only_reconciliation_reuses_segment_identity_and_selection() -> None:
    initial = reconcile_segments("project", "r1", compile_markdown("Hello.\n\n[1s]"), [])
    initial[0].selected_take_id = "take_approved"
    revised = reconcile_segments("project", "r2", compile_markdown("Hello.\n\n[2.5s]"), initial)
    assert revised[0].id == initial[0].id
    assert revised[0].selected_take_id == "take_approved"
    assert revised[0].pause_after_ms == 2500


def test_punctuation_change_keeps_stable_id_but_invalidates_take() -> None:
    initial = reconcile_segments("project", "r1", compile_markdown("Hello."), [])
    initial[0].selected_take_id = "take_old"
    revised = reconcile_segments("project", "r2", compile_markdown("Hello!"), initial)
    assert revised[0].id == initial[0].id
    assert revised[0].selected_take_id is None


def test_reconciliation_prefers_exact_hash_before_normalized_fallback() -> None:
    initial = reconcile_segments("project", "r1", compile_markdown("Hello!\n\nHello?"), [])
    initial[0].selected_take_id = "take_bang"
    initial[1].selected_take_id = "take_question"
    revised = reconcile_segments("project", "r2", compile_markdown("Hello?\n\nHello!"), initial)
    assert [row.id for row in revised] == [initial[1].id, initial[0].id]
    assert [row.selected_take_id for row in revised] == ["take_question", "take_bang"]

    mixed = reconcile_segments("project", "r3", compile_markdown("Hello.\n\nHello!"), initial)
    assert mixed[1].id == initial[0].id
    assert mixed[1].selected_take_id == "take_bang"
    assert mixed[0].id == initial[1].id
    assert mixed[0].selected_take_id is None


def test_one_shot_migrator_removes_legacy_cues_and_emits_canonical_pause() -> None:
    migrated, report = migrate_legacy_markdown("**Listen.** ↘\n\n^\n")
    assert migrated == "Listen.\n\n[1.2s]\n"
    assert {row["action"] for row in report} == {
        "remove-prosody-cue",
        "convert-caret",
    }
    assert compile_markdown(migrated)[0].pause_after_ms == 1200


def test_migrator_discards_editorial_header_and_converts_inline_cues() -> None:
    source = """# Guided induction

> **Overall voice:** warm and slow.

- / = half-second pause

First, / listen. ^ / Stay **with** the sound. ↘
"""
    migrated, report = migrate_legacy_markdown(source)
    blocks = compile_markdown(migrated)
    assert [row.text for row in blocks] == ["First,", "listen.", "Stay with the sound."]
    assert [row.pause_after_ms for row in blocks] == [500, 1700, 0]
    assert sum(str(row.get("action", "")).startswith("discard") for row in report) == 3


def test_migrator_preserves_speech_across_structures_holds_wraps_and_cr() -> None:
    source = (
        "Spoken before.\r---\rBefore `[hold ~1.0–2.0s]` after.\r\r"
        "Soft wrapped\rparagraph /\r\rLast.\r"
    )
    migrated, report = migrate_legacy_markdown(source)
    blocks = compile_markdown(migrated)
    assert [row.text for row in blocks] == [
        "Spoken before.",
        "Before",
        "after.",
        "Soft wrapped paragraph",
        "Last.",
    ]
    assert [row.pause_after_ms for row in blocks] == [0, 1500, 0, 500, 0]
    assert any(row.get("action") == "convert-hold" for row in report)
    assert any(row.get("action") == "convert-slash" for row in report)
    assert any(row.get("action") == "join-soft-wrap" for row in report)


def test_migrator_does_not_treat_all_text_before_rule_as_preamble() -> None:
    migrated, report = migrate_legacy_markdown(
        "# Metadata\n\nSpoken before the rule...\n\n---\n\nSpoken after.\n"
    )
    blocks = compile_markdown(migrated)
    assert [row.text for row in blocks] == [
        "Spoken before the rule",
        "Spoken after.",
    ]
    assert blocks[0].pause_after_ms == 3000
    assert sum(str(row.get("action", "")).startswith("discard") for row in report) == 2


def test_migrator_preserves_standalone_pause_without_blank_lines() -> None:
    migrated, _ = migrate_legacy_markdown("One line.\n[pause: 1s]\nNext line.\n")
    blocks = compile_markdown(migrated)
    assert [row.text for row in blocks] == ["One line.", "Next line."]
    assert blocks[0].pause_after_ms == 1000


def test_migration_cli_rejects_collisions_and_requires_explicit_overwrite(tmp_path) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "canonical.md"
    report = tmp_path / "report.json"
    source.write_text("Listen. ^\n", encoding="utf-8")
    with pytest.raises(ValueError):
        migrate_editorial(source, source, report)
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        migrate_editorial(source, output, report)
    assert output.read_text(encoding="utf-8") == "keep"
    assert migrate_editorial(source, output, report, overwrite=True) == 0
    assert output.read_text(encoding="utf-8") == "Listen.\n\n[1.2s]\n"
    assert report.is_file()
