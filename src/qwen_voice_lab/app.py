from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .archive import list_archive_assets, resolve_archive_asset
from .audio_pipeline import read_project_asset, read_take_asset
from .config import Settings, get_settings
from .engine import QwenEngine, audio_info, sha256_file
from .longform import LongFormManager
from .models import (
    ArchiveAsset,
    Assembly,
    AssemblyKind,
    AssemblyRequest,
    AssemblyView,
    AuthRequest,
    Capabilities,
    Comparison,
    ComparisonDetail,
    ComparisonRequest,
    DesignRequest,
    IdentityCalibration,
    IdentityCalibrationCreate,
    Job,
    JobView,
    Language,
    Project,
    ProjectCreate,
    ProjectDetail,
    ProjectRun,
    RevisionCreate,
    SynthesisRequest,
    TakeDetail,
    TakeSelection,
    Voice,
    VoiceKind,
    VoiceView,
)
from .service import JobManager, new_id
from .starter import seed_starter_voices
from .storage import Store

ALLOWED_AUDIO_TYPES = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
}


def session_digest(access_token: str) -> str:
    return hmac.new(
        access_token.encode("utf-8"), b"qwen-voice-lab-session-v1", hashlib.sha256
    ).hexdigest()


class AccessTokenMiddleware:
    def __init__(self, app: ASGIApp, access_token: str):
        self.app = app
        self.access_token = access_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.access_token:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api/") or path in {
            "/api/health",
            "/api/auth/session",
            "/api/auth/status",
        }:
            await self.app(scope, receive, send)
            return
        supplied = Request(scope).cookies.get("qvl_session", "")
        if hmac.compare_digest(supplied, session_digest(self.access_token)):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": "Authentication required."}, status_code=401)
        await response(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare()
    store = Store(settings)
    seed_starter_voices(settings, store)
    manager = JobManager(settings, store)
    projects = LongFormManager(settings, store, manager.engine, manager.engine_lock)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            await projects.start()
            yield
        finally:
            try:
                await projects.stop()
            finally:
                await manager.stop()

    app = FastAPI(
        title="Qwen Voice Lab",
        version=__version__,
        description="Local-first Qwen3-TTS voice design and cloning studio.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.manager = manager
    app.state.projects = projects
    app.add_middleware(AccessTokenMiddleware, access_token=settings.access_token)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            f"http://127.0.0.1:{settings.port}",
            f"http://localhost:{settings.port}",
        ],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict:
        if not settings.access_token:
            return {"required": False, "authenticated": True}
        supplied = request.cookies.get("qvl_session", "")
        return {
            "required": True,
            "authenticated": hmac.compare_digest(supplied, session_digest(settings.access_token)),
        }

    @app.post("/api/auth/session")
    def login(payload: AuthRequest) -> JSONResponse:
        if settings.access_token and not hmac.compare_digest(payload.token, settings.access_token):
            raise HTTPException(401, "Invalid access token.")
        response = JSONResponse({"required": bool(settings.access_token), "authenticated": True})
        if settings.access_token:
            response.set_cookie(
                "qvl_session",
                session_digest(settings.access_token),
                httponly=True,
                secure=settings.cookie_secure,
                samesite="strict",
                max_age=86_400,
                path="/",
            )
        return response

    @app.delete("/api/auth/session")
    def logout() -> JSONResponse:
        response = JSONResponse({"required": bool(settings.access_token), "authenticated": False})
        response.delete_cookie("qvl_session", path="/")
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "engine": settings.engine,
            "queue_depth": manager.queue.qsize(),
        }

    @app.get("/api/capabilities", response_model=Capabilities)
    def capabilities() -> Capabilities:
        ready = True
        reason = None
        execution_mode = "in-process"
        worker_state = "not-applicable"
        worker_reason = None
        wrapper_verified = QwenEngine.wrapper_verified(settings)
        if settings.engine == "qwen" and settings.require_gpu_wrapper:
            runtime_status = getattr(manager.engine, "runtime_status", None)
            if runtime_status is not None:
                status = runtime_status()
                execution_mode = "wrapped-worker"
                worker_state = status["state"]
                worker_reason = status["reason"]
                wrapper_verified = status["wrapper_verified"]
                ready = status["configured"] and worker_state not in {
                    "cooldown",
                    "misconfigured",
                    "unavailable",
                }
                reason = worker_reason
            else:
                try:
                    QwenEngine.assert_wrapped(settings)
                except RuntimeError as exc:
                    ready = False
                    reason = str(exc)
        return Capabilities(
            engine=settings.engine,
            engine_ready=ready,
            engine_reason=reason,
            base_model=settings.qwen_base_model_label,
            design_model=settings.qwen_design_model_label,
            max_upload_mib=settings.max_upload_mib,
            max_text_chars=settings.max_text_chars,
            max_segments=settings.max_segments,
            max_comparison_voices=settings.max_comparison_voices,
            gpu_wrapper_required=settings.require_gpu_wrapper,
            gpu_wrapper_verified=wrapper_verified,
            gpu_execution_mode=execution_mode,
            gpu_worker_state=worker_state,
            gpu_worker_reason=worker_reason,
            local_validator_enabled=settings.validator_enabled,
            validator_models=[settings.qwen_asr_model, settings.qwen_aligner_model],
        )

    @app.get("/api/archive", response_model=list[ArchiveAsset])
    def archive() -> list[ArchiveAsset]:
        return list_archive_assets(settings.archive_dir)

    @app.get("/api/archive/{asset_id}/audio")
    def archive_audio(asset_id: str) -> FileResponse:
        path = resolve_archive_asset(settings.archive_dir, asset_id)
        if not path:
            raise HTTPException(404, "Archived audio not found.")
        return FileResponse(path)

    @app.get("/api/voices", response_model=list[VoiceView])
    def voices() -> list[VoiceView]:
        return [manager.voice_view(voice) for voice in store.list_voices()]

    @app.post("/api/voices", response_model=VoiceView, status_code=201)
    async def import_voice(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form(min_length=1, max_length=80)],
        consent_confirmed: Annotated[bool, Form()],
        description: Annotated[str, Form(max_length=500)] = "",
        language_hint: Annotated[str, Form()] = "multilingual",
        reference_text: Annotated[str, Form(max_length=4000)] = "",
        tags: Annotated[str, Form()] = "",
    ) -> VoiceView:
        if not consent_confirmed:
            raise HTTPException(422, "Permission to use this voice must be confirmed.")
        if language_hint != "multilingual" and language_hint not in {
            language.value for language in Language
        }:
            raise HTTPException(422, "Unsupported language hint.")
        suffix = ALLOWED_AUDIO_TYPES.get(file.content_type or "")
        if not suffix:
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in set(ALLOWED_AUDIO_TYPES.values()):
                raise HTTPException(415, "Upload WAV, FLAC, MP3, M4A, WebM or OGG audio.")
        temp = settings.temp_dir / f"upload_{uuid.uuid4().hex}{suffix}"
        size = 0
        limit = settings.max_upload_mib * 1024 * 1024
        try:
            with temp.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"Audio exceeds {settings.max_upload_mib} MiB.")
                    handle.write(chunk)
            temp.chmod(0o600)
            try:
                duration, _ = audio_info(temp)
            except Exception as exc:
                raise HTTPException(422, f"Audio could not be decoded: {exc}") from exc
            if duration < 1:
                raise HTTPException(422, "Reference audio must be at least one second.")
            voice_id = new_id("voice")
            voice_dir = settings.voices_dir / voice_id
            voice_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            reference = voice_dir / f"reference{suffix}"
            shutil.move(temp, reference)
            reference.chmod(0o600)
            voice = Voice(
                id=voice_id,
                name=name.strip(),
                description=description.strip(),
                kind=VoiceKind.CLONE,
                language_hint=language_hint,
                reference_text=reference_text.strip(),
                reference_file=str(reference.resolve()),
                reference_sha256=sha256_file(reference),
                duration_seconds=duration,
                tags=[row.strip() for row in tags.split(",") if row.strip()][:12],
            )
            return manager.voice_view(store.save_voice(voice))
        finally:
            temp.unlink(missing_ok=True)

    @app.get("/api/voices/{voice_id}/audio")
    def voice_audio(voice_id: str) -> FileResponse:
        voice = store.get_voice(voice_id)
        if not voice:
            raise HTTPException(404, "Voice not found.")
        path = Path(voice.reference_file).resolve()
        if settings.voices_dir.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "Reference audio is unavailable.")
        return FileResponse(path)

    @app.get(
        "/api/voices/{voice_id}/identity-calibrations",
        response_model=list[IdentityCalibration],
    )
    def identity_calibrations(voice_id: str) -> list[IdentityCalibration]:
        if not store.get_voice(voice_id):
            raise HTTPException(404, "Voice not found.")
        return store.list_identity_calibrations(voice_id)

    @app.post(
        "/api/voices/{voice_id}/identity-calibrations",
        response_model=IdentityCalibration,
        status_code=201,
    )
    def calibrate_identity(
        voice_id: str, request: IdentityCalibrationCreate
    ) -> IdentityCalibration:
        if not store.get_voice(voice_id):
            raise HTTPException(404, "Voice not found.")
        return store.save_identity_calibration(
            IdentityCalibration(
                id=f"calibration_{uuid.uuid4().hex[:16]}",
                voice_id=voice_id,
                **request.model_dump(),
            )
        )

    @app.delete("/api/voices/{voice_id}", status_code=204)
    def delete_voice(voice_id: str) -> None:
        voice = store.get_voice(voice_id)
        if not voice:
            raise HTTPException(404, "Voice not found.")
        path = Path(voice.reference_file).resolve()
        if settings.voices_dir.resolve() in path.parents:
            shutil.rmtree(path.parent)
        store.delete_voice(voice_id)

    @app.post("/api/designs", response_model=JobView, status_code=202)
    async def design_voice(request: DesignRequest) -> Job:
        if len(request.sample_text) > settings.max_text_chars:
            raise HTTPException(422, "Sample text exceeds the configured limit.")
        return await manager.submit_design(request)

    @app.post("/api/jobs", response_model=JobView, status_code=202)
    async def synthesize(request: SynthesisRequest) -> Job:
        if len(request.segments) > settings.max_segments:
            raise HTTPException(422, "Score has too many segments.")
        if sum(len(row.text) for row in request.segments) > settings.max_text_chars:
            raise HTTPException(422, "Score text exceeds the configured limit.")
        try:
            return await manager.submit_synthesis(request)
        except KeyError as exc:
            raise HTTPException(404, "Voice not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs", response_model=list[JobView])
    def jobs(limit: int = Query(default=100, ge=1, le=500)) -> list[Job]:
        return store.list_jobs(limit)

    @app.get("/api/jobs/{job_id}", response_model=JobView)
    def job(job_id: str) -> Job:
        row = store.get_job(job_id)
        if not row:
            raise HTTPException(404, "Job not found.")
        return row

    @app.post("/api/jobs/{job_id}/promote", response_model=VoiceView)
    def promote_design(job_id: str) -> VoiceView:
        try:
            return manager.voice_view(manager.promote_design(job_id))
        except KeyError as exc:
            raise HTTPException(404, "Job not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/jobs/{job_id}", response_model=JobView)
    def cancel_job(job_id: str) -> Job:
        try:
            return manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found.") from exc

    def rendered_audio(job_id: str) -> tuple[Job, Path]:
        row = store.get_job(job_id)
        if not row or not row.output_file:
            raise HTTPException(404, "Rendered audio is unavailable.")
        path = Path(row.output_file).resolve()
        if settings.renders_dir.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "Rendered audio is unavailable.")
        return row, path

    def rendered_filename(row: Job) -> str:
        stem = re.sub(r"[^\w .-]+", "_", row.title, flags=re.UNICODE).strip(" ._")
        return f"{stem or row.id}.wav"

    @app.get("/api/jobs/{job_id}/audio")
    def job_audio(job_id: str) -> FileResponse:
        row, path = rendered_audio(job_id)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=rendered_filename(row),
            content_disposition_type="inline",
        )

    @app.get("/api/jobs/{job_id}/download")
    def job_download(job_id: str) -> FileResponse:
        row, path = rendered_audio(job_id)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=rendered_filename(row),
            content_disposition_type="attachment",
        )

    @app.post("/api/comparisons", response_model=Comparison, status_code=202)
    async def compare(request: ComparisonRequest) -> Comparison:
        if len(request.voice_ids) > settings.max_comparison_voices:
            raise HTTPException(422, "Too many voices in comparison.")
        try:
            return await manager.submit_comparison(request)
        except KeyError as exc:
            raise HTTPException(404, f"Voice not found: {exc.args[0]}") from exc

    @app.get("/api/comparisons/{comparison_id}", response_model=ComparisonDetail)
    def comparison(comparison_id: str) -> ComparisonDetail:
        row = store.get_comparison(comparison_id)
        if not row:
            raise HTTPException(404, "Comparison not found.")
        return ComparisonDetail(
            **row.model_dump(mode="json"),
            jobs=[
                JobView.model_validate(job) if job else None
                for job_id in row.job_ids
                if (job := store.get_job(job_id)) is not None
            ],
        )

    @app.get("/api/catalog/export")
    def export_catalog() -> dict:
        return store.export_catalog()

    @app.get("/api/projects", response_model=list[Project])
    def list_projects() -> list[Project]:
        return store.list_projects()

    @app.post("/api/projects", response_model=ProjectDetail, status_code=201)
    def create_project(request: ProjectCreate) -> ProjectDetail:
        try:
            return projects.create_project(request)
        except KeyError as exc:
            raise HTTPException(404, "Voice not found.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{project_id}", response_model=ProjectDetail)
    def project_detail(project_id: str) -> ProjectDetail:
        try:
            return projects.get_project(project_id)
        except KeyError as exc:
            raise HTTPException(404, "Project not found.") from exc

    @app.post("/api/projects/{project_id}/revisions", response_model=ProjectDetail, status_code=201)
    def create_revision(project_id: str, request: RevisionCreate) -> ProjectDetail:
        try:
            return projects.add_revision(project_id, request)
        except KeyError as exc:
            raise HTTPException(404, "Project not found.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{project_id}/runs", response_model=list[ProjectRun])
    def project_runs(project_id: str) -> list[ProjectRun]:
        if not store.get_project(project_id):
            raise HTTPException(404, "Project not found.")
        return store.list_runs(project_id)

    @app.post("/api/projects/{project_id}/runs", response_model=ProjectRun, status_code=202)
    async def create_project_run(project_id: str) -> ProjectRun:
        try:
            return await projects.submit_run(project_id)
        except KeyError as exc:
            raise HTTPException(404, "Project or segment not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/project-runs/{run_id}", response_model=ProjectRun)
    def project_run(run_id: str) -> ProjectRun:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Project run not found.")
        return run

    @app.get(
        "/api/projects/{project_id}/segments/{segment_id}/takes",
        response_model=list[TakeDetail],
    )
    def segment_takes(project_id: str, segment_id: str) -> list[TakeDetail]:
        try:
            return projects.list_takes(project_id, segment_id)
        except KeyError as exc:
            raise HTTPException(404, "Project or segment not found.") from exc

    @app.post(
        "/api/projects/{project_id}/segments/{segment_id}/takes",
        response_model=ProjectRun,
        status_code=202,
    )
    async def generate_manual_take(project_id: str, segment_id: str) -> ProjectRun:
        try:
            return await projects.submit_run(
                project_id, [segment_id], max_attempts=1, auto_select=False
            )
        except KeyError as exc:
            raise HTTPException(404, "Project or segment not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/api/projects/{project_id}/segments/{segment_id}/takes/{take_id}/select",
        response_model=ProjectDetail,
    )
    def select_project_take(
        project_id: str, segment_id: str, take_id: str, selection: TakeSelection
    ) -> ProjectDetail:
        try:
            return projects.select_take(project_id, segment_id, take_id, selection)
        except KeyError as exc:
            raise HTTPException(404, "Project, segment, or take not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def take_response(take_id: str, raw: bool, *, attachment: bool) -> Response:
        take = store.get_take(take_id)
        if not take:
            raise HTTPException(404, "Take not found.")
        try:
            asset, _ = read_take_asset(take, settings.projects_dir, raw=raw)
        except ValueError as exc:
            raise HTTPException(404, "Take audio is unavailable.") from exc
        return Response(
            content=asset,
            media_type="audio/wav",
            headers={
                "Content-Disposition": (
                    f'{"attachment" if attachment else "inline"}; '
                    f'filename="{take.id}{"-raw" if raw else ""}.wav"'
                )
            },
        )

    @app.get("/api/takes/{take_id}/audio")
    def take_audio(take_id: str, raw: bool = False) -> Response:
        return take_response(take_id, raw, attachment=False)

    @app.get("/api/takes/{take_id}/download")
    def take_download(take_id: str, raw: bool = False) -> Response:
        return take_response(take_id, raw, attachment=True)

    def create_assembly(project_id: str, kind: AssemblyKind, request: AssemblyRequest) -> Assembly:
        try:
            return projects.assemble(project_id, kind, request.override_reason)
        except KeyError as exc:
            raise HTTPException(404, "Project not found.") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/projects/{project_id}/preview", response_model=AssemblyView, status_code=201)
    def project_preview(project_id: str, request: AssemblyRequest) -> Assembly:
        return create_assembly(project_id, AssemblyKind.PREVIEW, request)

    @app.post(
        "/api/projects/{project_id}/assemblies",
        response_model=AssemblyView,
        status_code=201,
    )
    def project_assembly(project_id: str, request: AssemblyRequest) -> Assembly:
        return create_assembly(project_id, AssemblyKind.FINAL, request)

    @app.get("/api/projects/{project_id}/assemblies", response_model=list[AssemblyView])
    def project_assemblies(project_id: str) -> list[Assembly]:
        if not store.get_project(project_id):
            raise HTTPException(404, "Project not found.")
        return store.list_assemblies(project_id)

    def assembly_asset(assembly_id: str, field: str) -> tuple[Assembly, bytes]:
        assembly = store.get_assembly(assembly_id)
        if not assembly:
            raise HTTPException(404, "Assembly not found.")
        expected_sha256 = (
            assembly.output_sha256 if field == "output_file" else assembly.manifest_sha256
        )
        try:
            content, _ = read_project_asset(
                Path(getattr(assembly, field)),
                expected_sha256,
                settings.projects_dir,
                assembly.project_id,
                label="assembly asset",
            )
        except ValueError as exc:
            raise HTTPException(404, "Assembly asset is unavailable.") from exc
        return assembly, content

    def authenticated_audio_response(
        request: Request, content: bytes, *, filename: str | None = None
    ) -> Response:
        headers = {"Accept-Ranges": "bytes"}
        if filename:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        requested_range = request.headers.get("range")
        if not requested_range:
            return Response(content=content, media_type="audio/wav", headers=headers)
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range.strip())
        size = len(content)
        if not match or not size:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        start_text, end_text = match.groups()
        if not start_text:
            suffix = int(end_text or "0")
            if suffix <= 0:
                return Response(
                    status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{size}"},
                )
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = min(int(end_text), size - 1) if end_text else size - 1
            if start >= size or end < start:
                return Response(
                    status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{size}"},
                )
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(
            content=content[start : end + 1],
            status_code=206,
            media_type="audio/wav",
            headers=headers,
        )

    @app.get("/api/assemblies/{assembly_id}/audio")
    def assembly_audio(assembly_id: str, request: Request) -> Response:
        _, content = assembly_asset(assembly_id, "output_file")
        return authenticated_audio_response(request, content)

    @app.get("/api/assemblies/{assembly_id}/download")
    def assembly_download(assembly_id: str, request: Request) -> Response:
        assembly, content = assembly_asset(assembly_id, "output_file")
        return authenticated_audio_response(
            request,
            content,
            filename=f"{assembly.project_id}-{assembly.kind}.wav",
        )

    @app.get("/api/assemblies/{assembly_id}/manifest")
    def assembly_manifest(assembly_id: str) -> Response:
        _, content = assembly_asset(assembly_id, "manifest_file")
        return Response(content=content, media_type="application/json")

    source_frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    packaged_frontend = Path(__file__).resolve().parent / "static"
    frontend = packaged_frontend if packaged_frontend.is_dir() else source_frontend
    app.state.frontend_path = frontend
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("qwen_voice_lab.app:app", host=settings.host, port=settings.port, reload=False)
