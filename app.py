import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Deque, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg
from psycopg import sql


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

SITEANALYSIS_TABLE = "siteanalysis"

PROCESS_INTERVAL_SECONDS = int(os.environ.get("PROCESS_INTERVAL_SECONDS", "60"))
CONTAINER_MAX = int(os.environ.get("CONTAINER_MAX", "10000"))

EVENT_CONTAINER: Deque[Dict[str, Any]] = deque(maxlen=CONTAINER_MAX)
EVENT_CONTAINER_LOCK = Lock()
FAILED_DRAINED: List[Dict[str, Any]] = []

DB_INITIALIZED = False
LAST_PROCESS_AT: Optional[datetime] = None
LAST_PROCESS_MONOTONIC: Optional[float] = None

app = Flask(__name__)
CORS(app)


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

    drop_unique_visitor_id = sql.SQL(
        """
        DO $$
        DECLARE
          conname text;
        BEGIN
          SELECT c.conname INTO conname
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
          WHERE t.relname = {table_name}
            AND c.contype = 'u'
            AND pg_get_constraintdef(c.oid) ILIKE '%(visitor_id)%'
          LIMIT 1;

          IF conname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', {table_name}, conname);
          END IF;
        END $$;
        """
    ).format(table_name=sql.Literal(SITEANALYSIS_TABLE))

    create_indexes = sql.SQL(
        """
        CREATE INDEX IF NOT EXISTS {idx_visitor} ON {table} (visitor_id);
        CREATE INDEX IF NOT EXISTS {idx_visitor_session_started} ON {table} (visitor_id, session_started_at);
        CREATE INDEX IF NOT EXISTS {idx_visitor_session_started_heartbeat} ON {table} (visitor_id, session_started_at, last_heartbeat_at DESC);
        """
    ).format(
        idx_visitor=sql.Identifier(f"{SITEANALYSIS_TABLE}_visitor_id_idx"),
        idx_visitor_session_started=sql.Identifier(
            f"{SITEANALYSIS_TABLE}_visitor_id_session_started_at_idx"
        ),
        idx_visitor_session_started_heartbeat=sql.Identifier(
            f"{SITEANALYSIS_TABLE}_visitor_id_session_started_at_last_heartbeat_at_idx"
        ),
        table=sql.Identifier(SITEANALYSIS_TABLE),
    )

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute(drop_unique_visitor_id)
            cur.execute(create_indexes)
            conn.commit()

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
        if len(EVENT_CONTAINER) >= CONTAINER_MAX:
            return jsonify({"error": "container is full", "container_max": CONTAINER_MAX}), 429
        EVENT_CONTAINER.append(event)
        size = len(EVENT_CONTAINER)

    return jsonify({"queued": True, "container_size": size}), 202


