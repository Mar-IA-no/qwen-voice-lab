from __future__ import annotations

import json
import sqlite3
import threading
from typing import TypeVar

from pydantic import BaseModel

from .config import Settings
from .models import (
    Assembly,
    Comparison,
    IdentityCalibration,
    Job,
    Project,
    ProjectRun,
    ProjectSegment,
    QualityReport,
    RunStatus,
    SourceRevision,
    Take,
    Voice,
    VoiceView,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.settings.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS voices (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparisons (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_revisions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(project_id, number),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS project_segments (
                    revision_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(revision_id, id),
                    UNIQUE(revision_id, position),
                    FOREIGN KEY(revision_id) REFERENCES source_revisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS project_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS takes (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(revision_id, segment_id, attempt),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS quality_reports (
                    id TEXT PRIMARY KEY,
                    take_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(take_id) REFERENCES takes(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS assemblies (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS identity_calibrations (
                    id TEXT PRIMARY KEY,
                    voice_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(voice_id, language),
                    FOREIGN KEY(voice_id) REFERENCES voices(id) ON DELETE CASCADE
                );
                INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);
                """
            )

    @staticmethod
    def _payload(model: BaseModel) -> str:
        return model.model_dump_json()

    def save_voice(self, voice: Voice) -> Voice:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO voices(id, created_at, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (voice.id, voice.created_at, self._payload(voice)),
            )
        return voice

    def get_voice(self, voice_id: str) -> Voice | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM voices WHERE id = ?", (voice_id,)
            ).fetchone()
        return Voice.model_validate_json(row[0]) if row else None

    def list_voices(self) -> list[Voice]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM voices ORDER BY created_at DESC"
            ).fetchall()
        return [Voice.model_validate_json(row[0]) for row in rows]

    def delete_voice(self, voice_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM voices WHERE id = ?", (voice_id,))

    def save_job(self, job: Job) -> Job:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO jobs(id, created_at, status, payload) VALUES(?, ?, ?, ?)",
                (job.id, job.created_at, job.status, self._payload(job)),
            )
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def list_jobs(self, limit: int = 100) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.model_validate_json(row[0]) for row in rows]

    def save_comparison(self, comparison: Comparison) -> Comparison:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO comparisons(id, created_at, payload) VALUES(?, ?, ?)",
                (comparison.id, comparison.created_at, self._payload(comparison)),
            )
        return comparison

    def get_comparison(self, comparison_id: str) -> Comparison | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM comparisons WHERE id = ?", (comparison_id,)
            ).fetchone()
        return Comparison.model_validate_json(row[0]) if row else None

    def export_catalog(self) -> dict:
        return {
            "schema_version": "qwen-voice-lab-catalog-v1",
            "voices": [
                json.loads(VoiceView.model_validate(row).model_dump_json())
                for row in self.list_voices()
            ],
            "comparisons": [],
        }

    def save_project(self, project: Project) -> Project:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO projects(id, created_at, updated_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
                "payload=excluded.payload",
                (project.id, project.created_at, project.updated_at, self._payload(project)),
            )
        return project

    def save_project_revision(
        self, project: Project, revision: SourceRevision, segments: list[ProjectSegment]
    ) -> None:
        """Persist a source revision and its project pointer as one transaction."""
        self._save_project_revision(project, revision, segments)

    def save_project_revision_if_idle(
        self,
        project: Project,
        revision: SourceRevision,
        segments: list[ProjectSegment],
        expected_current_revision_id: str,
    ) -> None:
        """Persist a revision only if its source pointer is current and no run is active."""
        self._save_project_revision(
            project,
            revision,
            segments,
            expected_current_revision_id=expected_current_revision_id,
        )

    def _save_project_revision(
        self,
        project: Project,
        revision: SourceRevision,
        segments: list[ProjectSegment],
        *,
        expected_current_revision_id: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_current_revision_id is not None:
                row = connection.execute(
                    "SELECT payload FROM projects WHERE id = ?", (project.id,)
                ).fetchone()
                if not row:
                    raise ValueError("project disappeared before the revision could be saved")
                current = Project.model_validate_json(row[0])
                if current.current_revision_id != expected_current_revision_id:
                    raise ValueError("project revision changed; reload before saving")
                active = connection.execute(
                    "SELECT id FROM project_runs "
                    "WHERE project_id = ? AND status IN (?, ?) LIMIT 1",
                    (project.id, "queued", "running"),
                ).fetchone()
                if active:
                    raise ValueError("wait for the active project run before saving a revision")
            connection.execute(
                "INSERT INTO projects(id, created_at, updated_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
                "payload=excluded.payload",
                (project.id, project.created_at, project.updated_at, self._payload(project)),
            )
            connection.execute(
                "INSERT INTO source_revisions(id, project_id, number, created_at, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    revision.id,
                    revision.project_id,
                    revision.number,
                    revision.created_at,
                    self._payload(revision),
                ),
            )
            connection.executemany(
                "INSERT INTO project_segments(revision_id, id, project_id, position, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (revision.id, row.id, row.project_id, row.position, self._payload(row))
                    for row in segments
                ],
            )

    def get_project(self, project_id: str) -> Project | None:
        return self._get_payload("projects", project_id, Project)

    def list_projects(self) -> list[Project]:
        return self._list_payloads("projects", Project, "updated_at DESC")

    def save_revision(self, revision: SourceRevision) -> SourceRevision:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO source_revisions"
                "(id, project_id, number, created_at, payload) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (
                    revision.id,
                    revision.project_id,
                    revision.number,
                    revision.created_at,
                    self._payload(revision),
                ),
            )
        return revision

    def get_revision(self, revision_id: str) -> SourceRevision | None:
        return self._get_payload("source_revisions", revision_id, SourceRevision)

    def list_revisions(self, project_id: str) -> list[SourceRevision]:
        return self._query_payloads(
            "SELECT payload FROM source_revisions WHERE project_id = ? ORDER BY number DESC",
            (project_id,),
            SourceRevision,
        )

    def save_segments(self, revision_id: str, segments: list[ProjectSegment]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM project_segments WHERE revision_id = ?", (revision_id,))
            connection.executemany(
                "INSERT INTO project_segments(revision_id, id, project_id, position, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (revision_id, row.id, row.project_id, row.position, self._payload(row))
                    for row in segments
                ],
            )

    def save_segment(self, segment: ProjectSegment) -> ProjectSegment:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO project_segments"
                "(revision_id, id, project_id, position, payload) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(revision_id, id) DO UPDATE SET position=excluded.position, "
                "payload=excluded.payload",
                (
                    segment.revision_id,
                    segment.id,
                    segment.project_id,
                    segment.position,
                    self._payload(segment),
                ),
            )
        return segment

    def list_segments(self, revision_id: str) -> list[ProjectSegment]:
        return self._query_payloads(
            "SELECT payload FROM project_segments WHERE revision_id = ? ORDER BY position",
            (revision_id,),
            ProjectSegment,
        )

    def get_segment(self, revision_id: str, segment_id: str) -> ProjectSegment | None:
        rows = self._query_payloads(
            "SELECT payload FROM project_segments WHERE revision_id = ? AND id = ?",
            (revision_id, segment_id),
            ProjectSegment,
        )
        return rows[0] if rows else None

    def save_run(self, run: ProjectRun) -> ProjectRun:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO project_runs"
                "(id, project_id, created_at, status, payload) VALUES(?, ?, ?, ?, ?)",
                (run.id, run.project_id, run.created_at, run.status, self._payload(run)),
            )
        return run

    def save_terminal_run_and_project(self, run: ProjectRun, project: Project) -> None:
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise ValueError("run must be terminal before atomic project reconciliation")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO project_runs"
                "(id, project_id, created_at, status, payload) VALUES(?, ?, ?, ?, ?)",
                (run.id, run.project_id, run.created_at, run.status, self._payload(run)),
            )
            connection.execute(
                "INSERT INTO projects(id, created_at, updated_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
                "payload=excluded.payload",
                (project.id, project.created_at, project.updated_at, self._payload(project)),
            )

    def create_run_if_idle(self, run: ProjectRun) -> ProjectRun:
        """Atomically reject stale-revision and duplicate active runs."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT id FROM project_runs WHERE project_id = ? AND status IN (?, ?) LIMIT 1",
                (run.project_id, "queued", "running"),
            ).fetchone()
            if active:
                raise ValueError(f"project already has an active run: {active[0]}")
            project_row = connection.execute(
                "SELECT payload FROM projects WHERE id = ?", (run.project_id,)
            ).fetchone()
            if not project_row:
                raise ValueError("project disappeared before the run could be queued")
            project = Project.model_validate_json(project_row[0])
            if project.current_revision_id != run.revision_id:
                raise ValueError("project revision changed; reload before starting a run")
            connection.execute(
                "INSERT INTO project_runs(id, project_id, created_at, status, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                (run.id, run.project_id, run.created_at, run.status, self._payload(run)),
            )
        return run

    def get_run(self, run_id: str) -> ProjectRun | None:
        return self._get_payload("project_runs", run_id, ProjectRun)

    def list_runs(self, project_id: str) -> list[ProjectRun]:
        return self._query_payloads(
            "SELECT payload FROM project_runs WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
            ProjectRun,
        )

    def save_take(self, take: Take) -> Take:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO takes"
                "(id, project_id, revision_id, segment_id, attempt, created_at, payload) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (
                    take.id,
                    take.project_id,
                    take.revision_id,
                    take.segment_id,
                    take.attempt,
                    take.created_at,
                    self._payload(take),
                ),
            )
        return take

    def get_take(self, take_id: str) -> Take | None:
        return self._get_payload("takes", take_id, Take)

    def list_takes(self, revision_id: str, segment_id: str | None = None) -> list[Take]:
        if segment_id:
            return self._query_payloads(
                "SELECT payload FROM takes WHERE revision_id = ? AND segment_id = ? "
                "ORDER BY attempt DESC",
                (revision_id, segment_id),
                Take,
            )
        return self._query_payloads(
            "SELECT payload FROM takes WHERE revision_id = ? ORDER BY segment_id, attempt DESC",
            (revision_id,),
            Take,
        )

    def list_compatible_takes(
        self, project_id: str, segment_id: str, text_sha256: str
    ) -> list[Take]:
        rows = self._query_payloads(
            "SELECT payload FROM takes WHERE project_id = ? AND segment_id = ? "
            "ORDER BY attempt DESC, created_at DESC",
            (project_id, segment_id),
            Take,
        )
        return [row for row in rows if row.text_sha256 == text_sha256]

    def save_quality_report(self, report: QualityReport) -> QualityReport:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO quality_reports(id, take_id, created_at, payload) "
                "VALUES(?, ?, ?, ?)",
                (report.id, report.take_id, report.created_at, self._payload(report)),
            )
        return report

    def list_quality_reports(self, take_id: str) -> list[QualityReport]:
        return self._query_payloads(
            "SELECT payload FROM quality_reports WHERE take_id = ? ORDER BY created_at",
            (take_id,),
            QualityReport,
        )

    def save_assembly(self, assembly: Assembly) -> Assembly:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO assemblies"
                "(id, project_id, revision_id, created_at, payload) VALUES(?, ?, ?, ?, ?)",
                (
                    assembly.id,
                    assembly.project_id,
                    assembly.revision_id,
                    assembly.created_at,
                    self._payload(assembly),
                ),
            )
        return assembly

    def save_identity_calibration(self, calibration: IdentityCalibration) -> IdentityCalibration:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO identity_calibrations"
                "(id, voice_id, language, created_at, payload) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(voice_id, language) DO UPDATE SET id=excluded.id, "
                "created_at=excluded.created_at, payload=excluded.payload",
                (
                    calibration.id,
                    calibration.voice_id,
                    calibration.language,
                    calibration.created_at,
                    self._payload(calibration),
                ),
            )
        return calibration

    def get_identity_calibration(
        self,
        voice_id: str,
        language: str,
        validator: str | None = None,
        validator_model_sha256: str | None = None,
    ) -> IdentityCalibration | None:
        rows = self._query_payloads(
            "SELECT payload FROM identity_calibrations WHERE voice_id = ? AND language = ?",
            (voice_id, language),
            IdentityCalibration,
        )
        if not rows:
            return None
        calibration = rows[0]
        if validator is not None and calibration.validator != validator:
            return None
        if (
            validator_model_sha256 is not None
            and calibration.validator_model_sha256 != validator_model_sha256
        ):
            return None
        return calibration

    def list_identity_calibrations(self, voice_id: str) -> list[IdentityCalibration]:
        return self._query_payloads(
            "SELECT payload FROM identity_calibrations WHERE voice_id = ? ORDER BY language",
            (voice_id,),
            IdentityCalibration,
        )

    def get_assembly(self, assembly_id: str) -> Assembly | None:
        return self._get_payload("assemblies", assembly_id, Assembly)

    def list_assemblies(self, project_id: str) -> list[Assembly]:
        return self._query_payloads(
            "SELECT payload FROM assemblies WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
            Assembly,
        )

    def _get_payload(self, table: str, row_id: str, model: type[ModelT]) -> ModelT | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()
        return model.model_validate_json(row[0]) if row else None

    def _list_payloads(self, table: str, model: type[ModelT], order_by: str) -> list[ModelT]:
        return self._query_payloads(f"SELECT payload FROM {table} ORDER BY {order_by}", (), model)

    def _query_payloads(self, query: str, parameters: tuple, model: type[ModelT]) -> list[ModelT]:
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]
