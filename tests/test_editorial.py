from __future__ import annotations

import pytest

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


def test_one_shot_migrator_removes_legacy_cues_and_emits_canonical_pause() -> None:
    migrated, report = migrate_legacy_markdown("**Listen.** ↘\n\n^\n")
    assert migrated == "Listen.\n\n[1.2s]\n"
    assert len(report) == 2
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
    assert sum(row.get("action") == "discard" for row in report) == 3
