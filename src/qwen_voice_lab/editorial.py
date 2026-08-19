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
INLINE_MARKDOWN_RE = re.compile(r"(?:\[[^\]]*\]|[*_`]|!\[|<[^>]+>)")


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
    normalized_source = markdown.replace("\r\n", "\n").replace("\r", "\n")
    for line_number, raw in enumerate(normalized_source.split("\n"), start=1):
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
        if "[" in value or "]" in value:
            raise EditorialError(line_number, "unknown directive; pauses use standalone [Ns]")
        if (
            FORBIDDEN_LINE_RE.match(value)
            or any(cue in value for cue in LEGACY_CUES)
            or INLINE_MARKDOWN_RE.search(value)
        ):
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
    exact: dict[str, deque[ProjectSegment]] = defaultdict(deque)
    normalized: dict[str, deque[ProjectSegment]] = defaultdict(deque)
    for segment in sorted(previous, key=lambda row: row.position):
        exact[segment.text_sha256].append(segment)
        normalized[segment.normalized_text].append(segment)
    used: set[str] = set()
    assignments: list[ProjectSegment | None] = [None] * len(blocks)
    for position, block in enumerate(blocks):
        while exact[block.text_sha256] and assignments[position] is None:
            candidate = exact[block.text_sha256].popleft()
            if candidate.id not in used:
                assignments[position] = candidate
                used.add(candidate.id)
    for position, block in enumerate(blocks):
        while assignments[position] is None and normalized[block.normalized_text]:
            candidate = normalized[block.normalized_text].popleft()
            if candidate.id not in used:
                assignments[position] = candidate
                used.add(candidate.id)
    result = []
    for position, block in enumerate(blocks):
        prior = assignments[position]
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
    raw_lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs: list[list[tuple[int, str]]] = []
    pending: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            paragraphs.append(pending)
            pending = []

    for line_number, raw in enumerate(raw_lines, start=1):
        value = raw.strip()
        if not value:
            flush()
            continue
        if (
            FORBIDDEN_LINE_RE.match(value)
            or re.fullmatch(r"[-_*]{3,}", value)
            or re.fullmatch(r"\*?\(.*\)\*?", value)
        ):
            flush()
            report.append(
                {"line": line_number, "from": raw, "to": "", "action": "discard-structure"}
            )
            continue
        if (
            PAUSE_RE.fullmatch(value)
            or LEGACY_PAUSE_RE.fullmatch(value)
            or value in {"^", "/"}
            or LEGACY_HOLD_RE.fullmatch(value)
        ):
            flush()
            paragraphs.append([(line_number, value)])
            continue
        pending.append((line_number, value))
    flush()

    cue_re = re.compile(
        rf"({LEGACY_HOLD_RE.pattern}|(?<!\S)/(?!\S)|\^|…+|\.{{3,}})",
        re.IGNORECASE,
    )
    for paragraph in paragraphs:
        line_number = paragraph[0][0]
        value = " ".join(row for _, row in paragraph)
        if len(paragraph) > 1:
            report.append(
                {
                    "line": line_number,
                    "from": "\n".join(row for _, row in paragraph),
                    "to": value,
                    "action": "join-soft-wrap",
                }
            )
        legacy_pause = LEGACY_PAUSE_RE.fullmatch(value)
        canonical_pause = PAUSE_RE.fullmatch(value)
        if legacy_pause or canonical_pause:
            seconds = float((legacy_pause or canonical_pause).group("seconds"))
            stream.append(("pause", seconds))
            if legacy_pause:
                report.append(
                    {
                        "line": line_number,
                        "from": value,
                        "to": f"[{seconds:g}s]",
                        "action": "convert-pause",
                    }
                )
            continue

        converted = value
        for cue in ("**", "__", "↘", "↗", "→", "←"):
            if cue in converted:
                converted = converted.replace(cue, "")
                report.append(
                    {"line": line_number, "from": cue, "to": "", "action": "remove-prosody-cue"}
                )
        converted, count = re.subn(r"(?<!\*)\*(?!\*)", "", converted)
        if count:
            report.append({"line": line_number, "from": "*", "to": "", "action": "remove-emphasis"})
        converted, count = re.subn(r"\[(?:T|S|D|R)\]", "", converted)
        if count:
            report.append(
                {
                    "line": line_number,
                    "from": "function tag",
                    "to": "",
                    "action": "remove-prosody-cue",
                }
            )

        cursor = 0
        for match in cue_re.finditer(converted):
            if speech := converted[cursor : match.start()].strip():
                stream.append(("speech", speech))
            token = match.group(0)
            hold = LEGACY_HOLD_RE.fullmatch(token)
            if hold:
                start = float(hold.group("start"))
                end = float(hold.group("end") or start)
                seconds = (start + end) / 2
                action = "convert-hold"
            elif token == "^":
                seconds, action = 1.2, "convert-caret"
            elif token == "/":
                seconds, action = 0.5, "convert-slash"
            else:
                seconds, action = 3.0, "convert-ellipsis"
            stream.append(("pause", seconds))
            report.append(
                {"line": line_number, "from": token, "to": f"[{seconds:g}s]", "action": action}
            )
            cursor = match.end()
        if speech := converted[cursor:].strip():
            stream.append(("speech", speech))
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
