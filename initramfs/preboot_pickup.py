"""
Laptop Guard — Pre-boot Pickup Module
=======================================
/run/lg-handoff/ থেকে initramfs-captured evidence তুলে queue-এ দেয়।

How it works:
  1. initramfs (pre-LUKS) captures image → saves to /run/lg-handoff/
  2. /run is preserved across switch_root (initramfs → Ubuntu)
  3. This module reads /run/lg-handoff/ at Ubuntu startup
  4. Adds to encrypted queue with event_type="preboot_capture"
  5. Cleans up /run/lg-handoff/ (sensitive data removed)

Called from: tracker.py → LaptopGuard.run_full() and run_upload_only()

event_type = "preboot_capture"  (distinct from "os_boot_capture")
"""

import json
import uuid
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("laptop-guard.preboot")

HANDOFF_DIR = Path("/run/lg-handoff")


class PreBootPickup:

    def __init__(self, config, queue):
        self.config = config
        self.queue  = queue

    def process(self):
        """
        Check for pre-boot handoff. If found, queue it and clean up.
        Called at every laptop-guard startup (full + upload modes).
        """
        if not HANDOFF_DIR.exists():
            log.debug("No pre-boot handoff directory")
            return

        meta_file  = HANDOFF_DIR / "meta.json"
        image_file = HANDOFF_DIR / "capture.jpg"
        log_file   = HANDOFF_DIR / "boot.log"

        if not meta_file.exists():
            log.debug("No handoff metadata found")
            self._cleanup()
            return

        # Read metadata
        try:
            meta = json.loads(meta_file.read_text())
            log.info(
                f"Pre-boot handoff found: "
                f"stage={meta.get('stage','?')} "
                f"ts={meta.get('timestamp','?')}"
            )
        except Exception as e:
            log.error(f"Handoff meta read failed: {e}")
            self._cleanup()
            return

        # Log boot.log for debugging
        if log_file.exists():
            boot_log = log_file.read_text()
            log.info(f"Pre-boot script log:\n{boot_log}")

        # Read image if available
        image_data   = None
        image_sha256 = None
        if image_file.exists():
            image_data   = image_file.read_bytes()
            image_sha256 = hashlib.sha256(image_data).hexdigest()
            log.info(
                f"Pre-boot image: {len(image_data)} bytes "
                f"sha256={image_sha256[:16]}..."
            )

        # Build event
        now = datetime.now(timezone.utc).isoformat()
        event = {
            "event_id":       str(uuid.uuid4()),
            "event_type":     "preboot_capture",  # distinct from os_boot_capture
            "stage":          meta.get("stage", "pre_luks"),
            "timestamp":      meta.get("timestamp", now),
            "device_id":      self.config.device_id,
            "hostname":       "",   # not available in initramfs
            "boot_id":        "",   # not available in initramfs
            "public_ip":      meta.get("public_ip", "unknown"),
            "location":       meta.get("location", {
                "type":       "ip_geolocation",
                "confidence": "low",
                "note":       "Captured in initramfs — network may not have been available",
            }),
            "network":        meta.get("network", {}),
            "image_captured": image_data is not None,
            "image_id":       str(uuid.uuid4()) if image_data else None,
            "image_sha256":   image_sha256,
            "status":         "pending",
            "created_at":     now,
            "uploaded_at":    None,
        }

        # Add to encrypted queue
        self.queue.enqueue(event, image_data)
        log.info(f"Pre-boot event queued: {event['event_id']}")

        # Clean up sensitive data from /run
        self._cleanup()

    def _cleanup(self):
        """Remove all files from handoff directory."""
        if not HANDOFF_DIR.exists():
            return
        for f in HANDOFF_DIR.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        try:
            HANDOFF_DIR.rmdir()
            log.debug("Pre-boot handoff directory cleaned up")
        except Exception:
            pass