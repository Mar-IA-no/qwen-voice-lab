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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .archive import list_archive_assets, resolve_archive_asset
from .config import Settings, get_settings
from .engine import QwenEngine, audio_info, sha256_file
from .models import (
    ArchiveAsset,
    AuthRequest,
    Capabilities,
    Comparison,
    ComparisonDetail,
    ComparisonRequest,
    DesignRequest,
    Job,
    JobView,
    Language,
    SynthesisRequest,
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        yield
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
            "authenticated": hmac.compare_digest(
                supplied, session_digest(settings.access_token)
            ),
        }

    @app.post("/api/auth/session")
    def login(payload: AuthRequest) -> JSONResponse:
        if settings.access_token and not hmac.compare_digest(
            payload.token, settings.access_token
        ):
            raise HTTPException(401, "Invalid access token.")
        response = JSONResponse(
            {"required": bool(settings.access_token), "authenticated": True}
        )
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
