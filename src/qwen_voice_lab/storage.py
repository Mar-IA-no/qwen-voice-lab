from __future__ import annotations

import json
import sqlite3
import threading
from typing import TypeVar

from pydantic import BaseModel

from .config import Settings
from .models import Comparison, Job, Voice, VoiceView

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
                """
            )

    @staticmethod
    def _payload(model: BaseModel) -> str:
        return model.model_dump_json()

    def save_voice(self, voice: Voice) -> Voice:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO voices(id, created_at, payload) VALUES(?, ?, ?)",
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
