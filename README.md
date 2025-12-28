# Webpage Analysis Server

A small **Flask + Postgres** service for **webpage-embedded analysis tracking**.

Designed for setups like **Render free tier**, where your service may sleep and you still want the **webpage heartbeat** to stay fast.

---

## How it works (in 60 seconds)

This server is intentionally split into two responsibilities:

### 1) Enqueue (fast path)

Your webpage calls:

- `POST /siteanalysis/enqueue`

The server:

- validates the JSON
- adds `last_heartbeat_at = now()`
- appends the event into an in-memory container (`EVENT_CONTAINER`)

No database work happens here.

### 2) Process + Insert (batch path)

A cron/worker calls:

- `POST /siteanalysis/process?limit=500`

The server:

- only inserts when the server clock is exactly **`hh:mm:00`**
- de-dupes events by `(visitor_id, session_started_at)`
  - keeps **only the latest** `last_heartbeat_at` per key
- inserts all rows using **one bulk INSERT query**

---

## Operating model (important)

- **Time gate:** `/siteanalysis/process` will only insert when the current server time is exactly `hh:mm:00`.
- **Interval gate:** it also enforces `PROCESS_INTERVAL_SECONDS` between successful runs.
- **Retry buffer:** if a DB insert fails, the drained batch is kept server-side and retried on the next processor call.
- **Backpressure:** `/siteanalysis/enqueue` returns `429` when `CONTAINER_MAX` is reached.

## Features

- Global CORS enabled
- Postgres connection via `DATABASE_URL` using `psycopg[binary]`
- In-memory container (bounded): keeps incoming events until processor runs
  - backpressure: returns **429** if container is full
- De-dupe rule: **same visitor_id + same session_started_at → keep only the latest heartbeat**
- DB init happens only on the processor endpoint (first call per container)
- Safe batching: enqueue can continue while a batch is inserting (no loss)

---

## Table Schema

Table: `siteanalysis`

Columns:

- `id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
- `visitor_id TEXT NOT NULL`
- `city TEXT`
- `url TEXT`
- `session_started_at TIMESTAMPTZ NOT NULL`
- `last_heartbeat_at TIMESTAMPTZ NOT NULL`

Indexes are created on:

- `(visitor_id)`
- `(visitor_id, session_started_at)`
- `(visitor_id, session_started_at, last_heartbeat_at DESC)`

---

## Quick Start (Copy/Paste)

### 1) Install

```bash
pip install -r requirements.txt
```

### 2) Run locally

```bash
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
python app.py
```

---

## Production checklist

- **Enqueue endpoint** should be called by the website.
- **Process endpoint** must be called by a cron/worker every minute.
- Your cron should ideally call close to `hh:mm:00` (e.g. `:00` second) so inserts actually run.

---

## Best JS Embed (Copy/Paste)

Paste this on your website. It will:

- persist a stable `visitor_id`
- keep one `session_started_at` value per page load
- send a heartbeat every `10s`

```html
<script>
(function () {

  /* ------------------------------
     CONFIG
     ------------------------------ */
  const API_ENDPOINT = "https://your-api.com/collect"; // unchanged
  const analytics_HEARTBEAT_INTERVAL = 10_000; // 10 seconds

  /* ------------------------------
     VISITOR ID (cached per browser)
     ------------------------------ */
  function analytics_generateVisitorId() {
    return (
      "V" +
      Date.now().toString(36).slice(-4).toUpperCase() +
      Math.random().toString(36).slice(2, 6).toUpperCase()
    );
  }

  let analytics_visitor_id = localStorage.getItem("visitor_id");
  if (!analytics_visitor_id) {
    analytics_visitor_id = analytics_generateVisitorId();
    localStorage.setItem("visitor_id", analytics_visitor_id);
  }

  /* ------------------------------
     SESSION START
     ------------------------------ */
  const analytics_session_started_at = new Date().toISOString();

  /* ------------------------------
     CURRENT URL
     ------------------------------ */
  function analytics_getCurrentUrl() {
    return window.location.href;
  }

  /* ------------------------------
     CITY (fetch once per session)
     ------------------------------ */
  let analytics_city = null;

  (async function analytics_fetchCityOnce() {
    try {
      const res = await fetch("https://ipapi.co/json/", {
        cache: "no-store",
        mode: "cors"
      });

      if (res.ok) {
        const data = await res.json();
        analytics_city =
          typeof data.city === "string" ? data.city : null;
      }
    } catch {
      analytics_city = null; // VPN / ad blocker / network issue
    }
  })();

  /* ------------------------------
     HEARTBEAT LOOP
     ------------------------------ */
  setInterval(() => {
    const analytics_payload = {
      visitor_id: analytics_visitor_id,
      city: analytics_city,
      current_url: analytics_getCurrentUrl(),
      session_started_at: analytics_session_started_at
    };

    fetch(API_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(analytics_payload),
      keepalive: true
    }).catch(() => {
      /* silent failure */
    });

  }, analytics_HEARTBEAT_INTERVAL);

  /* ------------------------------
     FINAL SEND ON PAGE EXIT
     ------------------------------ */
  window.addEventListener("pagehide", () => {
    const analytics_payload = {
      visitor_id: analytics_visitor_id,
      city: analytics_city,
      current_url: analytics_getCurrentUrl(),
      session_started_at: analytics_session_started_at
    };

    navigator.sendBeacon(
      API_ENDPOINT,
      JSON.stringify(analytics_payload)
    );
  });

})();
</script>

