"""SQLite persistence and file-backed build logs.

The control plane is intentionally single-process, but every database method
opens its own connection and queue claims use ``BEGIN IMMEDIATE``.  This keeps
state correct across a worker thread and a web request thread and also makes a
second accidental worker unable to claim the same task.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Storage:
    """Persistent state for users, sessions, jobs, snapshots and artifacts."""

    def __init__(self, db_path: str | os.PathLike[str], log_dir: str | os.PathLike[str], artifact_dir: str | os.PathLike[str]):
        self.db_path = Path(db_path)
        self.log_dir = Path(log_dir)
        self.artifact_dir = Path(artifact_dir)
        self._log_lock = threading.RLock()
        # Log records live in JSONL files.  Keep the hot-path sequence and
        # offset in memory and persist only periodic checkpoints; writing a
        # SQLite row for every compiler line makes large builds needlessly
        # contend on the database.
        self._log_state: dict[str, tuple[int, int]] = {}
        self._log_checkpoint_every = 256
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    user_agent TEXT,
                    remote_addr TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS login_limits (
                    key TEXT PRIMARY KEY,
                    window_started_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    blocked_until INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    device TEXT NOT NULL,
                    config TEXT NOT NULL,
                    packages_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    parallel_jobs INTEGER NOT NULL DEFAULT 1,
                    reuse_cache INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    claimed_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    result_json TEXT,
                    output_dir TEXT NOT NULL,
                    owner_user_id INTEGER REFERENCES admin_users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
                /* Build output lives in JSONL files.  SQLite stores only
                   sequence/offset checkpoints so a long compile does not
                   duplicate millions of log lines in the database. */
                CREATE TABLE IF NOT EXISTS job_log_state (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    last_offset INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS job_log_checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    PRIMARY KEY(job_id, seq)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER,
                    sha256 TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
                CREATE TABLE IF NOT EXISTS device_defaults (
                    device TEXT PRIMARY KEY,
                    packages_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER REFERENCES admin_users(id)
                );
                CREATE TABLE IF NOT EXISTS source_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_snapshots (
                    id TEXT PRIMARY KEY,
                    source_snapshot_id TEXT NOT NULL,
                    device TEXT NOT NULL DEFAULT '*',
                    items_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_created ON catalog_snapshots(source_snapshot_id,device,created_at);
                """
            )
            # ``CREATE TABLE IF NOT EXISTS`` does not update an existing
            # installation.  Keep old Web databases readable while adding
            # the two build controls introduced after the first release.
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "parallel_jobs" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN parallel_jobs INTEGER NOT NULL DEFAULT 1")
            if "reuse_cache" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN reuse_cache INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # -- users and sessions -------------------------------------------------

    def create_admin(self, username: str, password_hash: str) -> int:
        now = utc_now()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO admin_users(username,password_hash,created_at) VALUES(?,?,?)",
                (username, password_hash, now),
            )
            return int(cur.lastrowid)

    def get_admin_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row_dict(db.execute("SELECT * FROM admin_users WHERE username=?", (username,)).fetchone())

    def get_admin(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row_dict(db.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone())

    def admin_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])

    def mark_login(self, user_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE admin_users SET last_login_at=? WHERE id=?", (utc_now(), user_id))

    def create_session(self, user_id: int, token: str, csrf_token: str, expires_at: str, user_agent: str | None, remote_addr: str | None) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        csrf_hash = hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_hash,created_at,expires_at,last_seen_at,user_agent,remote_addr) VALUES(?,?,?,?,?,?,?,?)",
                (token_hash, user_id, csrf_hash, now, expires_at, now, user_agent, remote_addr),
            )

    def get_session(self, token: str, touch: bool = True) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT s.*,u.username FROM sessions s JOIN admin_users u ON u.id=s.user_id WHERE s.token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            result = self._row_dict(row)
            if touch:
                db.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (utc_now(), token_hash))
            return result

    def delete_session(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_expired_sessions(self, now: str | None = None) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now or utc_now(),))

    # -- login rate limits -------------------------------------------------

    def login_limit(self, key: str, now_epoch: int, window_seconds: int, max_attempts: int) -> tuple[bool, int]:
        """Return ``(allowed, retry_after)`` and update the fixed window."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
            if row is None or now_epoch - int(row["window_started_at"]) >= window_seconds:
                db.execute(
                    "INSERT INTO login_limits(key,window_started_at,attempts,blocked_until) VALUES(?,?,0,0) ON CONFLICT(key) DO UPDATE SET window_started_at=excluded.window_started_at,attempts=0,blocked_until=0",
                    (key, now_epoch),
                )
                return True, 0
            if int(row["blocked_until"]) > now_epoch:
                return False, int(row["blocked_until"]) - now_epoch
            attempts = int(row["attempts"]) + 1
            if attempts > max_attempts:
                blocked_until = now_epoch + window_seconds
                db.execute("UPDATE login_limits SET attempts=?,blocked_until=? WHERE key=?", (attempts, blocked_until, key))
                return False, window_seconds
            db.execute("UPDATE login_limits SET attempts=? WHERE key=?", (attempts, key))
            return True, 0

    def clear_login_limit(self, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM login_limits WHERE key=?", (key,))

    # -- jobs and queue ----------------------------------------------------

    def create_job(self, job: dict[str, Any]) -> None:
        with self.connect() as db:
            parallel_jobs = job.get("parallel_jobs", 1)
            try:
                parallel_jobs = max(1, int(parallel_jobs or 1))
            except (TypeError, ValueError):
                parallel_jobs = 1
            reuse_cache = job.get("reuse_cache", True)
            if reuse_cache is None:
                reuse_cache = True
            db.execute(
                """INSERT INTO jobs(
                    id,device,config,packages_json,options_json,source_snapshot_json,
                    parallel_jobs,reuse_cache,status,created_at,output_dir,owner_user_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job["id"], job["device"], job["config"], _json(job.get("packages", [])),
                    _json(job.get("options", {})), _json(job.get("source_snapshot", {})),
                    parallel_jobs, 1 if bool(reuse_cache) else 0,
                    job.get("status", "queued"), job.get("created_at", utc_now()), job["output_dir"], job.get("owner_user_id"),
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = self._row_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        return self._decode_job(row)

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [self._decode_job(self._row_dict(row)) for row in rows]

    def _decode_job(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["packages"] = _loads(row.pop("packages_json", None), [])
        row["options"] = _loads(row.pop("options_json", None), {})
        row["source_snapshot"] = _loads(row.pop("source_snapshot_json", None), {})
        row["result"] = _loads(row.pop("result_json", None), None)
        row["cancel_requested"] = bool(row.get("cancel_requested"))
        try:
            row["parallel_jobs"] = max(1, int(row.get("parallel_jobs", 1) or 1))
        except (TypeError, ValueError):
            row["parallel_jobs"] = 1
        reuse_cache = row.get("reuse_cache", 1)
        if reuse_cache is None:
            reuse_cache = True
        if isinstance(reuse_cache, str):
            reuse_cache = reuse_cache.strip().lower() not in {"", "0", "false", "no", "off"}
        row["reuse_cache"] = bool(reuse_cache)
        return row

    def claim_next_job(self) -> dict[str, Any] | None:
        """Atomically move one queued job to running and return it."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if row is None:
                return None
            job_id = row["id"]
            now = utc_now()
            db.execute("UPDATE jobs SET status='running',started_at=?,claimed_at=? WHERE id=? AND status='queued'", (now, now, job_id))
            updated = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode_job(self._row_dict(updated))

    def request_cancel(self, job_id: str) -> str | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            if status == "queued":
                error = "管理员取消任务"
                result = {"status": "canceled", "success": False, "ok": False, "error": error}
                db.execute(
                    "UPDATE jobs SET status='canceled',cancel_requested=1,finished_at=?,error=?,result_json=? WHERE id=?",
                    (utc_now(), error, _json(result), job_id),
                )
                return "canceled"
            if status == "running":
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                return "cancel_requested"
            return status

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row[0])

    def finish_job(self, job_id: str, status: str, error: str | None = None, result: Any = None) -> str:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None and bool(row["cancel_requested"]) and status != "canceled":
                # A cancellation request wins over a late worker result.  The
                # request and this final write share the same SQLite lock, so
                # a result can only win when cancellation arrived afterward.
                status = "canceled"
                error = error or "管理员取消任务"
                if isinstance(result, dict):
                    result = dict(result)
                    result.update({"status": "canceled", "success": False, "ok": False, "error": error})
            db.execute(
                "UPDATE jobs SET status=?,finished_at=?,error=?,result_json=? WHERE id=?",
                (status, utc_now(), error, _json(result) if result is not None else None, job_id),
            )
        self._flush_log_state(job_id)
        return status

    def mark_interrupted(self, job_id: str, reason: str = "Web 进程重启时未检测到仍在运行的构建容器") -> None:
        self.finish_job(job_id, "interrupted", reason)

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        return [self._decode_job(self._row_dict(row)) for row in rows]

    # -- logs --------------------------------------------------------------

    def log_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", str(job_id)):
            raise ValueError("invalid job id")
        return self.log_dir / f"{job_id}.log"

    def _recover_log_state(self, job_id: str, path: Path) -> tuple[int, int]:
        """Recover the last sequence/offset after a process restart."""

        with self.connect() as db:
            row = db.execute("SELECT last_seq,last_offset FROM job_log_state WHERE job_id=?", (job_id,)).fetchone()
        seq = int(row[0]) if row is not None else 0
        offset = int(row[1]) if row is not None else 0
        try:
            size = path.stat().st_size
            if offset < 0 or offset > size:
                seq, offset = 0, 0
            with path.open("rb") as fp:
                fp.seek(offset)
                for raw in fp:
                    end = fp.tell()
                    try:
                        record = json.loads(raw.decode("utf-8"))
                        current = int(record.get("seq", 0))
                    except (UnicodeDecodeError, ValueError, TypeError):
                        continue
                    if current > seq:
                        seq = current
                        offset = end
        except OSError:
            return 0, 0
        return seq, offset

    def _checkpoint_log(self, job_id: str, seq: int, offset: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO job_log_state(job_id,last_seq,last_offset) VALUES(?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET last_seq=excluded.last_seq,last_offset=excluded.last_offset",
                (job_id, seq, offset),
            )
            db.execute(
                "INSERT OR REPLACE INTO job_log_checkpoints(job_id,seq,byte_offset) VALUES(?,?,?)",
                (job_id, seq, max(0, offset)),
            )

    def _flush_log_state(self, job_id: str) -> None:
        with self._log_lock:
            path = self.log_path(job_id)
            state = self._log_state.get(job_id)
            if state is None:
                state = self._recover_log_state(job_id, path)
                self._log_state[job_id] = state
            seq, offset = state
            if seq:
                self._checkpoint_log(job_id, seq, offset)

    def append_log(self, job_id: str, line: str) -> dict[str, Any]:
        line = str(line).replace("\x00", "")
        with self._log_lock:
            path = self.log_path(job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            if job_id not in self._log_state:
                self._log_state[job_id] = self._recover_log_state(job_id, path)
            previous_seq, _previous_offset = self._log_state[job_id]
            with path.open("ab") as fp:
                byte_offset = fp.tell()
                seq = previous_seq + 1
                created = utc_now()
                record = _json({"job_id": job_id, "seq": seq, "created_at": created, "line": line}) + "\n"
                encoded = record.encode("utf-8")
                fp.write(encoded)
                fp.flush()
                end_offset = byte_offset + len(encoded)
                self._log_state[job_id] = (seq, end_offset)
                if seq == 1 or seq % self._log_checkpoint_every == 0:
                    self._checkpoint_log(job_id, seq, end_offset)
        return {"job_id": job_id, "seq": seq, "created_at": created, "line": line}

    def get_logs(self, job_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        path = self.log_path(job_id)
        if not path.exists():
            return []
        offset = 0
        if after > 0:
            with self.connect() as db:
                checkpoint = db.execute(
                    "SELECT seq,byte_offset FROM job_log_checkpoints WHERE job_id=? AND seq<=? ORDER BY seq DESC LIMIT 1",
                    (job_id, after),
                ).fetchone()
            offset = int(checkpoint["byte_offset"]) if checkpoint else 0
        result: list[dict[str, Any]] = []
        with path.open("rb") as fp:
            fp.seek(offset)
            for raw in fp:
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if int(row.get("seq", 0)) <= after:
                    continue
                result.append(row)
                if len(result) >= limit:
                    break
        return result

    # -- artifacts ---------------------------------------------------------

    def add_artifact(self, artifact: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO artifacts(id,job_id,name,relative_path,size,sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                (artifact["id"], artifact["job_id"], artifact["name"], artifact["relative_path"], artifact.get("size"), artifact.get("sha256"), artifact.get("created_at", utc_now())),
            )

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row_dict(db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY name", (job_id,)).fetchall()]

    # -- defaults, source and catalog -------------------------------------

    def save_defaults(self, device: str, packages: list[str], options: dict[str, Any], user_id: int | None) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO device_defaults(device,packages_json,options_json,updated_at,updated_by) VALUES(?,?,?,?,?)
                   ON CONFLICT(device) DO UPDATE SET packages_json=excluded.packages_json,options_json=excluded.options_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (device, _json(packages), _json(options), utc_now(), user_id),
            )

    def get_defaults(self, device: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = self._row_dict(db.execute("SELECT * FROM device_defaults WHERE device=?", (device,)).fetchone())
        if row is None:
            return None
        row["packages"] = _loads(row.pop("packages_json", None), [])
        row["options"] = _loads(row.pop("options_json", None), {})
        return row

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO source_state(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, _json(value), utc_now()),
            )

    def get_state(self, key: str) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM source_state WHERE key=?", (key,)).fetchone()
        return _loads(row[0], None) if row else None

    def save_catalog(self, snapshot_id: str, source_snapshot_id: str, device: str, items: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO catalog_snapshots(
                    id,source_snapshot_id,device,items_json,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    source_snapshot_id=excluded.source_snapshot_id,
                    device=excluded.device,
                    items_json=excluded.items_json,
                    metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at""",
                (snapshot_id, source_snapshot_id, device, _json(items), _json(metadata or {}), utc_now()),
            )
            db.execute("DELETE FROM catalog_snapshots WHERE id NOT IN (SELECT id FROM catalog_snapshots ORDER BY created_at DESC LIMIT 16)")

    def latest_catalog(self, source_snapshot_id: str | None = None, device: str | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            clauses: list[str] = []
            params: list[Any] = []
            if source_snapshot_id:
                clauses.append("source_snapshot_id=?")
                params.append(source_snapshot_id)
            if device:
                clauses.append("(device=? OR device='*')")
                params.append(device)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            row = self._row_dict(db.execute(f"SELECT * FROM catalog_snapshots{where} ORDER BY created_at DESC LIMIT 1", params).fetchone())
        if row is None:
            return None
        row["items"] = _loads(row.pop("items_json", None), [])
        row["metadata"] = _loads(row.pop("metadata_json", None), {})
        return row


@contextlib.contextmanager
def temporary_storage(root: str | os.PathLike[str]) -> Iterator[Storage]:
    root_path = Path(root)
    yield Storage(root_path / "state.sqlite3", root_path / "logs", root_path / "artifacts")
