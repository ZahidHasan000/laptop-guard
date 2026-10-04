#!/usr/bin/env python3
"""
Laptop Guard — Final Working Version
=====================================
All fixes applied based on real device testing (HP EliteBook 845 G10 / Ubuntu 24.04)

Fixes included:
  ✅ WiFi SSID/BSSID: separate nmcli --escape no calls (colon conflict fixed)
  ✅ Camera: skip=10 (warm-up for HP 5MP Camera)
  ✅ Camera: auto-detect /dev/video0..3
  ✅ BeaconDB: WiFi-based geolocation
  ✅ Armed/Disarmed: .disarmed file control
  ✅ --mode full/upload: NM dispatcher no longer triggers camera
  ✅ Pre-boot pickup: /run/lg-handoff/ integration
  ✅ MAX_QUEUE_BYTES: disk space protection
  ✅ VPN detection
  ✅ UTC timestamps throughout
  ✅ HTTP-aware retry (4xx no retry, 5xx retry)

Usage:
  python3 tracker.py               = full capture (boot)
  python3 tracker.py --mode upload = queue upload only (network/resume)
  python3 tracker.py --mode full   = force full capture (test)
"""

import os, sys, json, uuid, time, hashlib, logging, socket
import subprocess, tempfile, smtplib, shutil, glob, argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

try:
    from cryptography.fernet import Fernet
    ENC_OK = True
except ImportError:
    ENC_OK = False

# ─── Paths ────────────────────────────────────────────────────────────
CONFIG_DIR  = Path("/etc/laptop-guard")
BASE_DIR    = Path("/var/lib/laptop-guard")
QUEUE_DIR   = BASE_DIR / "queue"
UPLOAD_DIR  = BASE_DIR / "uploaded"
LOG_FILE    = BASE_DIR / "laptop-guard.log"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE    = CONFIG_DIR / "guard.key"
DEV_FILE    = CONFIG_DIR / "device-id"
DISARM_FILE = CONFIG_DIR / ".disarmed"
LAST_RUN    = BASE_DIR / ".last_run"
HANDOFF_DIR = Path("/run/lg-handoff")

# ─── Constants ────────────────────────────────────────────────────────
MIN_HOURS       = 4
MAX_QUEUE_ITEMS = 50
MAX_QUEUE_BYTES = 150 * 1024 * 1024  # 150MB
TIMEOUT         = 15

BASE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("laptop-guard")


# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════

class Config:
    def __init__(self):
        if not CONFIG_FILE.exists():
            raise FileNotFoundError(f"Run setup.sh first: {CONFIG_FILE}")
        with open(CONFIG_FILE) as f:
            self._d = json.load(f)

    @property
    def gmail(self):        return self._d["gmail"]
    @property
    def app_password(self): return self._d["app_password"]
    @property
    def to_email(self):     return self._d.get("to_email", self._d["gmail"])
    @property
    def api_url(self):
        u = self._d.get("api_url", "").strip()
        return u if u else None
    @property
    def api_token(self):    return self._d.get("api_token", "").strip() or None

    @property
    def device_id(self):
        if DEV_FILE.exists():
            return DEV_FILE.read_text().strip()
        nid = str(uuid.uuid4())
        DEV_FILE.write_text(nid)
        DEV_FILE.chmod(0o600)
        return nid


# ══════════════════════════════════════════════════════
# ENCRYPTION
# ══════════════════════════════════════════════════════

class Encryptor:
    def __init__(self):
        if not ENC_OK:
            log.warning("cryptography not installed — queue unencrypted")
            self._f = None
            return
        if not KEY_FILE.exists():
            raise FileNotFoundError(f"Key missing: {KEY_FILE}")
        from cryptography.fernet import Fernet as _F
        self._f = _F(KEY_FILE.read_bytes().strip())

    def encrypt(self, d): return self._f.encrypt(d) if self._f else d
    def decrypt(self, d): return self._f.decrypt(d) if self._f else d
    @property
    def enabled(self): return self._f is not None


# ══════════════════════════════════════════════════════
# EVIDENCE COLLECTOR
# ══════════════════════════════════════════════════════

