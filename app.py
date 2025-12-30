import os
import time
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg
from psycopg import sql
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

SITEANALYSIS_TABLE = "siteanalysis"

PROCESS_INTERVAL_SECONDS = int(os.environ.get("PROCESS_INTERVAL_SECONDS", "60"))
CONTAINER_MAX = int(os.environ.get("CONTAINER_MAX", "10000"))

EVENT_CONTAINER: Dict[Tuple[str, str], Dict[str, Any]] = {}
EVENT_CONTAINER_LOCK = Lock()

PERSIST_LOCK = Lock()
SCHEDULER_START_LOCK = Lock()
SCHEDULER: Optional[BackgroundScheduler] = None
SCHEDULER_STARTED = False

DB_INITIALIZED = False
LAST_PROCESS_AT: Optional[datetime] = None
LAST_PROCESS_MONOTONIC: Optional[float] = None

app = Flask(__name__)
CORS(app)

DB_INIT_LOCK = Lock()


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _parse_timestamptz(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _init_db() -> None:
    global DB_INITIALIZED

    # Fast path
    if DB_INITIALIZED:
        return

    with DB_INIT_LOCK:
        # Double-check inside lock
        if DB_INITIALIZED:
            return

        ddl = sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                visitor_id TEXT NOT NULL,
                city TEXT,
                url TEXT,
                session_started_at TIMESTAMPTZ NOT NULL,
                last_heartbeat_at TIMESTAMPTZ NOT NULL
            )
            """
        ).format(table=sql.Identifier(SITEANALYSIS_TABLE))

        ensure_unique_session = sql.SQL(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = {table_name}
                  AND c.contype = 'u'
                  AND pg_get_constraintdef(c.oid) ILIKE '%(visitor_id, session_started_at)%'
              ) THEN
                EXECUTE format(
                  'ALTER TABLE %I ADD CONSTRAINT %I UNIQUE (visitor_id, session_started_at)',
                  {table_name},
                  {constraint_name}
                );
              END IF;
            END $$;
            """
        ).format(
            table_name=sql.Literal(SITEANALYSIS_TABLE),
            constraint_name=sql.Literal(
                f"{SITEANALYSIS_TABLE}_visitor_id_session_started_at_key"
            ),
        )

        drop_unique_visitor_id = sql.SQL(
            """
            DO $$
            DECLARE
              conname text;
            BEGIN
              SELECT c.conname INTO conname
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
              WHERE t.relname = {table_name}
                AND c.contype = 'u'
                AND pg_get_constraintdef(c.oid) ILIKE '%(visitor_id)%'
              LIMIT 1;

              IF conname IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE %I DROP CONSTRAINT %I',
                    {table_name},
                    conname
                );
              END IF;
            END $$;
            """
        ).format(table_name=sql.Literal(SITEANALYSIS_TABLE))

        create_indexes = sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS {idx_visitor}
              ON {table} (visitor_id);

            CREATE INDEX IF NOT EXISTS {idx_visitor_session}
              ON {table} (visitor_id, session_started_at);

            CREATE INDEX IF NOT EXISTS {idx_visitor_session_hb}
              ON {table} (visitor_id, session_started_at, last_heartbeat_at DESC);
            """
        ).format(
            idx_visitor=sql.Identifier(f"{SITEANALYSIS_TABLE}_visitor_id_idx"),
            idx_visitor_session=sql.Identifier(
                f"{SITEANALYSIS_TABLE}_visitor_id_session_started_at_idx"
            ),
            idx_visitor_session_hb=sql.Identifier(
                f"{SITEANALYSIS_TABLE}_visitor_id_session_started_at_last_heartbeat_at_idx"
            ),
            table=sql.Identifier(SITEANALYSIS_TABLE),
        )

        # ---- DB EXECUTION ----
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(drop_unique_visitor_id)
                cur.execute(ensure_unique_session)
                cur.execute(create_indexes)
                conn.commit()

        # ✅ Set flag ONLY after success
        DB_INITIALIZED = True


@app.get("/health")
def health():
    return {"status": "ok"}


def _ensure_json_object(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    if not value:
        raise ValueError("JSON body must not be empty")
    return value


@app.post("/siteanalysis/enqueue")
def enqueue_siteanalysis():
    try:
        box = _ensure_json_object(request.get_json(silent=True))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    visitor_id = box.get("visitor_id")
    if not isinstance(visitor_id, str) or not visitor_id.strip():
        return jsonify({"error": "visitor_id is required"}), 400
    visitor_id = visitor_id.strip()

    try:
        session_started_at = _parse_timestamptz(box.get("session_started_at"), "session_started_at")
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    city = box.get("city")
    url = box.get("url")

    now = datetime.now(timezone.utc)
    event = {
        "visitor_id": visitor_id,
        "city": city,
        "url": url,
        "session_started_at": session_started_at.isoformat(),
        "last_heartbeat_at": now.isoformat(),
    }
    with EVENT_CONTAINER_LOCK:
        key = (visitor_id, session_started_at.isoformat())
        existing = EVENT_CONTAINER.get(key)
        if existing is None:
            if len(EVENT_CONTAINER) >= CONTAINER_MAX:
                return jsonify({"error": "container is full", "container_max": CONTAINER_MAX}), 429
            EVENT_CONTAINER[key] = event
        else:
            try:
                existing_hb = _parse_timestamptz(existing.get("last_heartbeat_at"), "last_heartbeat_at")
            except Exception:
                existing_hb = datetime.min.replace(tzinfo=timezone.utc)
            if now >= existing_hb:
                EVENT_CONTAINER[key] = event
        size = len(EVENT_CONTAINER)

    return jsonify({"queued": True, "container_size": size}), 202


def _snapshot_compacted_sessions(limit: int) -> List[Dict[str, Any]]:
    with EVENT_CONTAINER_LOCK:
        items = list(EVENT_CONTAINER.values())

    if limit > 0:
        items = items[:limit]
    return items


def _persist_compacted_sessions(limit: int = 5000) -> None:
    global LAST_PROCESS_AT
    global LAST_PROCESS_MONOTONIC

    if not PERSIST_LOCK.acquire(blocking=False):
        return
    try:
        now = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        min_interval = float(PROCESS_INTERVAL_SECONDS)
        if LAST_PROCESS_MONOTONIC is not None:
            elapsed = now_mono - LAST_PROCESS_MONOTONIC
            if elapsed < min_interval:
                return

        drained = _snapshot_compacted_sessions(limit=limit)
        if not drained:
            LAST_PROCESS_AT = now
            LAST_PROCESS_MONOTONIC = now_mono
            return

        # Track keys to delete later
        drained_keys = [
            (item["visitor_id"], item["session_started_at"])
            for item in drained
        ]

        _init_db()

        rows: List[Tuple[Any, Any, Any, Any, Any]] = []
        for item in drained:
            try:
                session_started_at = _parse_timestamptz(item.get("session_started_at"), "session_started_at")
                last_heartbeat_at = _parse_timestamptz(item.get("last_heartbeat_at"), "last_heartbeat_at")
            except Exception:
                continue

            rows.append(
                (
                    item.get("visitor_id"),
                    item.get("city"),
                    item.get("url"),
                    session_started_at,
                    last_heartbeat_at,
                )
            )

        if not rows:
            LAST_PROCESS_AT = now
            LAST_PROCESS_MONOTONIC = now_mono
            return

        values_sql = sql.SQL(", ").join(sql.SQL("(%s, %s, %s, %s, %s)") for _ in rows)
        insert_sql = sql.SQL(
            "INSERT INTO {table} (visitor_id, city, url, session_started_at, last_heartbeat_at) VALUES "
        ).format(table=sql.Identifier(SITEANALYSIS_TABLE)) + values_sql

        upsert_sql = insert_sql + sql.SQL(
            " ON CONFLICT (visitor_id, session_started_at) DO UPDATE SET "
            "city = EXCLUDED.city, "
            "url = COALESCE(NULLIF({table}.url, ''), NULLIF(EXCLUDED.url, '') ), "
            "last_heartbeat_at = EXCLUDED.last_heartbeat_at "
            "WHERE {table}.last_heartbeat_at <= EXCLUDED.last_heartbeat_at"
        ).format(table=sql.Identifier(SITEANALYSIS_TABLE))

        params: List[Any] = [v for row in rows for v in row]

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(upsert_sql, params)
                conn.commit()

        # ✅ Remove persisted items from memory
        with EVENT_CONTAINER_LOCK:
            for key in drained_keys:
                EVENT_CONTAINER.pop(key, None)


        LAST_PROCESS_AT = now
        LAST_PROCESS_MONOTONIC = now_mono
    except Exception:
        logger.exception("background persistence failed")
    finally:
        PERSIST_LOCK.release()


def _ensure_scheduler_started() -> None:
    global SCHEDULER
    global SCHEDULER_STARTED

    if SCHEDULER_STARTED:
        return

    with SCHEDULER_START_LOCK:
        if SCHEDULER_STARTED:
            return

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            func=_persist_compacted_sessions,
            trigger=IntervalTrigger(seconds=60),
            id="persist_compacted_sessions",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        scheduler.start()
        SCHEDULER = scheduler
        SCHEDULER_STARTED = True


@app.get("/siteanalysis/state")
def siteanalysis_state():
    with EVENT_CONTAINER_LOCK:
        container_size = len(EVENT_CONTAINER)
    return jsonify(
        {
            "process_interval_seconds": PROCESS_INTERVAL_SECONDS,
            "container_size": container_size,
            "container_max": CONTAINER_MAX,
            "db_initialized": DB_INITIALIZED,
            "last_process_at": LAST_PROCESS_AT.isoformat() if LAST_PROCESS_AT else None,
            "scheduler_started": SCHEDULER_STARTED,
        }
    )

'''
@app.before_request
def _start_scheduler_once_per_process() -> None:
    _ensure_scheduler_started()
'''

if __name__ == "__main__":
    _ensure_scheduler_started()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
