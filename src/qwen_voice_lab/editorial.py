from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from .models import ProjectSegment

PAUSE_RE = re.compile(r"^\[(?P<seconds>(?:\d+(?:\.\d{1,3})?|\.\d{1,3}))s\]$")
LEGACY_PAUSE_RE = re.compile(
    r"^\[(?:pause|silence)\s*:?\s*(?P<seconds>\d+(?:\.\d{1,3})?)\s*s?\]$",
    re.IGNORECASE,
)
LEGACY_HOLD_RE = re.compile(
    r"`?\[hold\s*~(?P<start>\d+(?:\.\d+)?)"
    r"(?:\s*[–-]\s*(?P<end>\d+(?:\.\d+)?))?s\]`?",
    re.IGNORECASE,
)
FORBIDDEN_LINE_RE = re.compile(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|~~~)")
LEGACY_CUES = ("**", "__", "↘", "↗", "→", "←", "^", "<break", "[T]", "[S]", "[D]", "[R]")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class EditorialError(ValueError):
    def __init__(self, line: int, message: str):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


@dataclass(frozen=True)
class CompiledBlock:
    text: str
    normalized_text: str
    text_sha256: str
    pause_after_ms: int
    source_line: int


def normalize_spoken_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"[^\w\s']", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _speech_block(lines: list[tuple[int, str]]) -> tuple[int, str]:
    line_number = lines[0][0]
    text = " ".join(value.strip() for _, value in lines).strip()
    if not text:
        raise EditorialError(line_number, "empty spoken block")
    return line_number, text


def compile_markdown(markdown: str) -> list[CompiledBlock]:
    """Compile strict long-form Markdown into speech blocks and exact pauses.

    The accepted body language is intentionally small: plain text paragraphs and a
    standalone ``[Ns]`` after a spoken paragraph. Metadata belongs to the Project API.
    """

    paragraphs: list[tuple[int, str]] = []
    pending: list[tuple[int, str]] = []
    for line_number, raw in enumerate(markdown.replace("\r\n", "\n").split("\n"), start=1):
        value = raw.strip()
        if not value:
            if pending:
                paragraphs.append(_speech_block(pending))
                pending = []
            continue
        pause = PAUSE_RE.fullmatch(value)
        if pause:
            if pending:
                paragraphs.append(_speech_block(pending))
                pending = []
            paragraphs.append((line_number, value))
            continue
        if value.startswith("[") and value.endswith("]"):
            raise EditorialError(line_number, "unknown directive; pauses use standalone [Ns]")
        if FORBIDDEN_LINE_RE.match(value) or any(cue in value for cue in LEGACY_CUES):
            raise EditorialError(
                line_number,
                "formatting and legacy prosody cues are not spoken input; migrate them first",
            )
        pending.append((line_number, value))
    if pending:
        paragraphs.append(_speech_block(pending))

    blocks: list[CompiledBlock] = []
    for line_number, value in paragraphs:
        pause = PAUSE_RE.fullmatch(value)
        if pause:
            if not blocks:
                raise EditorialError(line_number, "a pause must follow a spoken block")
            seconds = float(pause.group("seconds"))
            if not 0 < seconds <= 60:
                raise EditorialError(line_number, "pause must be greater than 0 and at most 60s")
            if blocks[-1].pause_after_ms:
                raise EditorialError(line_number, "consecutive pauses are not allowed")
            previous = blocks[-1]
            blocks[-1] = CompiledBlock(
                **{**previous.__dict__, "pause_after_ms": round(seconds * 1000)}
            )
            continue
        normalized = normalize_spoken_text(value)
        if not normalized:
            raise EditorialError(line_number, "spoken block has no pronounceable text")
        blocks.append(
            CompiledBlock(
                text=value,
                normalized_text=normalized,
                text_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                pause_after_ms=0,
                source_line=line_number,
            )
        )
    if not blocks:
        raise EditorialError(1, "at least one spoken block is required")
    return blocks