<!-- End of Analytics Script -->

```

---

## API

### Health

`GET /health`

```json
{"status":"ok"}
```

### 1) Enqueue (webpage calls this)

`POST /siteanalysis/enqueue`

Input JSON:

- `visitor_id` (string, required)
- `city` (string, optional)
- `url` (string, optional)
- `session_started_at` (string, required, ISO-8601)

Server behavior:

- sets `last_heartbeat_at` to current time
- stores the enriched event in the global container

Backpressure:

- returns **`429`** if the container is full (`CONTAINER_MAX` reached)

Response:

- `202 Accepted`

### 2) Process (cron/worker calls this)

`POST /siteanalysis/process?limit=500`

Rules:

- **time gate:** inserts only when current time is exactly `hh:mm:00` (server time)
- **interval gate:** will only do work if at least `PROCESS_INTERVAL_SECONDS` have passed
- it does nothing if the container is empty
- it de-dupes `(visitor_id, session_started_at)` keeping the latest `last_heartbeat_at`
- inserts all rows with **one bulk INSERT query**

Reliability note:

- if the DB insert fails, drained events are kept for retry (in a server-side buffer) and will be retried on the next process call.

---

## Environment Variables

Required:

- `DATABASE_URL`

Optional:

- `PORT` (default `8000`)
- `PROCESS_INTERVAL_SECONDS` (default `60`)
- `CONTAINER_MAX` (default `10000`)

---

## Example cron call

```bash
curl -X POST "https://YOUR-RENDER-URL/siteanalysis/process?limit=500"
```

---

## Render Deployment (Recommended)

### Web Service

- **Start command**

```bash
python app.py
```

- Set `DATABASE_URL`

### Processor Trigger (every 60s)

You need something to call the processor endpoint every minute.

Example request:

```bash
curl -X POST "https://YOUR-RENDER-URL/siteanalysis/process?limit=500"
```

Good options:

- Render Cron Job (if available)
- GitHub Actions scheduled workflow
- Any external cron service

---

## Important Notes (Production)

- **The container is in-memory.** If the service restarts/sleeps, queued events are lost.
- If you need durability, the next step is a staging table or a proper queue (Redis/SQS).
- `/siteanalysis/process` is currently unauthenticated; for production consider a shared secret header.

---

## Architecture (Detailed)

### Why two endpoints?

Your webpage calls an endpoint frequently (every few seconds). That endpoint should be:

- fast
- low-latency
- not dependent on database health

So `enqueue` only validates + stores in memory.

### What exactly is stored in memory?

Each `enqueue` call becomes an in-memory event object with:

- the input fields (`visitor_id`, `city`, `url`, `session_started_at`)
- `last_heartbeat_at` set by the server to the current time

Events are stored in a bounded container:

- `EVENT_CONTAINER` (max length controlled by `CONTAINER_MAX`)

### How dedupe works

During `process`, the server de-dupes in memory using a key made from:

- `visitor_id`
- `session_started_at`

If multiple events arrive with the same key, it keeps only the one with the **latest** `last_heartbeat_at`.

This matches the rule: “same visitor + same session start = one final row”.

### Processor timing gate

The processor endpoint enforces a minimum interval:

- `PROCESS_INTERVAL_SECONDS` (default 60)

If it is called early, it returns a JSON response telling you how many seconds remain.

### Database initialization

The table and indexes are created only when processing starts (first processor run per container). This avoids extra work on the hot path.

### Bulk insert strategy

For each processing run, the server inserts all deduped rows using:

- **one SQL INSERT statement** with multiple `VALUES` rows

This reduces round-trips and is the fastest approach for small-to-medium batches.
