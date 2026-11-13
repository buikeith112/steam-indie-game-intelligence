"""Database plumbing: engine, run log, raw payload store, resumable work queue, pipeline state."""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB

from .config import get_settings
from .http import ApiResult

MAX_ATTEMPTS = 5


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True, future=True,
                         connect_args={"connect_timeout": 10})


def upgrade_schema(url: str | None = None) -> None:
    """Programmatic `alembic upgrade head`."""
    from alembic import command
    from alembic.config import Config

    from .config import PROJECT_ROOT
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "db" / "migrations"))
    if url:
        import os
        os.environ["DATABASE_URL"] = url
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------------
def start_run(conn: Connection, job: str) -> int:
    return conn.execute(text("INSERT INTO ops.ingestion_runs (job) VALUES (:job) RETURNING run_id"),
                        {"job": job}).scalar_one()


def finish_run(conn: Connection, run_id: int, status: str, ok: int, failed: int, notes: dict | None = None):
    conn.execute(text("""
        UPDATE ops.ingestion_runs SET finished_at = now(), status = :status,
               items_ok = :ok, items_failed = :failed, notes = :notes
        WHERE run_id = :run_id""").bindparams(bindparam("notes", type_=JSONB)),
                 {"run_id": run_id, "status": status, "ok": ok, "failed": failed, "notes": notes})


# ---------------------------------------------------------------------------------
# Raw payloads
# ---------------------------------------------------------------------------------
_SAVE_RAW = text("""
    INSERT INTO raw.api_responses (source, app_id, request_key, request_params, http_status, payload, run_id)
    VALUES (:source, :app_id, :request_key, :params, :status, :payload, :run_id)
    RETURNING response_id
""").bindparams(bindparam("params", type_=JSONB), bindparam("payload", type_=JSONB))


def save_raw(conn: Connection, source: str, result: ApiResult, *, app_id: int | None = None,
             request_key: str | None = None, run_id: int | None = None) -> int:
    params = result.params
    if params and "input_json" in params:          # store the decoded request, not a JSON string
        params = {**params, "input_json": json.loads(params["input_json"])}
    if params and "key" in params:                 # never persist the API key
        params = {k: v for k, v in params.items() if k != "key"}
    return conn.execute(_SAVE_RAW, {
        "source": source, "app_id": app_id, "request_key": request_key, "params": params,
        "status": result.status, "payload": result.payload, "run_id": run_id,
    }).scalar_one()


def mark_processed(conn: Connection, response_ids: Iterable[int]) -> None:
    ids = list(response_ids)
    if ids:
        conn.execute(text("UPDATE raw.api_responses SET processed_at = now() WHERE response_id = ANY(:ids)"),
                     {"ids": ids})


# ---------------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------------
def enqueue(conn: Connection, source: str, app_ids: Iterable[int], priority: int | dict[int, int] = 100,
            requeue_done: bool = False) -> int:
    """Insert (source, app_id) work items. Existing items are left alone unless requeue_done."""
    rows = [{"source": source, "app_id": int(a),
             "priority": priority.get(a, 100) if isinstance(priority, dict) else priority} for a in app_ids]
    if not rows:
        return 0
    conflict = ("DO UPDATE SET status = 'pending', attempts = 0, next_retry_at = NULL, "
                "priority = EXCLUDED.priority, updated_at = now() "
                "WHERE ops.ingestion_queue.status IN ('done', 'failed', 'skip')") if requeue_done else "DO NOTHING"
    conn.execute(text(f"""
        INSERT INTO ops.ingestion_queue (source, app_id, priority) VALUES (:source, :app_id, :priority)
        ON CONFLICT (source, app_id) {conflict}"""), rows)
    return len(rows)


def claim(conn: Connection, source: str, limit: int) -> list[int]:
    """Atomically take up to `limit` ready items (pending, or failed and due for retry)."""
    return list(conn.execute(text("""
        UPDATE ops.ingestion_queue q SET status = 'in_progress', updated_at = now()
        FROM (
            SELECT source, app_id FROM ops.ingestion_queue
            WHERE source = :source
              AND (status = 'pending' OR (status = 'failed' AND attempts < :max AND next_retry_at <= now()))
            ORDER BY priority, app_id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        ) pick
        WHERE q.source = pick.source AND q.app_id = pick.app_id
        RETURNING q.app_id"""), {"source": source, "limit": limit, "max": MAX_ATTEMPTS}).scalars())


def mark_done(conn: Connection, source: str, app_id: int, status: str = "done", note: str | None = None):
    conn.execute(text("""
        UPDATE ops.ingestion_queue SET status = :status, last_error = :note, updated_at = now()
        WHERE source = :source AND app_id = :app_id"""),
                 {"source": source, "app_id": app_id, "status": status, "note": note})


def mark_failed(conn: Connection, source: str, app_id: int, error: str) -> None:
    conn.execute(text("""
        UPDATE ops.ingestion_queue
        SET status = 'failed', attempts = attempts + 1, last_error = left(:err, 1000), updated_at = now(),
            next_retry_at = now() + make_interval(mins => power(2, attempts + 1)::int)
        WHERE source = :source AND app_id = :app_id"""), {"source": source, "app_id": app_id, "err": error})


def release(conn: Connection, source: str, app_ids: Iterable[int]) -> None:
    """Return claimed-but-unprocessed items to 'pending' (e.g. when a run stops early)."""
    ids = list(app_ids)
    if ids:
        conn.execute(text("""UPDATE ops.ingestion_queue SET status = 'pending', updated_at = now()
                             WHERE source = :s AND app_id = ANY(:ids) AND status = 'in_progress'"""),
                     {"s": source, "ids": ids})


def reset_stale(conn: Connection, source: str | None = None, older_than_min: int = 60) -> int:
    """Items left 'in_progress' by a crashed run go back to 'pending'."""
    return conn.execute(text("""
        UPDATE ops.ingestion_queue SET status = 'pending', updated_at = now()
        WHERE status = 'in_progress' AND updated_at < now() - make_interval(mins => :m)
          AND (CAST(:source AS text) IS NULL OR source = :source)"""),
                        {"m": older_than_min, "source": source}).rowcount


def queue_stats(conn: Connection) -> list[tuple]:
    return conn.execute(text("""
        SELECT source, status, count(*) FROM ops.ingestion_queue GROUP BY 1, 2 ORDER BY 1, 2""")).all()


# ---------------------------------------------------------------------------------
# Pipeline state (checkpoints)
# ---------------------------------------------------------------------------------
def get_state(conn: Connection, key: str, default=None):
    v = conn.execute(text("SELECT value FROM ops.pipeline_state WHERE key = :k"), {"k": key}).scalar()
    return default if v is None else v


def set_state(conn: Connection, key: str, value) -> None:
    conn.execute(text("""
        INSERT INTO ops.pipeline_state (key, value) VALUES (:k, :v)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """).bindparams(bindparam("v", type_=JSONB)), {"k": key, "v": value})


def utcnow() -> datetime:
    return datetime.now(UTC)


def days_ago(n: int) -> datetime:
    return utcnow() - timedelta(days=n)