@app.post("/siteanalysis/process")
def process_siteanalysis_queue():
    global LAST_PROCESS_AT
    global LAST_PROCESS_MONOTONIC

    now = datetime.now(timezone.utc)

    now_mono = time.monotonic()
    if LAST_PROCESS_MONOTONIC is not None:
        elapsed = now_mono - LAST_PROCESS_MONOTONIC
        min_interval = float(PROCESS_INTERVAL_SECONDS)
        if elapsed < min_interval:
            remaining = max(0, int(min_interval - elapsed))
            next_allowed = now + timedelta(seconds=remaining)
            with EVENT_CONTAINER_LOCK:
                container_size = len(EVENT_CONTAINER)
                failed_buffer_size = len(FAILED_DRAINED)
            return (
                jsonify(
                    {
                        "skipped": True,
                        "reason": "already processed recently",
                        "next_allowed_in_seconds": remaining,
                        "next_allowed_at": next_allowed.isoformat(),
                        "container_size": container_size,
                        "failed_buffer_size": failed_buffer_size,
                    }
                ),
                200,
            )

    with EVENT_CONTAINER_LOCK:
        has_work = bool(FAILED_DRAINED) or bool(EVENT_CONTAINER)
    if not has_work:
        LAST_PROCESS_AT = now
        LAST_PROCESS_MONOTONIC = now_mono
        return jsonify({"processed": 0, "inserted": 0, "container_size": 0}), 200

    limit_raw = request.args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 500
    except Exception:
        return jsonify({"error": "limit must be an integer"}), 400
    if limit <= 0:
        return jsonify({"error": "limit must be > 0"}), 400

    _init_db()

    drained: List[Dict[str, Any]] = []
    with EVENT_CONTAINER_LOCK:
        if FAILED_DRAINED:
            take = min(limit, len(FAILED_DRAINED))
            drained.extend(FAILED_DRAINED[:take])
            FAILED_DRAINED[:] = FAILED_DRAINED[take:]

        remaining = limit - len(drained)
        for _ in range(min(remaining, len(EVENT_CONTAINER))):
            drained.append(EVENT_CONTAINER.popleft())

    latest_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in drained:
        visitor_id = item.get("visitor_id")
        if not isinstance(visitor_id, str) or not visitor_id:
            continue

        try:
            session_started_at = _parse_timestamptz(item.get("session_started_at"), "session_started_at")
            hb = _parse_timestamptz(item.get("last_heartbeat_at"), "last_heartbeat_at")
        except Exception:
            continue

        key = (visitor_id, session_started_at.isoformat())
        existing = latest_by_key.get(key)
        if existing is None:
            latest_by_key[key] = item
        else:
            try:
                existing_hb = _parse_timestamptz(existing.get("last_heartbeat_at"), "last_heartbeat_at")
            except Exception:
                existing_hb = datetime.min.replace(tzinfo=timezone.utc)
            if hb >= existing_hb:
                latest_by_key[key] = item

    rows: List[Tuple[Any, Any, Any, Any, Any]] = []
    for item in latest_by_key.values():
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
        return jsonify({"processed": len(drained), "inserted": 0, "container_size": len(EVENT_CONTAINER)}), 200

    values_sql = sql.SQL(", ").join(sql.SQL("(%s, %s, %s, %s, %s)") for _ in rows)
    query = sql.SQL(
        "INSERT INTO {table} (visitor_id, city, url, session_started_at, last_heartbeat_at) VALUES "
    ).format(table=sql.Identifier(SITEANALYSIS_TABLE)) + values_sql

    params: List[Any] = [v for row in rows for v in row]

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
        LAST_PROCESS_AT = now
        LAST_PROCESS_MONOTONIC = now_mono
        with EVENT_CONTAINER_LOCK:
            container_size = len(EVENT_CONTAINER)
        return jsonify({"processed": len(drained), "inserted": len(rows), "container_size": container_size}), 200
    except Exception as e:
        # Put drained items into FAILED_DRAINED so we do not lose them.
        # This is safe even if new events were enqueued while inserting.
        with EVENT_CONTAINER_LOCK:
            FAILED_DRAINED[:0] = drained
        return (
            jsonify(
                {
                    "error": str(e),
                    "processed": 0,
                    "inserted": 0,
                    "container_size": len(EVENT_CONTAINER),
                    "failed_buffer_size": len(FAILED_DRAINED),
                }
            ),
            500,
        )


@app.get("/siteanalysis/state")
def siteanalysis_state():
    with EVENT_CONTAINER_LOCK:
        container_size = len(EVENT_CONTAINER)
        failed_buffer_size = len(FAILED_DRAINED)
    return jsonify(
        {
            "process_interval_seconds": PROCESS_INTERVAL_SECONDS,
            "container_size": container_size,
            "container_max": CONTAINER_MAX,
            "db_initialized": DB_INITIALIZED,
            "last_process_at": LAST_PROCESS_AT.isoformat() if LAST_PROCESS_AT else None,
            "failed_buffer_size": failed_buffer_size,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