def reconcile_segments(
    project_id: str,
    revision_id: str,
    blocks: list[CompiledBlock],
    previous: list[ProjectSegment],
) -> list[ProjectSegment]:
    reusable: dict[str, deque[ProjectSegment]] = defaultdict(deque)
    for segment in sorted(previous, key=lambda row: row.position):
        reusable[segment.normalized_text].append(segment)
    result = []
    for position, block in enumerate(blocks):
        prior = (
            reusable[block.normalized_text].popleft() if reusable[block.normalized_text] else None
        )
        result.append(
            ProjectSegment(
                id=prior.id if prior else _new_id("seg"),
                project_id=project_id,
                revision_id=revision_id,
                position=position,
                text=block.text,
                normalized_text=block.normalized_text,
                text_sha256=block.text_sha256,
                pause_after_ms=block.pause_after_ms,
                selected_take_id=(
                    prior.selected_take_id
                    if prior and prior.text_sha256 == block.text_sha256
                    else None
                ),
            )
        )
    return result


def migrate_legacy_markdown(markdown: str) -> tuple[str, list[dict[str, object]]]:
    """Best-effort, one-shot conversion. Runtime parsing never accepts these cues."""

    stream: list[tuple[str, str | float]] = []
    report: list[dict[str, object]] = []
    raw_lines = markdown.replace("\r\n", "\n").split("\n")
    in_preamble = any(re.fullmatch(r"\s*[-_*]{3,}\s*", row) for row in raw_lines)
    for line_number, raw in enumerate(raw_lines, start=1):
        value = raw.strip()
        if not value:
            continue
        if in_preamble:
            report.append({"line": line_number, "from": raw, "to": "", "action": "discard"})
            if re.fullmatch(r"[-_*]{3,}", value):
                in_preamble = False
            continue
        if (
            FORBIDDEN_LINE_RE.match(value)
            or re.fullmatch(r"[-_*]{3,}", value)
            or (value.startswith(">") and value.endswith("]**"))
            or re.fullmatch(r"\*?\(.*\)\*?", value)
        ):
            report.append({"line": line_number, "from": raw, "to": "", "action": "discard"})
            continue
        hold = LEGACY_HOLD_RE.search(value)
        if hold:
            start = float(hold.group("start"))
            end = float(hold.group("end") or start)
            seconds = (start + end) / 2
            stream.append(("pause", seconds))
            report.append(
                {
                    "line": line_number,
                    "from": raw,
                    "to": f"[{seconds:g}s]",
                    "action": "convert-hold",
                }
            )
            continue
        legacy_pause = LEGACY_PAUSE_RE.fullmatch(value)
        if legacy_pause:
            converted = f"[{legacy_pause.group('seconds')}s]"
            stream.append(("pause", float(legacy_pause.group("seconds"))))
            report.append({"line": line_number, "from": raw, "to": converted})
            continue
        canonical_pause = PAUSE_RE.fullmatch(value)
        if canonical_pause:
            stream.append(("pause", float(canonical_pause.group("seconds"))))
            continue
        if value == "^":
            stream.append(("pause", 1.2))
            report.append({"line": line_number, "from": raw, "to": "[1.2s]"})
            continue
        converted = value
        for cue in ("**", "__", "↘", "↗", "→", "←"):
            converted = converted.replace(cue, "")
        converted = re.sub(r"(?<!\*)\*(?!\*)", "", converted)
        converted = re.sub(r"\[(?:T|S|D|R)\]", "", converted)
        parts = re.split(r"(\s*/\s+|\s*\^\s*|…+|\.{3,})", converted)
        for part in parts:
            if not part:
                continue
            if "^" in part:
                stream.append(("pause", 1.2))
            elif part.strip() == "/":
                stream.append(("pause", 0.5))
            elif "…" in part or re.fullmatch(r"\.{3,}", part):
                stream.append(("pause", 3.0))
            elif speech := part.strip():
                stream.append(("speech", speech))
        if converted != raw or len(parts) > 1:
            report.append({"line": line_number, "from": raw, "to": converted})
    collapsed: list[tuple[str, str | float]] = []
    for kind, value in stream:
        if kind == "speech" and not normalize_spoken_text(str(value)):
            report.append({"action": "discard-non-speech", "text": value})
            continue
        if kind == "pause" and collapsed and collapsed[-1][0] == "pause":
            collapsed[-1] = ("pause", float(collapsed[-1][1]) + float(value))
        elif kind == "pause" and not collapsed:
            report.append({"action": "discard-leading-pause", "seconds": value})
        else:
            collapsed.append((kind, value))
    output = [
        f"[{float(value):g}s]" if kind == "pause" else str(value) for kind, value in collapsed
    ]
    migrated = "\n\n".join(output).strip() + "\n"
    compile_markdown(migrated)
    return migrated, report
