from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import threading
import uuid
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio_pipeline import build_timeline, read_project_asset, trim_speech_edges
from .config import Settings
from .editorial import (
    compile_markdown,
    normalize_spoken_text,
    reconcile_segments,
    segments_to_markdown,
    structured_segments,
)
from .engine import audio_info, sha256_file
from .models import (
    Assembly,
    AssemblyKind,
    BeaconSettings,
    Project,
    ProjectCreate,
    ProjectDetail,
    ProjectRun,
    ProjectSegment,
    ProjectStatus,
    QualityReport,
    RevisionBlock,
    RevisionCreate,
    RevisionRestore,
    RunStatus,
    ScoreSegment,
    SourceRevision,
    SynthesisRequest,
    Take,
    TakeDetail,
    TakeSelection,
    TakeStatus,
    utc_now,
)
from .quality import ContentValidator, ValidationItem, ValidationResult
from .storage import Store


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def deterministic_seed(project_seed: int, segment_id: str, attempt: int) -> int:
    material = f"{project_seed}:{segment_id}:{attempt}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 2_147_483_648


class LongFormManager:
    def __init__(self, settings: Settings, store: Store, engine, engine_lock: threading.RLock):
        self.settings = settings
        self.store = store
        self.engine = engine
        self.engine_lock = engine_lock
        self.validator = ContentValidator(settings)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._active_run_id: str | None = None

    async def start(self) -> None:
        for project in self.store.list_projects():
            interrupted_runs = []
            runs = self.store.list_runs(project.id)
            for run in runs:
                if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    run.status = RunStatus.FAILED
                    run.error = "The process stopped before this run completed."
                    run.finished_at = utc_now()
                    interrupted_runs.append(run)
            if interrupted_runs or project.status == ProjectStatus.GENERATING:
                segments = (
                    self.store.list_segments(project.current_revision_id)
                    if project.current_revision_id
                    else []
                )
                latest_status = runs[0].status if runs else None
                project.status = (
                    ProjectStatus.READY
                    if (
                        latest_status == RunStatus.COMPLETE
                        and segments
                        and all(row.selected_take_id for row in segments)
                    )
                    else ProjectStatus.NEEDS_REVIEW
                )
                project.updated_at = utc_now()
                if interrupted_runs:
                    for run in interrupted_runs:
                        self.store.save_terminal_run_and_project(run, project)
                else:
                    self.store.save_project(project)
        self._worker = asyncio.create_task(self._run(), name="qvl-long-form-worker")

    async def stop(self) -> None:
        cleanup_error: Exception | None = None
        if self._worker:
            had_active_run = self._active_run_id is not None
            self._worker.cancel()
            if had_active_run and (cancel_active := getattr(self.engine, "cancel_active", None)):
                try:
                    await asyncio.to_thread(cancel_active)
                except Exception as exc:
                    cleanup_error = exc
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self.validator.close()
        if cleanup_error:
            raise RuntimeError("active long-form GPU worker cleanup failed") from cleanup_error

    def create_project(self, request: ProjectCreate) -> ProjectDetail:
        if not self.store.get_voice(request.voice_id):
            raise KeyError(request.voice_id)
        project = Project(
            id=new_id("project"),
            title=request.title,
            voice_id=request.voice_id,
            language=request.language,
            project_seed=request.project_seed,
            sampling=request.sampling,
            baseline_speed=request.baseline_speed,
            provenance=request.provenance,
        )
        return self._persist_revision(
            project, request.markdown, request.blocks, [], [],
            lead_pause_ms=request.lead_pause_ms,
            speech_speed=request.speech_speed,
            beacon=request.beacon,
        )

    def add_revision(self, project_id: str, request: RevisionCreate) -> ProjectDetail:
        project = self._project(project_id)
        expected_current_revision_id = project.current_revision_id
        if not expected_current_revision_id:
            raise ValueError("project has no current revision")
        if (
            request.expected_revision_id
            and request.expected_revision_id != expected_current_revision_id
        ):
            raise ValueError("project revision changed; reload before saving")
        revisions = self.store.list_revisions(project_id)
        previous = (
            self.store.list_segments(project.current_revision_id)
            if project.current_revision_id
            else []
        )
        current_revision = self.store.get_revision(expected_current_revision_id)
        return self._persist_revision(
            project,
            request.markdown,
            request.blocks,
            previous,
            revisions,
            expected_current_revision_id=expected_current_revision_id,
            lead_pause_ms=(
                request.lead_pause_ms if "lead_pause_ms" in request.model_fields_set
                else current_revision.lead_pause_ms
            ),
            speech_speed=(
                request.speech_speed if "speech_speed" in request.model_fields_set
                else current_revision.speech_speed
            ),
            beacon=(
                request.beacon if "beacon" in request.model_fields_set
                else current_revision.beacon
            ),
        )

    def _persist_revision(
        self,
        project: Project,
        markdown: str | None,
        blocks,
        previous,
        revisions,
        *,
        expected_current_revision_id: str | None = None,
        lead_pause_ms: int = 0,
        speech_speed: float = 1,
        beacon=None,
        restored_from_revision_id: str | None = None,
    ) -> ProjectDetail:
        revision_id = new_id("revision")
        if blocks is not None:
            segments = structured_segments(project.id, revision_id, blocks, previous)
            markdown = segments_to_markdown(segments)
        else:
            assert markdown is not None
            segments = reconcile_segments(
                project.id, revision_id, compile_markdown(markdown), previous
            )
        if beacon and beacon.enabled:
            self._verify_beacon(project.id, beacon.asset_id)
        snapshot = [self._block_from_segment(row) for row in segments]
        source = json.dumps(
            {"blocks": [row.model_dump() for row in snapshot], "lead_pause_ms": lead_pause_ms,
             "speech_speed": speech_speed, "beacon": beacon.model_dump() if beacon else None},
            ensure_ascii=False, sort_keys=True,
        )
        revision = SourceRevision(
            id=revision_id,
            project_id=project.id,
            number=(revisions[0].number + 1) if revisions else 1,
            markdown=markdown,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            blocks=snapshot,
            lead_pause_ms=lead_pause_ms,
            speech_speed=speech_speed,
            beacon=beacon or BeaconSettings(),
            restored_from_revision_id=restored_from_revision_id,
        )
        project.current_revision_id = revision.id
        project.status = (
            ProjectStatus.READY
            if segments and all(row.selected_take_id for row in segments)
            else ProjectStatus.DRAFT
        )
        project.updated_at = utc_now()
        if expected_current_revision_id is None:
            self.store.save_project_revision(project, revision, segments)
        else:
            self.store.save_project_revision_if_idle(
                project,
                revision,
                segments,
                expected_current_revision_id,
            )
        return ProjectDetail(**project.model_dump(), revision=revision, segments=segments)

    @staticmethod
    def _block_from_segment(segment: ProjectSegment) -> RevisionBlock:
        return RevisionBlock(
            id=segment.id, text=segment.text, pause_after_ms=segment.pause_after_ms,
            speed=segment.speed, provenance=segment.provenance,
            selected_take_id=segment.selected_take_id,
            selection_override_reason=segment.selection_override_reason,
        )

    def list_revisions(self, project_id: str) -> list[SourceRevision]:
        self._project(project_id)
        return [self._complete_revision(row) for row in self.store.list_revisions(project_id)]

    def _complete_revision(self, revision: SourceRevision) -> SourceRevision:
        if not revision.blocks:
            revision.blocks = [
                self._block_from_segment(row) for row in self.store.list_segments(revision.id)
            ]
        return revision

    def restore_revision(self, project_id: str, request: RevisionRestore) -> ProjectDetail:
        project = self._project(project_id)
        source = self.store.get_revision(request.revision_id)
        if not source or source.project_id != project_id:
            raise KeyError(request.revision_id)
        current_id = project.current_revision_id
        if request.expected_revision_id and request.expected_revision_id != current_id:
            raise ValueError("project revision changed; reload before restoring")
        source = self._complete_revision(source)
        blocks = [row.model_copy() for row in source.blocks]
        revision_id = new_id("revision")
        segments = [ProjectSegment(
            id=block.id, project_id=project_id, revision_id=revision_id, position=index,
            text=block.text, normalized_text=normalize_spoken_text(block.text),
            text_sha256=hashlib.sha256(block.text.encode()).hexdigest(),
            pause_after_ms=block.pause_after_ms, speed=block.speed,
            provenance=block.provenance, selected_take_id=block.selected_take_id,
            selection_override_reason=block.selection_override_reason,
        ) for index, block in enumerate(blocks)]
        return self._publish_snapshot(
            project, source, segments, current_id, restored_from=source.id
        )

    def get_project(self, project_id: str) -> ProjectDetail:
        project = self._project(project_id)
        revision = (
            self.store.get_revision(project.current_revision_id)
            if project.current_revision_id
            else None
        )
        if revision:
            revision = self._complete_revision(revision)
        segments = self.store.list_segments(revision.id) if revision else []
        return ProjectDetail(**project.model_dump(), revision=revision, segments=segments)

    def _publish_snapshot(
        self,
        project: Project,
        source: SourceRevision,
        segments: list[ProjectSegment],
        expected_revision_id: str,
        *,
        restored_from: str | None = None,
        run: ProjectRun | None = None,
    ) -> ProjectDetail:
        revision_id = segments[0].revision_id
        markdown = segments_to_markdown(segments)
        blocks = [self._block_from_segment(row) for row in segments]
        payload = json.dumps({
            "blocks": [row.model_dump() for row in blocks],
            "lead_pause_ms": source.lead_pause_ms,
            "speech_speed": source.speech_speed,
            "beacon": source.beacon.model_dump(),
        }, ensure_ascii=False, sort_keys=True)
        revision = SourceRevision(
            id=revision_id, project_id=project.id,
            number=self.store.list_revisions(project.id)[0].number + 1,
            markdown=markdown, source_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            blocks=blocks, lead_pause_ms=source.lead_pause_ms,
            speech_speed=source.speech_speed, beacon=source.beacon,
            restored_from_revision_id=restored_from,
        )
        project.current_revision_id = revision.id
        project.status = (
            ProjectStatus.READY if all(row.selected_take_id for row in segments)
            else ProjectStatus.NEEDS_REVIEW
        )
        project.updated_at = utc_now()
        if run:
            self.store.finish_run(run, project, revision, segments)
        else:
            self.store.save_project_revision_if_idle(
                project, revision, segments, expected_revision_id
            )
        return ProjectDetail(**project.model_dump(), revision=revision, segments=segments)

    async def submit_run(
        self,
        project_id: str,
        segment_ids: list[str] | None = None,
        max_attempts: int | None = None,
        *,
        auto_select: bool = True,
        expected_revision_id: str | None = None,
    ) -> ProjectRun:
        project = self.get_project(project_id)
        if expected_revision_id and expected_revision_id != project.current_revision_id:
            raise ValueError("project revision changed; reload before starting a run")
        if not project.revision:
            raise ValueError("project has no source revision")
        known = {row.id for row in project.segments}
        selected = segment_ids or [row.id for row in project.segments if not row.selected_take_id]
        if not selected:
            raise ValueError("every segment already has a selected take")
        if unknown := set(selected) - known:
            raise KeyError(",".join(sorted(unknown)))
        run = ProjectRun(
            id=new_id("run"),
            project_id=project.id,
            revision_id=project.revision.id,
            segment_ids=selected,
            max_attempts=max_attempts or self.settings.project_max_attempts,
            auto_select=auto_select,
        )
        self.store.create_run_if_idle(run)
        await self.queue.put(run.id)
        return run

    def list_takes(self, project_id: str, segment_id: str) -> list[TakeDetail]:
        project = self.get_project(project_id)
        segment = (
            self.store.get_segment(project.revision.id, segment_id) if project.revision else None
        )
        if not project.revision or not segment:
            raise KeyError(segment_id)
        return [
            TakeDetail(
                **{
                    **take.model_dump(),
                    "selected": take.id == segment.selected_take_id,
                    "override_reason": (
                        segment.selection_override_reason
                        if take.id == segment.selected_take_id else None
                    ),
                },
                quality_reports=self.store.list_quality_reports(take.id),
            )
            for take in self.store.list_compatible_takes(
                project.id, segment_id, segment.text_sha256
            )
        ]

    def select_take(
        self, project_id: str, segment_id: str, take_id: str, selection: TakeSelection
    ) -> ProjectDetail:
        detail = self.get_project(project_id)
        assert detail.revision
        if selection.expected_revision_id and selection.expected_revision_id != detail.revision.id:
            raise ValueError("project revision changed; reload before selecting")
        segment = self.store.get_segment(detail.revision.id, segment_id)
        take = self.store.get_take(take_id)
        if (
            not segment
            or not take
            or take.segment_id != segment.id
            or take.project_id != detail.id
            or take.text_sha256 != segment.text_sha256
        ):
            raise KeyError(take_id)
        if take.status != TakeStatus.PASS and not selection.override:
            raise ValueError("only passing takes can be selected without an override")
        revision_id = new_id("revision")
        segments = [row.model_copy(update={"revision_id": revision_id}) for row in detail.segments]
        chosen = next(row for row in segments if row.id == segment_id)
        chosen.selected_take_id = take.id
        chosen.selection_override_reason = selection.reason.strip() if selection.override else None
        return self._publish_snapshot(
            self._project(project_id), detail.revision, segments, detail.revision.id
        )

    def assemble(
        self, project_id: str, kind: AssemblyKind, override_reason: str | None = None,
        *, revision_id: str | None = None, segment_id: str | None = None,
    ) -> Assembly:
        project = self._project(project_id)
        revision = self.store.get_revision(revision_id or project.current_revision_id)
        if not revision or revision.project_id != project_id:
            raise ValueError("project has no revision")
        revision = self._complete_revision(revision)
        segments = self.store.list_segments(revision.id)
        if segment_id:
            if kind != AssemblyKind.PREVIEW:
                raise ValueError("segment_id is only supported for preview")
            segments = [row for row in segments if row.id == segment_id]
            if not segments:
                raise KeyError(segment_id)
        selected_ids = [row.selected_take_id for row in segments if row.selected_take_id]
        takes = {take_id: self.store.get_take(take_id) for take_id in selected_ids}
        if len(takes) != len(segments) or any(value is None for value in takes.values()):
            raise ValueError("all segments must have an available selected take")
        assembly_id = new_id("assembly")
        directory = self._project_dir(project_id) / "assemblies" / assembly_id
        output = directory / "audio.wav"
        manifest_file = directory / "manifest.json"
        manifest = build_timeline(
            output,
            manifest_file,
            segments,
            takes,  # type: ignore[arg-type]
            project_id=project_id,
            revision_id=revision.id,
            projects_root=self.settings.projects_dir,
            lead_pause_ms=revision.lead_pause_ms if not segment_id else 0,
            speech_speed=revision.speech_speed,
        )
        audit_status = "pending"
        audit = {}
        if kind == AssemblyKind.FINAL:
            expected = " ".join(row.text for row in segments)
            voice = self.store.get_voice(project.voice_id)
            try:
                with self.engine_lock:
                    report = self.validator.validate(
                        output,
                        expected,
                        project.language,
                        reference_text=voice.reference_text if voice else "",
                        expected_blocks=[row.text for row in segments],
                        mock=self.settings.engine == "mock",
                    )
                audit = report.model_dump(mode="json", exclude={"take_id"})
                audit_status = "pass" if report.verdict == "pass" else "review"
            except Exception as exc:
                audit_status = "unavailable"
                audit = {"error": f"{type(exc).__name__}: {exc}"}
            if audit_status != "pass" and override_reason:
                audit_status = "overridden"
        duration, sample_rate = audio_info(output)
        assembly = Assembly(
            id=assembly_id,
            project_id=project_id,
            revision_id=revision.id,
            segment_id=segment_id,
            kind=kind,
            output_file=str(output.resolve()),
            output_sha256=manifest["output_sha256"],
            manifest_file=str(manifest_file.resolve()),
            manifest_sha256=sha256_file(manifest_file),
            duration_seconds=duration,
            sample_rate=sample_rate,
            audit_status=audit_status,
            audit=audit,
            override_reason=override_reason,
        )
        return self.store.save_assembly(assembly)

    def bundle(self, assembly_id: str) -> tuple[Assembly, bytes]:
        assembly = self.store.get_assembly(assembly_id)
        if not assembly:
            raise KeyError(assembly_id)
        if assembly.segment_id is not None:
            raise ValueError("partial previews cannot be exported as a full score bundle")
        revision = self.store.get_revision(assembly.revision_id)
        if not revision or revision.project_id != assembly.project_id:
            raise ValueError("assembly revision is unavailable")
        revision = self._complete_revision(revision)
        audio, _ = read_project_asset(
            Path(assembly.output_file), assembly.output_sha256,
            self.settings.projects_dir, assembly.project_id, label="assembly audio",
        )
        manifest, _ = read_project_asset(
            Path(assembly.manifest_file), assembly.manifest_sha256,
            self.settings.projects_dir, assembly.project_id, label="assembly manifest",
        )
        score = json.dumps(
            revision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        ).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("preview.wav", audio)
            archive.writestr("manifest.json", manifest)
            archive.writestr("score.md", revision.markdown.encode())
            archive.writestr("score.json", score)
        return assembly, buffer.getvalue()

    def _verify_beacon(self, project_id: str, asset_id: str | None) -> tuple[bytes, str]:
        if not asset_id:
            raise ValueError("beacon asset_id is required")
        directory = self.settings.projects_dir / project_id / "beacon"
        manifest_path = directory / "manifest.json"
        try:
            if directory.is_symlink() or manifest_path.is_symlink():
                raise ValueError("beacon path must not contain symbolic links")
            descriptor = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("beacon manifest is not a regular file")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    manifest = json.load(stream)
            finally:
                os.close(descriptor)
            mime_type = manifest["mime_type"]
            digest = manifest["sha256"]
            if digest != asset_id or mime_type not in {
                "audio/wav", "audio/x-wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4"
            }:
                raise ValueError("beacon manifest does not match asset")
            candidates = list(directory.glob("audio.*"))
            if len(candidates) != 1:
                raise ValueError("beacon audio is unavailable")
            content, _ = read_project_asset(
                candidates[0], digest, self.settings.projects_dir, project_id, label="beacon audio"
            )
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("beacon audio is unavailable") from exc
        return content, mime_type

    def beacon_audio(self, project_id: str) -> tuple[bytes, str]:
        project = self._project(project_id)
        revision = self.store.get_revision(project.current_revision_id)
        if not revision or not revision.beacon.asset_id:
            raise KeyError("beacon")
        return self._verify_beacon(project_id, revision.beacon.asset_id)

    async def _run(self) -> None:
        while True:
            run_id = await self.queue.get()
            self._active_run_id = run_id
            try:
                await self._execute_run(run_id)
            finally:
                self._active_run_id = None
                self.queue.task_done()

    async def _execute_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if not run:
            return
        project = self._project(run.project_id)
        revision = self.store.get_revision(run.revision_id)
        voice = self.store.get_voice(project.voice_id)
        if not revision or not voice:
            run.status = RunStatus.FAILED
            run.error = "project revision or voice is unavailable"
            run.finished_at = utc_now()
            project.status = ProjectStatus.NEEDS_REVIEW
            project.updated_at = utc_now()
            self.store.save_terminal_run_and_project(run, project)
            return
        run.status = RunStatus.RUNNING
        run.started_at = utc_now()
        self.store.save_run(run)
        project.status = ProjectStatus.GENERATING
        self.store.save_project(project)
        needs_review = False
        auto_selections: dict[str, str] = {}
        try:
            pending = {}
            for segment_id in run.segment_ids:
                segment = self.store.get_segment(revision.id, segment_id)
                if not segment:
                    raise RuntimeError(f"segment disappeared: {segment_id}")
                pending[segment_id] = segment
            total = len(pending)
            for _ in range(run.max_attempts):
                generated = []
                for segment in pending.values():
                    existing = self.store.list_compatible_takes(
                        project.id, segment.id, segment.text_sha256
                    )
                    attempt = max((row.attempt for row in existing), default=0) + 1
                    take, technical = await self._render_take(
                        project, revision, segment, voice, attempt
                    )
                    generated.append((segment, take, technical))
                if self.settings.validator_enabled and self.settings.engine == "qwen":
                    await asyncio.to_thread(self._unload_locked)
                try:
                    reports = await asyncio.to_thread(
                        self._validate_batch_locked,
                        [
                            ValidationItem(
                                audio=Path(take.trimmed_file),
                                expected=segment.text,
                                language=project.language,
                                reference=Path(voice.reference_file),
                                reference_text=voice.reference_text,
                                expected_blocks=(segment.text,),
                            )
                            for segment, take, _ in generated
                        ],
                    )
                except Exception as exc:
                    reports = [
                        ValidationResult(
                            content=QualityReport(
                                id=new_id("qc"),
                                take_id=take.id,
                                validator="qwen3-asr-0.6b+forced-aligner-0.6b",
                                verdict="unavailable",
                                reasons=[f"{type(exc).__name__}: {exc}"],
                            ),
                            identity=QualityReport(
                                id=new_id("qc"),
                                take_id=take.id,
                                validator="ecapa-speaker-window-v1",
                                verdict="unavailable",
                                reasons=[f"{type(exc).__name__}: {exc}"],
                            ),
                        )
                        for _, take, _ in generated
                    ]
                next_pending = {}
                for (segment, take, technical), validation in zip(generated, reports, strict=True):
                    content = validation.content
                    content.take_id = take.id
                    self.store.save_quality_report(content)
                    if validation.identity:
                        validation.identity.take_id = take.id
                        candidate = self.store.get_identity_calibration(voice.id, project.language)
                        calibration = self.store.get_identity_calibration(
                            voice.id,
                            project.language,
                            validation.identity.validator,
                            validation.identity.validator_model_sha256,
                        )
                        if calibration and validation.identity.identity_windows:
                            validation.identity.calibration_id = calibration.id
                            validation.identity.reasons = [f"calibration {calibration.id} applied"]
                            median = validation.identity.identity_median
                            minimum = validation.identity.identity_min
                            failures = []
                            if median is not None and median < calibration.min_median_score:
                                failures.append(
                                    "median speaker score is below calibrated threshold"
                                )
                            if minimum is not None and minimum < calibration.min_window_score:
                                failures.append("a speaker window is below calibrated threshold")
                            validation.identity.reasons.extend(failures)
                            validation.identity.verdict = "retry" if failures else "pass"
                        elif candidate:
                            validation.identity.reasons = [
                                "calibration provenance does not match this scorer/model; "
                                "score is advisory"
                            ]
                        self.store.save_quality_report(validation.identity)
                    verdict = technical.verdict
                    if verdict == "pass":
                        verdict = content.verdict
                    if (
                        verdict == "pass"
                        and validation.identity
                        and validation.identity.verdict != "pass"
                    ):
                        verdict = validation.identity.verdict
                    take.status = {
                        "pass": TakeStatus.PASS,
                        "retry": TakeStatus.RETRY,
                        "review": TakeStatus.NEEDS_REVIEW,
                        "unavailable": TakeStatus.NEEDS_REVIEW,
                    }[verdict]
                    self.store.save_take(take)
                    if verdict == "pass":
                        if run.auto_select:
                            auto_selections[segment.id] = take.id
                    elif verdict == "retry":
                        next_pending[segment.id] = segment
                    else:
                        needs_review = True
                pending = next_pending
                run.progress = (total - len(pending)) / total
                self.store.save_run(run)
                if not pending:
                    break
            if pending:
                needs_review = True
                for segment in pending.values():
                    for take in self.store.list_compatible_takes(
                        project.id, segment.id, segment.text_sha256
                    ):
                        if take.status == TakeStatus.RETRY:
                            take.status = TakeStatus.NEEDS_REVIEW
                            self.store.save_take(take)
            run.status = RunStatus.NEEDS_REVIEW if needs_review else RunStatus.COMPLETE
        except asyncio.CancelledError:
            run.status = RunStatus.FAILED
            run.error = "The process stopped before this run completed."
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = utc_now()
            project = self._project(project.id)
            selected = self.store.list_segments(run.revision_id)
            if auto_selections:
                revision_id = new_id("revision")
                snapshot = [row.model_copy(update={"revision_id": revision_id}) for row in selected]
                for row in snapshot:
                    if row.id in auto_selections:
                        row.selected_take_id = auto_selections[row.id]
                        row.selection_override_reason = None
                self._publish_snapshot(
                    project, self._complete_revision(revision), snapshot, run.revision_id,
                    run=run,
                )
            else:
                project.status = (
                    ProjectStatus.READY
                    if selected and all(row.selected_take_id for row in selected)
                    else ProjectStatus.NEEDS_REVIEW
                )
                project.updated_at = utc_now()
                self.store.finish_run(run, project)

    async def _render_take(self, project, revision, segment, voice, attempt):
        seed = deterministic_seed(project.project_seed, segment.id, attempt)
        take_id = new_id("take")
        directory = self._project_dir(project.id) / "takes" / segment.id / take_id
        raw = directory / "raw.wav"
        trimmed = directory / "speech.wav"
        request = SynthesisRequest(
            title=f"{project.title} · {segment.position + 1}",
            voice_id=voice.id,
            language=project.language,
            segments=[ScoreSegment(id=segment.id, text=segment.text)],
            seed=seed,
            sampling=project.sampling,
        )
        metrics = await asyncio.to_thread(
            self._render_locked,
            request,
            voice,
            raw,
            lambda _: None,
            lambda: False,
        )
        trim_start, trim_end, duration = trim_speech_edges(
            raw,
            trimmed,
            threshold_db=self.settings.trim_threshold_db,
            padding_ms=self.settings.trim_padding_ms,
        )
        take = Take(
            id=take_id,
            project_id=project.id,
            revision_id=revision.id,
            segment_id=segment.id,
            attempt=attempt,
            seed=seed,
            raw_file=str(raw.resolve()),
            trimmed_file=str(trimmed.resolve()),
            raw_sha256=sha256_file(raw),
            trimmed_sha256=sha256_file(trimmed),
            duration_seconds=duration,
            trim_start_ms=trim_start,
            trim_end_ms=trim_end,
            trim_threshold_db=self.settings.trim_threshold_db,
            trim_padding_ms=self.settings.trim_padding_ms,
            voice_id=voice.id,
            voice_reference_sha256=voice.reference_sha256,
            model=metrics.model,
            text_sha256=segment.text_sha256,
            sampling=project.sampling,
            baseline_speed=project.baseline_speed,
            provenance=project.provenance,
        )
        technical = self._technical_report(take)
        self.store.save_take(take)
        self.store.save_quality_report(technical)
        return take, technical

    def _render_locked(self, request, voice, output, progress, cancelled):
        with self.engine_lock:
            return self.engine.render_synthesis(request, voice, output, progress, cancelled)

    def _unload_locked(self) -> None:
        with self.engine_lock:
            self.engine.unload()

    def _validate_batch_locked(self, items):
        with self.engine_lock:
            return self.validator.validate_batch(items, mock=self.settings.engine == "mock")

    def _technical_report(self, take: Take) -> QualityReport:
        audio, _ = sf.read(take.trimmed_file, dtype="float32", always_2d=False)
        array = np.asarray(audio)
        reasons = []
        if take.duration_seconds < 0.2:
            reasons.append("speech is shorter than 200ms")
        if not np.isfinite(array).all():
            reasons.append("audio contains non-finite samples")
        if len(array) and float(np.mean(np.abs(array) >= 0.999)) > 0.01:
            reasons.append("more than 1% of samples are clipped")
        return QualityReport(
            id=new_id("qc"),
            take_id=take.id,
            validator="technical-audio-v1",
            verdict="retry" if reasons else "pass",
            reasons=reasons,
        )

    def _project(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(project_id)
        return project

    def _project_dir(self, project_id: str) -> Path:
        directory = self.settings.projects_dir / project_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory
