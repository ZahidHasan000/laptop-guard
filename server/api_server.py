"""
Laptop Guard — Remote API Server (v2.0 Final)
===============================================
FastAPI + SQLite (prototype) / PostgreSQL (production)
Nginx/Caddy TLS reverse proxy required for HTTPS

Architecture Doc Phase 5:
  POST /api/v1/events             → receive event metadata
  POST /api/v1/events/{id}/image  → receive webcam image
  POST /api/v1/preboot/image      → receive pre-boot image (with X-Lg-Meta)
  GET  /api/v1/events             → list events (owner only)
  GET  /api/v1/events/{id}        → single event detail
  GET  /health                    → health check (no auth)

Security:
  - Bearer token (constant-time compare)
  - Image size limit (10MB)
  - UUID deduplication
  - SHA-256 image integrity
  - UTC timestamps

Run (behind Nginx/Caddy):
  pip install -r requirements.txt
  export LAPTOP_GUARD_TOKEN="your-long-random-token"
  export NOTIFY_GMAIL="you@gmail.com"
  export NOTIFY_APP_PASS="app-password"
  export NOTIFY_TO="you@gmail.com"
  # SQLite (default prototype):
  uvicorn api_server:app --host 127.0.0.1 --port 8000
  # PostgreSQL (production):
  export DATABASE_URL="postgresql://user:pass@localhost/laptopguard"
  uvicorn api_server:app --host 127.0.0.1 --port 8000
"""

import os
import json
import uuid
import hashlib
import smtplib
import logging
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse

