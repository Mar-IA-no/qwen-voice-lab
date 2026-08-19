from __future__ import annotations

import asyncio
import shutil
import threading
import uuid
from pathlib import Path

from .config import Settings
from .engine import RenderCancelled, audio_info, build_engine, sha256_file
from .models import (
    Comparison,
    ComparisonRequest,
    DesignRequest,
    Job,
    JobKind,
    JobStatus,
    ScoreSegment,
    SynthesisRequest,
    Voice,
    VoiceKind,
    VoiceView,
    utc_now,
)
from .prosody import ProsodyRegistry
from .storage import Store


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class JobManager:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.prosody = ProsodyRegistry(settings)
        self.engine = build_engine(settings, self.prosody)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.cancelled: set[str] = set()
        self._promotion_lock = threading.RLock()
        self.engine_lock = threading.RLock()
        self._worker: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None

    async def start(self) -> None:
        for job in self.store.list_jobs(500):
            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                job.status = JobStatus.FAILED
                job.error = "The process stopped before this job completed."
                job.finished_at = utc_now()
                self.store.save_job(job)
        self._worker = asyncio.create_task(self._run(), name="qvl-serial-worker")
        self._sweeper = asyncio.create_task(self._sweep_idle_model(), name="qvl-model-sweeper")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
        self.engine.unload()

    async def submit_synthesis(self, request: SynthesisRequest) -> Job:
        voice = self.store.get_voice(request.voice_id)
        if not voice:
            raise KeyError(request.voice_id)
        self.prosody.validate_score(voice, request.language, request.segments)
        job = Job(
            id=new_id("job"),
            kind=JobKind.SYNTHESIS,
            title=request.title,
            request=request.model_dump(mode="json"),
        )
        self.store.save_job(job)
        await self.queue.put(job.id)
        return job

    def voice_view(self, voice: Voice) -> VoiceView:
        return VoiceView.model_validate(voice).model_copy(
            update={"prosody_profile": self.prosody.view_for(voice)}
        )

    async def submit_design(self, request: DesignRequest) -> Job:
        job = Job(
            id=new_id("job"),
            kind=JobKind.DESIGN,
            title=f"Design · {request.name}",
            request=request.model_dump(mode="json"),
        )
        self.store.save_job(job)
        await self.queue.put(job.id)
        return job

    async def submit_comparison(self, request: ComparisonRequest) -> Comparison:
        missing = [voice_id for voice_id in request.voice_ids if not self.store.get_voice(voice_id)]
        if missing:
            raise KeyError(",".join(missing))
        comparison_id = new_id("cmp")
        jobs = []
        for voice_id in request.voice_ids:
            job = await self.submit_synthesis(
                SynthesisRequest(
                    title=f"{request.title} · {self.store.get_voice(voice_id).name}",
                    voice_id=voice_id,
                    language=request.language,
                    segments=[ScoreSegment(id="p01", text=request.text)],
                    seed=request.seed,
                    comparison_id=comparison_id,
                )
            )
            jobs.append(job.id)
        comparison = Comparison(
            id=comparison_id,
            title=request.title,
            voice_ids=request.voice_ids,
            job_ids=jobs,
            language=request.language,
            text=request.text,
            seed=request.seed,
        )
        self.store.save_comparison(comparison)
        return comparison

    def cancel(self, job_id: str) -> Job:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.status in {JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        was_running = job.status == JobStatus.RUNNING
        self.cancelled.add(job_id)
        job.status = JobStatus.CANCELLED
        job.finished_at = utc_now()
        if was_running and (cancel_active := getattr(self.engine, "cancel_active", None)):
            try:
                cancel_active()
            except Exception as exc:
                job.error = f"Worker cleanup warning: {type(exc).__name__}: {exc}"
        self.store.save_job(job)
        return job

    def promote_design(self, job_id: str) -> Voice:
        with self._promotion_lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job.kind != JobKind.DESIGN:
                raise ValueError("Only VoiceDesign jobs can become reusable voices.")
            if job.status != JobStatus.COMPLETE or not job.output_file:
                raise ValueError("The VoiceDesign sample must complete before promotion.")
            if job.result_voice_id:
                voice = self.store.get_voice(job.result_voice_id)
                if voice:
                    return voice
            output = Path(job.output_file).resolve()
            if self.settings.renders_dir.resolve() not in output.parents or not output.is_file():
                raise ValueError("The VoiceDesign sample audio is unavailable.")
            request = DesignRequest.model_validate(job.request)
            voice_id = job.result_voice_id or f"voice_design_{job.id.removeprefix('job_')}"
            voice_dir = self.settings.voices_dir / voice_id
            voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            reference = voice_dir / "reference.wav"
            shutil.copy2(output, reference)
            reference.chmod(0o600)
            duration, _ = audio_info(reference)
            voice = self.store.save_voice(
                Voice(
                    id=voice_id,
                    name=request.name,
                    description=request.description,
                    kind=VoiceKind.DESIGNED,
                    language_hint="multilingual",
                    reference_text=request.sample_text,
                    reference_file=str(reference.resolve()),
                    reference_sha256=sha256_file(reference),
                    duration_seconds=duration,
                    design_instruction=request.instruction,
                    tags=["voice-design", request.language],
                )
            )
            job.result_voice_id = voice.id
            self.store.save_job(job)
            return voice

    async def _run(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._execute(job_id)
            finally:
                self.queue.task_done()

    async def _sweep_idle_model(self) -> None:
        while True:
            await asyncio.sleep(min(60, self.settings.model_idle_seconds))
            await asyncio.to_thread(
                self.engine.unload_if_idle, self.settings.model_idle_seconds
            )

    async def _execute(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job or job_id in self.cancelled or job.status == JobStatus.CANCELLED:
            return
        job.status = JobStatus.RUNNING
        job.started_at = utc_now()
        job.progress = 0
        self.store.save_job(job)

        def progress(value: float) -> None:
            current = self.store.get_job(job_id)
            if current and current.status == JobStatus.RUNNING:
                current.progress = max(current.progress, min(1.0, value))
                self.store.save_job(current)

        def cancelled() -> bool:
            return job_id in self.cancelled

        try:
            if job.kind == JobKind.SYNTHESIS:
                request = SynthesisRequest.model_validate(job.request)
                voice = self.store.get_voice(request.voice_id)
                if not voice:
                    raise RuntimeError("voice was removed before rendering")
                output = self.settings.renders_dir / f"{job.id}.wav"
                metrics = await asyncio.to_thread(
                    self._render_synthesis_locked,
                    request,
                    voice,
                    output,
                    progress,
                    cancelled,
                )
                job.output_file = str(output.resolve())
                job.metrics = metrics
            else:
                request = DesignRequest.model_validate(job.request)
                output = self.settings.renders_dir / f"{job.id}.wav"
                metrics = await asyncio.to_thread(
                    self._render_design_locked, request, output, progress, cancelled
                )
                job.output_file = str(output.resolve())
                job.metrics = metrics
            if cancelled():
                raise RenderCancelled("render cancelled")
            job.status = JobStatus.COMPLETE
            job.progress = 1
        except RenderCancelled:
            job.status = JobStatus.CANCELLED
            job.error = None
        except Exception as exc:
            if cancelled():
                job.status = JobStatus.CANCELLED
                job.error = None
            else:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = utc_now()
            self.store.save_job(job)

    def _render_synthesis_locked(self, request, voice, output, progress, cancelled):
        with self.engine_lock:
            return self.engine.render_synthesis(request, voice, output, progress, cancelled)

    def _render_design_locked(self, request, output, progress, cancelled):
        with self.engine_lock:
            return self.engine.render_design(request, output, progress, cancelled)
