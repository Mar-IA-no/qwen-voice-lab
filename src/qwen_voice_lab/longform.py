from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio_pipeline import build_timeline, trim_speech_edges
from .config import Settings
from .editorial import compile_markdown, reconcile_segments
from .engine import audio_info, sha256_file
from .models import (
    Assembly,
    AssemblyKind,
    Project,
    ProjectCreate,
    ProjectDetail,
    ProjectRun,
    ProjectStatus,
    QualityReport,
    RevisionCreate,
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
from .quality import ContentValidator, ValidationResult
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
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self.validator.close()

    def create_project(self, request: ProjectCreate) -> ProjectDetail:
        if not self.store.get_voice(request.voice_id):
            raise KeyError(request.voice_id)
        blocks = compile_markdown(request.markdown)
        project = Project(
            id=new_id("project"),
            title=request.title,
            voice_id=request.voice_id,
            language=request.language,
            project_seed=request.project_seed,
            sampling=request.sampling,
        )
        return self._persist_revision(project, request.markdown, blocks, [], [])

    def add_revision(self, project_id: str, request: RevisionCreate) -> ProjectDetail:
        project = self._project(project_id)
        if any(
            run.status in {RunStatus.QUEUED, RunStatus.RUNNING}
            for run in self.store.list_runs(project_id)
        ):
            raise ValueError("wait for the active project run before saving a revision")
        blocks = compile_markdown(request.markdown)
        revisions = self.store.list_revisions(project_id)
        previous = (
            self.store.list_segments(project.current_revision_id)
            if project.current_revision_id
            else []
        )
        return self._persist_revision(project, request.markdown, blocks, previous, revisions)

    def _persist_revision(
        self, project: Project, markdown: str, blocks, previous, revisions
    ) -> ProjectDetail:
        revision = SourceRevision(
            id=new_id("revision"),
            project_id=project.id,
            number=(revisions[0].number + 1) if revisions else 1,
            markdown=markdown,
            source_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )
        segments = reconcile_segments(project.id, revision.id, blocks, previous)
        project.current_revision_id = revision.id
        project.status = (
            ProjectStatus.READY
            if segments and all(row.selected_take_id for row in segments)
            else ProjectStatus.DRAFT
        )
        project.updated_at = utc_now()
        self.store.save_project_revision(project, revision, segments)
        return ProjectDetail(**project.model_dump(), revision=revision, segments=segments)

    def get_project(self, project_id: str) -> ProjectDetail:
        project = self._project(project_id)
        revision = (
            self.store.get_revision(project.current_revision_id)
            if project.current_revision_id
            else None
        )
        segments = self.store.list_segments(revision.id) if revision else []
        return ProjectDetail(**project.model_dump(), revision=revision, segments=segments)

    async def submit_run(
        self,
        project_id: str,
        segment_ids: list[str] | None = None,
        max_attempts: int | None = None,
        *,
        auto_select: bool = True,
    ) -> ProjectRun:
        project = self.get_project(project_id)
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
                **take.model_dump(),
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
        for previous in self.store.list_compatible_takes(
            detail.id, segment.id, segment.text_sha256
        ):
            if previous.selected:
                previous.selected = False
                self.store.save_take(previous)
        take.selected = True
        if selection.override:
            take.status = TakeStatus.OVERRIDDEN
            take.override_reason = selection.reason.strip() if selection.reason else None
        self.store.save_take(take)
        segment.selected_take_id = take.id
        self.store.save_segment(segment)
        project = self._project(project_id)
        current = self.store.list_segments(detail.revision.id)
        project.status = (
            ProjectStatus.READY
            if all(row.selected_take_id for row in current)
            else ProjectStatus.NEEDS_REVIEW
        )
        project.updated_at = utc_now()
        self.store.save_project(project)
        return self.get_project(project_id)

    def assemble(
        self, project_id: str, kind: AssemblyKind, override_reason: str | None = None
    ) -> Assembly:
        detail = self.get_project(project_id)
        if not detail.revision:
            raise ValueError("project has no revision")
        selected_ids = [row.selected_take_id for row in detail.segments if row.selected_take_id]
        takes = {take_id: self.store.get_take(take_id) for take_id in selected_ids}
        if len(takes) != len(detail.segments) or any(value is None for value in takes.values()):
            raise ValueError("all segments must have an available selected take")
        assembly_id = new_id("assembly")
        directory = self._project_dir(project_id) / "assemblies" / assembly_id
        output = directory / "audio.wav"
        manifest_file = directory / "manifest.json"
        manifest = build_timeline(
            output,
            manifest_file,
            detail.segments,
            takes,  # type: ignore[arg-type]
            project_id=project_id,
            revision_id=detail.revision.id,
            projects_root=self.settings.projects_dir,
        )
        audit_status = "pending"
        audit = {}
        if kind == AssemblyKind.FINAL:
            expected = " ".join(row.text for row in detail.segments)
            try:
                with self.engine_lock:
                    report = self.validator.validate(
                        output,
                        expected,
                        detail.language,
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
            revision_id=detail.revision.id,
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

    async def _run(self) -> None:
        while True:
            run_id = await self.queue.get()
            try:
                await self._execute_run(run_id)
            finally:
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
                            (
                                Path(take.trimmed_file),
                                segment.text,
                                project.language,
                                Path(voice.reference_file),
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
                            self.select_take(
                                project.id,
                                segment.id,
                                take.id,
                                TakeSelection(override=False),
                            )
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
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = utc_now()
            project = self._project(project.id)
            selected = self.store.list_segments(run.revision_id)
            if run.status in {RunStatus.NEEDS_REVIEW, RunStatus.FAILED}:
                project.status = ProjectStatus.NEEDS_REVIEW
            elif selected and all(row.selected_take_id for row in selected):
                project.status = ProjectStatus.READY
            else:
                project.status = ProjectStatus.NEEDS_REVIEW
            project.updated_at = utc_now()
            self.store.save_terminal_run_and_project(run, project)

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
            voice_id=voice.id,
            voice_reference_sha256=voice.reference_sha256,
            model=metrics.model,
            text_sha256=segment.text_sha256,
            sampling=project.sampling,
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