# ─── Configuration (from environment — no secrets in code) ────────────
API_TOKEN    = os.environ.get("LAPTOP_GUARD_TOKEN", "")
SERVER_DIR   = Path(os.environ.get("LAPTOP_GUARD_DATA", "/var/lib/laptop-guard-server"))
DB_PATH      = SERVER_DIR / "events.db"
IMAGES_DIR   = SERVER_DIR / "images"
GMAIL        = os.environ.get("NOTIFY_GMAIL", "")
APP_PASS     = os.environ.get("NOTIFY_APP_PASS", "")
NOTIFY_TO    = os.environ.get("NOTIFY_TO", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # PostgreSQL if set
MAX_IMG_SIZE = 10 * 1024 * 1024  # 10MB

if not API_TOKEN:
    raise RuntimeError("Set LAPTOP_GUARD_TOKEN environment variable")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("laptop-guard-server")

# ─── Database ──────────────────────────────────────────────────────────

def get_db():
    """SQLite connection (prototype). Switch to PostgreSQL via DATABASE_URL."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id      TEXT PRIMARY KEY,
            event_type    TEXT NOT NULL,
            stage         TEXT DEFAULT '',
            timestamp_utc TEXT NOT NULL,
            device_id     TEXT DEFAULT '',
            hostname      TEXT DEFAULT '',
            boot_id       TEXT DEFAULT '',
            public_ip     TEXT DEFAULT '',
            location_json TEXT DEFAULT '{}',
            network_json  TEXT DEFAULT '{}',
            image_id      TEXT DEFAULT '',
            image_path    TEXT DEFAULT '',
            image_sha256  TEXT DEFAULT '',
            status        TEXT DEFAULT 'received',
            created_at    TEXT NOT NULL,
            received_at   TEXT NOT NULL,
            gmail_sent    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    if DATABASE_URL:
        log.info("NOTE: DATABASE_URL set — switch api_server.py to use PostgreSQL")
    log.info(f"DB: {DB_PATH} | Images: {IMAGES_DIR}")

# ─── App ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Laptop Guard Server started")
    yield

app = FastAPI(
    title="Laptop Guard Server",
    description="Evidence collection API",
    version="2.0.0",
    docs_url=None,   # disable in production
    redoc_url=None,
    lifespan=lifespan,
)

# ─── Auth ──────────────────────────────────────────────────────────────

security = HTTPBearer()

def verify_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    import hmac
    if not hmac.compare_digest(creds.credentials, API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")
    return creds.credentials

# ─── Gmail Notification ────────────────────────────────────────────────

def _send_gmail(event: dict, image_path: Path | None = None):
    """Gmail = notification only. Evidence primary storage = database."""
    if not all([GMAIL, APP_PASS, NOTIFY_TO]):
        return

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage

    loc     = json.loads(event.get("location_json", "{}"))
    net     = json.loads(event.get("network_json", "{}"))
    wifi    = net.get("wifi") or {}
    city    = loc.get("city", "?")
    country = loc.get("country", "?")
    coords  = loc.get("coordinates")
    etype   = event.get("event_type", "capture")
    badge   = "🔔 Pre-boot Alert" if etype == "preboot_capture" else "📸 Boot Capture"

    rows = [
        ("Event Type",  etype),
        ("Event ID",    event.get("event_id","?")[:16] + "..."),
        ("Device",      event.get("hostname") or event.get("device_id","?")),
        ("Timestamp",   event.get("timestamp_utc","?")),
        ("Location",    f"{city}, {country} (IP, low confidence)"),
        ("Public IP",   event.get("public_ip","?")),
        ("ISP",         loc.get("isp","?")),
        ("WiFi SSID",   wifi.get("ssid","?")),
        ("WiFi BSSID",  wifi.get("bssid","?")),
        ("SHA-256",     event.get("image_sha256","?")[:24] + "..." if event.get("image_sha256") else "N/A"),
    ]
    tbl = "".join(
        f"<tr style='background:{'#f5f5f5' if i%2==0 else '#fff'}'>"
        f"<td style='padding:8px;border:1px solid #ddd;font-weight:bold'>{k}</td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{v}</td></tr>"
        for i, (k, v) in enumerate(rows)
    )
    map_btn = (f"<p><a href='https://maps.google.com/?q={coords}' "
               f"style='background:#1565c0;color:white;padding:10px 20px;"
               f"border-radius:4px;text-decoration:none'>📍 Map</a></p>"
               ) if coords else ""

    html = (f"<html><body style='font-family:Arial,sans-serif;max-width:640px'>"
            f"<div style='background:#1a237e;color:white;padding:16px'>"
            f"<h2>{badge} — Server Alert</h2>"
            f"<p>Received at server: {datetime.now(timezone.utc).isoformat()}</p></div>"
            f"<div style='padding:20px;border:1px solid #ddd'>"
            f"<table style='border-collapse:collapse;width:100%'>{tbl}</table>"
            f"{map_btn}"
            f"<p style='background:#fff3e0;padding:12px;border-radius:4px'>"
            f"✅ Evidence stored on server. Even if laptop is wiped, evidence remains.</p>"
            f"<p style='font-size:11px;color:#999'>Gmail = notification only. "
            f"Server = primary evidence storage.</p>"
            f"</div></body></html>")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"{badge} — {city}, {country} — {event.get('timestamp_utc','')[:10]}"
    msg["From"]    = GMAIL
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(html, "html"))

    if image_path and image_path.exists():
        with open(image_path, "rb") as f:
            img_d = f.read()
        img_m = MIMEImage(img_d, _subtype="jpeg")
        img_m.add_header("Content-Disposition", "attachment",
                         filename=f"cap_{event.get('event_id','')[:8]}.jpg")
        msg.attach(img_m)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL, APP_PASS)
            s.send_message(msg)
        log.info(f"Gmail sent: {event.get('event_id')}")
    except Exception as e:
        log.error(f"Gmail failed: {e}")

# ─── Endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "laptop-guard-server", "version": "2.0.0"}


@app.post("/api/v1/events", status_code=201)
async def receive_event(request: Request, _: str = Depends(verify_token)):
    """Receive event metadata. Deduplicate by event_id UUID."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    eid = body.get("event_id", "")
    try:
        uuid.UUID(eid)
    except (ValueError, AttributeError):
        raise HTTPException(400, "Invalid event_id UUID")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        # Deduplication check
        existing = conn.execute(
            "SELECT event_id FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        if existing:
            log.info(f"Duplicate event: {eid}")
            return JSONResponse({"status": "received", "event_id": eid, "duplicate": True}, 200)

        conn.execute("""
            INSERT INTO events (
                event_id, event_type, stage, timestamp_utc, device_id,
                hostname, boot_id, public_ip, location_json, network_json,
                image_id, image_sha256, status, created_at, received_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            eid,
            body.get("event_type", "capture"),
            body.get("stage", ""),
            body.get("timestamp", now),
            body.get("device_id", ""),
            body.get("hostname", ""),
            body.get("boot_id", ""),
            body.get("public_ip", ""),
            json.dumps(body.get("location", {})),
            json.dumps(body.get("network", {})),
            body.get("image_id", ""),
            body.get("image_sha256", ""),
            "received",
            body.get("created_at", now),
            now,
        ))
        conn.commit()
        log.info(f"Event stored: {eid} type={body.get('event_type')} device={body.get('device_id')}")
    finally:
        conn.close()

    asyncio.create_task(asyncio.to_thread(
        _send_gmail, {**body, "location_json": json.dumps(body.get("location", {})),
                      "network_json": json.dumps(body.get("network", {})),
                      "timestamp_utc": body.get("timestamp", now)}, None
    ))
    return JSONResponse({"status": "received", "event_id": eid, "duplicate": False}, 201)


@app.post("/api/v1/events/{event_id}/image", status_code=201)
async def receive_image(event_id: str, request: Request, _: str = Depends(verify_token)):
    """Receive webcam image. Verify SHA-256 if event has hash."""
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError):
        raise HTTPException(400, "Invalid event_id")

    # Size limit
    content_len = request.headers.get("content-length")
    if content_len and int(content_len) > MAX_IMG_SIZE:
        raise HTTPException(413, f"Image too large (max {MAX_IMG_SIZE//1024//1024}MB)")

    img_data = await request.body()
    if not img_data:
        raise HTTPException(400, "Empty image")
    if len(img_data) > MAX_IMG_SIZE:
        raise HTTPException(413, "Image too large")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT image_sha256 FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Event not found")

        # Integrity check
        received_hash = hashlib.sha256(img_data).hexdigest()
        stored_hash   = row["image_sha256"]
        if stored_hash and stored_hash != received_hash:
            log.warning(f"SHA-256 mismatch for {event_id}: expected={stored_hash[:16]}, got={received_hash[:16]}")

        # Save image
        img_path = IMAGES_DIR / f"{event_id}.jpg"
        img_path.write_bytes(img_data)
        img_path.chmod(0o600)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE events SET image_path=?, image_sha256=?, status='complete' WHERE event_id=?",
            (str(img_path), received_hash, event_id)
        )
        conn.commit()
        log.info(f"Image stored: {event_id} ({len(img_data)} bytes) sha256={received_hash[:16]}...")

        ev = dict(conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())
    finally:
        conn.close()

    asyncio.create_task(asyncio.to_thread(_send_gmail, ev, img_path))
    return {"status": "stored", "event_id": event_id, "sha256": received_hash}


@app.post("/api/v1/preboot/image", status_code=201)
async def receive_preboot_image(request: Request, _: str = Depends(verify_token)):
    """
    Receive pre-boot image with metadata in X-Lg-Meta header.
    Called by initramfs lg-capture-script directly.
    """
    meta_str = request.headers.get("x-lg-meta", "{}")
    try:
        meta = json.loads(meta_str)
    except Exception:
        meta = {}

    eid = meta.get("event_id") or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    img_data = await request.body()
    if len(img_data) > MAX_IMG_SIZE:
        raise HTTPException(413, "Image too large")

    img_hash = hashlib.sha256(img_data).hexdigest() if img_data else ""
    img_path  = IMAGES_DIR / f"preboot_{eid}.jpg"
    if img_data:
        img_path.write_bytes(img_data)
        img_path.chmod(0o600)

    conn = get_db()
    try:
        existing = conn.execute("SELECT event_id FROM events WHERE event_id=?", (eid,)).fetchone()
        if existing:
            return JSONResponse({"status": "received", "event_id": eid, "duplicate": True}, 200)

        conn.execute("""
            INSERT INTO events (
                event_id, event_type, stage, timestamp_utc, device_id,
                public_ip, location_json, network_json, image_id,
                image_path, image_sha256, status, created_at, received_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            eid,
            meta.get("event_type", "preboot_capture"),
            meta.get("stage", "pre_luks"),
            meta.get("timestamp", now),
            meta.get("device_id", ""),
            meta.get("public_ip", ""),
            json.dumps(meta.get("location", {})),
            json.dumps(meta.get("network", {})),
            eid,
            str(img_path) if img_data else "",
            img_hash,
            "complete" if img_data else "received",
            now, now,
        ))
        conn.commit()
        log.info(f"Pre-boot event from initramfs: {eid}")
    finally:
        conn.close()

    ev_for_mail = {
        "event_id": eid, "event_type": "preboot_capture",
        "hostname": "", "device_id": meta.get("device_id",""),
        "timestamp_utc": meta.get("timestamp", now),
        "public_ip": meta.get("public_ip",""),
        "location_json": json.dumps(meta.get("location",{})),
        "network_json":  json.dumps(meta.get("network",{})),
        "image_sha256":  img_hash,
    }
    asyncio.create_task(asyncio.to_thread(
        _send_gmail, ev_for_mail, img_path if img_data else None
    ))
    return JSONResponse({"status": "received", "event_id": eid}, 201)


@app.get("/api/v1/events")
def list_events(limit: int = 20, offset: int = 0, _: str = Depends(verify_token)):
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        rows  = conn.execute(
            """SELECT event_id, event_type, stage, timestamp_utc, hostname,
                      device_id, public_ip, location_json, status,
                      received_at, image_sha256,
                      CASE WHEN image_path!='' THEN 1 ELSE 0 END as has_image
               FROM events ORDER BY received_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        events = []
        for r in rows:
            e = dict(r)
            e["location"] = json.loads(e.pop("location_json", "{}"))
            events.append(e)
        return {"total": total, "limit": limit, "offset": offset, "events": events}
    finally:
        conn.close()


@app.get("/api/v1/events/{event_id}")
def get_event(event_id: str, _: str = Depends(verify_token)):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Event not found")
        e = dict(row)
        e["location"] = json.loads(e.pop("location_json", "{}"))
        e["network"]  = json.loads(e.pop("network_json", "{}"))
        e["has_image"] = bool(e.get("image_path"))
        return e
    finally:
        conn.close()