class EvidenceCollector:

    def boot_id(self):
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except Exception:
            return "unknown"

    def find_camera(self):
        """Auto-detect camera — no hardcoded /dev/video0."""
        for dev in ["/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3"]:
            if Path(dev).exists():
                return dev
        return None

    def detect_vpn(self):
        try:
            r = subprocess.run(["ip", "link", "show"],
                               capture_output=True, text=True, timeout=3)
            for name in ["tun", "tap", "wg", "proton", "nordlynx", "mullvad"]:
                if name in r.stdout:
                    return True
        except Exception:
            pass
        return False

    def get_wifi(self):
        """
        WiFi SSID + BSSID — fix for HP EliteBook.
        Uses separate nmcli --escape no calls to avoid
        BSSID colon (AA:BB:CC) conflicting with field separator.
        Verified working: SSID=Zahid, BSSID=74:DA:88:71:62:52
        """
        wifi = {"ssid": "?", "bssid": "?", "signal": "?"}
        try:
            # SSID — no colon issue
            r1 = subprocess.run(
                ["nmcli", "--escape", "no", "-t", "-f", "active,ssid", "dev", "wifi"],
                capture_output=True, text=True, timeout=5)
            for ln in r1.stdout.splitlines():
                if ln.startswith("yes:"):
                    wifi["ssid"] = ln[4:].strip()
                    break

            # BSSID — --escape no removes backslashes from 74\:DA\:88 → 74:DA:88
            r2 = subprocess.run(
                ["nmcli", "--escape", "no", "-t", "-f", "active,bssid", "dev", "wifi"],
                capture_output=True, text=True, timeout=5)
            for ln in r2.stdout.splitlines():
                if ln.startswith("yes:"):
                    wifi["bssid"] = ln[4:].strip()
                    break

            # Signal (optional, failure OK)
            r3 = subprocess.run(
                ["nmcli", "--escape", "no", "-t", "-f", "active,signal", "dev", "wifi"],
                capture_output=True, text=True, timeout=3)
            for ln in r3.stdout.splitlines():
                if ln.startswith("yes:"):
                    wifi["signal"] = ln[4:].strip()
                    break

        except Exception as e:
            log.warning(f"WiFi info: {e}")
        return wifi

    def wifi_geolocation(self, bssid):
        """BeaconDB WiFi location — more accurate than IP. Free, no API key."""
        if not bssid or bssid == "?":
            return None
        try:
            payload = json.dumps(
                {"wifiAccessPoints": [{"macAddress": bssid}]}
            ).encode()
            req = Request(
                "https://api.beacondb.net/v1/geolocate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=8) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode())
                    loc  = data.get("location", {})
                    if loc.get("lat") and loc.get("lng"):
                        log.info(f"BeaconDB: {loc}")
                        return {
                            "lat":      loc["lat"],
                            "lng":      loc["lng"],
                            "accuracy": data.get("accuracy", "?"),
                        }
        except Exception as e:
            log.debug(f"BeaconDB: {e}")
        return None

    def get_location(self, bssid="?"):
        """IP geolocation + optional BeaconDB WiFi location."""
        vpn = self.detect_vpn()
        loc = {
            "type":       "ip_geolocation",
            "confidence": "low",
            "vpn_active": vpn,
        }
        try:
            req = Request("https://ipinfo.io/json",
                          headers={"User-Agent": "curl/7.81.0"})
            with urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
            public_ip = d.get("ip", "unknown")
            loc.update({
                "city":        d.get("city", "?"),
                "region":      d.get("region", "?"),
                "country":     d.get("country", "?"),
                "coordinates": d.get("loc"),
                "isp":         d.get("org", "?"),
                "note":        "VPN active — location is VPN exit" if vpn else "",
            })
        except Exception as e:
            log.warning(f"IP location: {e}")
            public_ip = "unavailable"

        # BeaconDB for better accuracy
        wifi_coords = self.wifi_geolocation(bssid)
        if wifi_coords:
            loc["wifi_coordinates"] = wifi_coords
            loc["wifi_confidence"]  = "medium"

        return public_ip, loc

    def get_network(self):
        """Interface list + WiFi details."""
        s = {"interfaces": [], "wifi": None}
        try:
            r = subprocess.run(["ip", "-j", "addr"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                s["interfaces"] = [
                    {"name": i.get("ifname"), "state": i.get("operstate")}
                    for i in json.loads(r.stdout)
                    if i.get("operstate") == "UP" and i.get("ifname") != "lo"
                ]
        except Exception:
            pass

        wifi = self.get_wifi()
        if wifi["ssid"] != "?" or wifi["bssid"] != "?":
            wifi["note"] = "BSSID = AP identifier, not direct location"
            s["wifi"] = wifi

        return s

    def capture_webcam(self):
        """
        Webcam capture — skip=10 for HP 5MP Camera warm-up.
        Verified working on HP EliteBook 845 G10.
        """
        cam = self.find_camera()
        if not cam:
            log.warning("No camera device found")
            return None

        img = tempfile.mktemp(suffix=".jpg", prefix="lg_")

        # fswebcam — skip=10 for warm-up (HP camera needs this)
        try:
            r = subprocess.run(
                ["fswebcam", "--device", cam, "--resolution", "1280x720",
                 "--no-banner", "--quiet", "--skip", "10", img],
                capture_output=True, timeout=25)
            if r.returncode == 0 and Path(img).exists() and Path(img).stat().st_size > 0:
                log.info(f"Webcam OK via fswebcam: {cam}")
                return img
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"fswebcam: {e}")

        # ffmpeg fallback
        try:
            r = subprocess.run(
                ["ffmpeg", "-loglevel", "quiet", "-f", "v4l2",
                 "-i", cam, "-vframes", "1", "-q:v", "2", "-y", img],
                capture_output=True, timeout=25)
            if r.returncode == 0 and Path(img).exists() and Path(img).stat().st_size > 0:
                log.info(f"Webcam OK via ffmpeg: {cam}")
                return img
        except Exception as e:
            log.warning(f"ffmpeg: {e}")

        return None

    def build_event(self, cfg, etype):
        now = datetime.now(timezone.utc).isoformat()
        net = self.get_network()

        # Get WiFi BSSID for BeaconDB
        bssid = "?"
        if net.get("wifi"):
            bssid = net["wifi"].get("bssid", "?")

        public_ip, loc = self.get_location(bssid)

        return {
            "event_id":       str(uuid.uuid4()),
            "event_type":     etype,
            "timestamp":      now,
            "device_id":      cfg.device_id,
            "hostname":       socket.gethostname(),
            "boot_id":        self.boot_id(),
            "public_ip":      public_ip,
            "location":       loc,
            "network":        net,
            "image_captured": False,
            "image_id":       None,
            "image_sha256":   None,
            "status":         "pending",
            "created_at":     now,
            "uploaded_at":    None,
        }


# ══════════════════════════════════════════════════════
# LOCAL QUEUE
# ══════════════════════════════════════════════════════

class LocalQueue:
    def __init__(self, enc):
        self.enc = enc
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def enqueue(self, event, image_data):
        eid = event["event_id"]
        if image_data:
            import base64
            event["image_captured"] = True
            event["image_id"]       = eid
            event["image_sha256"]   = hashlib.sha256(image_data).hexdigest()
            payload = json.dumps(
                {"event": event, "image_b64": base64.b64encode(image_data).decode()},
                ensure_ascii=False).encode()
        else:
            payload = json.dumps(
                {"event": event, "image_b64": None},
                ensure_ascii=False).encode()

        enc  = self.enc.encrypt(payload)
        tmp  = QUEUE_DIR / f"{eid}.tmp"
        fin  = QUEUE_DIR / f"{eid}.lgq"
        tmp.write_bytes(enc)
        tmp.rename(fin)
        sz = len(enc) // 1024
        log.info(f"Queued: {eid} ({sz}KB, encrypted={self.enc.enabled})")
        self._trim()
        return eid

    def pending(self):
        return sorted(QUEUE_DIR.glob("*.lgq"))

    def load(self, p):
        return json.loads(self.enc.decrypt(p.read_bytes()).decode())

    def mark_uploaded(self, p, ts):
        shutil.move(str(p), str(UPLOAD_DIR / p.name))
        log.info(f"Uploaded: {p.name}")

    def _trim(self):
        items = self.pending()
        # Count limit
        while len(items) > MAX_QUEUE_ITEMS:
            items[0].unlink()
            log.warning(f"Queue trim (count): {items[0].name}")
            items = items[1:]
        # Byte limit
        total = sum(f.stat().st_size for f in items if f.exists())
        while total > MAX_QUEUE_BYTES and items:
            sz = items[0].stat().st_size if items[0].exists() else 0
            items[0].unlink()
            log.warning(f"Queue trim (bytes {total//1024//1024}MB)")
            total -= sz
            items = items[1:]


# ══════════════════════════════════════════════════════
# UPLOADER
# ══════════════════════════════════════════════════════

class Uploader:
    def __init__(self, cfg): self.cfg = cfg

    def to_api(self, event, image_data):
        if not self.cfg.api_url:
            return False
        base = self.cfg.api_url.rstrip("/")
        tok  = self.cfg.api_token or ""
        hdrs = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        try:
            req = Request(f"{base}/api/v1/events",
                          data=json.dumps(event).encode(), headers=hdrs, method="POST")
            with urlopen(req, timeout=TIMEOUT) as r:
                resp = json.loads(r.read().decode())
            if resp.get("status") != "received":
                log.warning(f"API: {resp}"); return False
            log.info(f"API ACK: {event['event_id']}")
        except HTTPError as e:
            if e.code in (400, 401, 403):
                log.error(f"API {e.code} — no retry"); return True
            raise
        except Exception as e:
            log.warning(f"API: {e}"); return False

        if image_data:
            try:
                ireq = Request(
                    f"{base}/api/v1/events/{event['event_id']}/image",
                    data=image_data,
                    headers={"Authorization": f"Bearer {tok}",
                             "Content-Type": "image/jpeg"},
                    method="POST")
                with urlopen(ireq, timeout=30) as r:
                    log.info(f"Image: HTTP {r.status}")
            except Exception as e:
                log.warning(f"Image upload: {e}")
        return True

    def notify_gmail(self, event, image_data):
        """Gmail = notification only. Evidence primary = server/queue."""
        loc    = event.get("location", {})
        net    = event.get("network", {})
        wifi   = net.get("wifi") or {}
        city   = loc.get("city", "?")
        country = loc.get("country", "BD")
        coords  = loc.get("coordinates")
        vpn     = loc.get("vpn_active", False)
        etype   = event.get("event_type", "capture")
        wifi_c  = loc.get("wifi_coordinates")

        if etype == "preboot_capture":
            badge = "🔔 Pre-boot Alert"
        elif etype == "resume_capture":
            badge = "⏰ Resume Alert"
        else:
            badge = "📸 Boot Capture"

        rows = [
            ("Event Type",  etype),
            ("Event ID",    event.get("event_id", "?")[:16] + "..."),
            ("Device",      event.get("hostname") or event.get("device_id", "?")),
            ("Timestamp",   event.get("timestamp", "?")),
            ("Location",    f"{city}, {country} (IP, low confidence{', VPN' if vpn else ''})"),
            ("Public IP",   event.get("public_ip", "?")),
            ("ISP",         loc.get("isp", "?")),
            ("WiFi SSID",   wifi.get("ssid", "?")),
            ("WiFi BSSID",  wifi.get("bssid", "?")),
        ]

        tbl = "".join(
            f"<tr style='background:{'#f5f5f5' if i%2==0 else '#fff'}'>"
            f"<td style='padding:8px;border:1px solid #ddd;font-weight:bold'>{k}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{v}</td></tr>"
            for i, (k, v) in enumerate(rows))

        # Map buttons — WiFi location (green) + IP location (blue)
        if wifi_c and wifi_c.get("lat"):
            wmap = f"https://maps.google.com/?q={wifi_c['lat']},{wifi_c['lng']}"
            imap = f"https://maps.google.com/?q={coords}" if coords else None
            map_btn = (
                f"<p><a href='{wmap}' style='background:#2e7d32;color:white;"
                f"padding:10px 20px;border-radius:4px;text-decoration:none;"
                f"margin-right:8px'>📍 WiFi Location (better accuracy)</a>"
                + (f"<a href='{imap}' style='background:#1565c0;color:white;"
                   f"padding:10px 20px;border-radius:4px;text-decoration:none'>"
                   f"📍 IP Location</a>" if imap else "")
                + "</p>")
        elif coords:
            map_btn = (f"<p><a href='https://maps.google.com/?q={coords}' "
                       f"style='background:#1565c0;color:white;padding:10px 20px;"
                       f"border-radius:4px;text-decoration:none'>📍 Map</a></p>")
        else:
            map_btn = ""

        html = (
            f"<html><body style='font-family:Arial,sans-serif;max-width:640px'>"
            f"<div style='background:#b71c1c;color:white;padding:16px'>"
            f"<h2>{badge}</h2><p>{event.get('timestamp','')}</p></div>"
            f"<div style='padding:20px;border:1px solid #ddd'>"
            f"<table style='border-collapse:collapse;width:100%'>{tbl}</table>"
            f"{map_btn}"
            f"{'<p style=background:#fff9c4;padding:8px>⚠ VPN active — location is VPN exit node</p>' if vpn else ''}"
            f"<p style='font-size:11px;color:#999'>"
            f"IP location = low confidence. BSSID = AP identifier only. "
            f"Gmail = notification. Server = primary.</p>"
            f"</div></body></html>")

        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"{badge} — {city}, {country} — {event.get('timestamp','')[:10]}"
        msg["From"]    = self.cfg.gmail
        msg["To"]      = self.cfg.to_email
        msg.attach(MIMEText(html, "html"))

        if image_data:
            img = MIMEImage(image_data, _subtype="jpeg")
            img.add_header("Content-Disposition", "attachment",
                           filename=f"cap_{event.get('event_id','')[:8]}.jpg")
            msg.attach(img)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(self.cfg.gmail, self.cfg.app_password)
            s.send_message(msg)
        log.info(f"Gmail sent: {self.cfg.to_email}")


# ══════════════════════════════════════════════════════
# PRE-BOOT PICKUP
# ══════════════════════════════════════════════════════

class PreBootPickup:
    """Pick up initramfs pre-LUKS evidence from /run/lg-handoff/."""
    def __init__(self, cfg, queue): self.cfg=cfg; self.queue=queue

    def process(self):
        if not HANDOFF_DIR.exists(): return
        mf = HANDOFF_DIR / "meta.json"
        if not mf.exists(): self._clean(); return
        try:
            meta = json.loads(mf.read_text())
            log.info(f"Pre-boot handoff: stage={meta.get('stage')}")
        except Exception as e:
            log.error(f"Handoff: {e}"); self._clean(); return

        imgf = HANDOFF_DIR / "capture.jpg"
        img  = imgf.read_bytes() if imgf.exists() else None
        sha  = hashlib.sha256(img).hexdigest() if img else None
        now  = datetime.now(timezone.utc).isoformat()

        event = {
            "event_id":       str(uuid.uuid4()),
            "event_type":     "preboot_capture",
            "stage":          meta.get("stage", "pre_luks"),
            "timestamp":      meta.get("timestamp", now),
            "device_id":      self.cfg.device_id,
            "hostname":       "",
            "boot_id":        "",
            "public_ip":      meta.get("public_ip", "unknown"),
            "location":       meta.get("location", {
                "type": "ip_geolocation", "confidence": "low"}),
            "network":        meta.get("network", {}),
            "image_captured": img is not None,
            "image_id":       str(uuid.uuid4()) if img else None,
            "image_sha256":   sha,
            "status":         "pending",
            "created_at":     now,
            "uploaded_at":    None,
        }
        self.queue.enqueue(event, img)
        log.info(f"Pre-boot queued: {event['event_id']}")
        self._clean()

    def _clean(self):
        for f in HANDOFF_DIR.glob("*"):
            try: f.unlink()
            except: pass
        try: HANDOFF_DIR.rmdir()
        except: pass


# ══════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════

def net_ok():
    try: socket.create_connection(("8.8.8.8", 53), timeout=5); return True
    except: return False

def rate_limited():
    if not LAST_RUN.exists(): return False
    try:
        h = (time.time() - float(LAST_RUN.read_text().strip())) / 3600
        if h < MIN_HOURS: log.info(f"Rate limit: {h:.1f}h"); return True
    except: pass
    return False

def is_disarmed():
    """Owner creates .disarmed to pause captures (queue still uploads)."""
    return DISARM_FILE.exists()

def flush_queue(q, up):
    items = q.pending()
    if not items: return
    log.info(f"Flushing queue: {len(items)} item(s)")
    for qf in items:
        try:
            pl  = q.load(qf)
            ev  = pl["event"]
            img = None
            if pl.get("image_b64"):
                import base64; img = base64.b64decode(pl["image_b64"])

            done = False
            ts   = datetime.now(timezone.utc).isoformat()

            if up.cfg.api_url:
                try: done = up.to_api(ev, img)
                except Exception as e: log.warning(f"API: {e}")

            if not ev.get("_gm"):
                try:
                    up.notify_gmail(ev, img)
                    ev["_gm"] = True
                    done = done or True
                except Exception as e: log.warning(f"Gmail: {e}")

            if done:
                ev["status"] = "uploaded"; ev["uploaded_at"] = ts
                q.mark_uploaded(qf, ts)
            else:
                log.warning(f"Upload failed: {qf.name}"); break
            time.sleep(2)
        except Exception as e:
            log.error(f"Flush error {qf.name}: {e}"); break


# ══════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════

class LaptopGuard:
    def __init__(self):
        self.cfg = Config()
        self.enc = Encryptor()
        self.q   = LocalQueue(self.enc)
        self.up  = Uploader(self.cfg)
        self.col = EvidenceCollector()

    def run_full(self):
        """Full mode: pre-boot pickup + capture + upload. Used by boot trigger."""
        log.info(f"=== FULL MODE start device={self.cfg.device_id} ===")

        # Pre-boot handoff from initramfs
        try: PreBootPickup(self.cfg, self.q).process()
        except Exception as e: log.warning(f"Preboot: {e}")

        online = net_ok()
        if online: flush_queue(self.q, self.up)

        # Armed/Disarmed check
        if is_disarmed():
            log.info("DISARMED — skipping capture, queue uploaded")
            sys.exit(0)

        if rate_limited():
            sys.exit(0)

        # Evidence collection
        log.info("Collecting os_boot_capture...")
        event = self.col.build_event(self.cfg, "os_boot_capture")
        ip    = self.col.capture_webcam()
        img   = None
        if ip:
            try: img = Path(ip).read_bytes()
            except Exception as e: log.warning(f"Img read: {e}")

        self.q.enqueue(event, img)
        LAST_RUN.write_text(str(time.time()))

        if online: flush_queue(self.q, self.up)

        if ip:
            try: os.unlink(ip)
            except: pass
        log.info("=== FULL MODE end ===")

    def run_upload_only(self):
        """Upload-only mode: flush queue, no camera. Used by NM dispatcher + resume."""
        log.info("=== UPLOAD MODE start ===")
        if net_ok():
            try: PreBootPickup(self.cfg, self.q).process()
            except: pass
            flush_queue(self.q, self.up)
        else:
            log.info("Upload mode: no network")
        log.info("=== UPLOAD MODE end ===")


# ══════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Laptop Guard Agent")
    parser.add_argument("--mode", choices=["full", "upload"], default="full",
                        help="full=capture+upload, upload=queue only")
    args = parser.parse_args()

    try:
        guard = LaptopGuard()
        if args.mode == "upload":
            guard.run_upload_only()
        else:
            guard.run_full()
    except FileNotFoundError as e:
        log.error(f"Setup needed: {e}"); sys.exit(1)
    except Exception as e:
        log.exception(f"Fatal: {e}"); sys.exit(1)

if __name__ == "__main__":
    main()