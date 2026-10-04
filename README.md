# Laptop Guard 🛡️

**DIY Anti-Theft Security System for Linux Laptops**

A defense-in-depth security agent that captures evidence (photo + location) when your laptop is powered on — even before the LUKS encryption passphrase is entered.

Built for HP EliteBook 845 G10 / Ubuntu 24.04. Adaptable to any Linux laptop.

---

## What It Does

```
Laptop stolen → Thief powers it on
                      ↓
             Pre-boot capture (before LUKS)
                      ↓
             LUKS passphrase prompt
                      ↓
        If Ubuntu boots → full evidence:
          • Webcam photo
          • Public IP + geolocation
          • WiFi SSID + BSSID
          • BeaconDB WiFi location
          • Encrypted queue → Gmail alert
```

---

## Architecture

```
LAYER 1: UEFI/BIOS  → Admin password, Secure Boot, DriveLock
LAYER 2: initramfs  → Pre-LUKS capture (before passphrase)
LAYER 3: LUKS       → Full disk encryption
LAYER 4: Ubuntu OS  → laptop-guard.service (main agent)
LAYER 5: Remote API → Optional evidence server
```

---

## Features

- Pre-boot capture (before LUKS passphrase)
- Boot-time webcam photo + WiFi BSSID + IP geolocation
- BeaconDB WiFi-based location (more accurate than IP)
- Fernet authenticated encryption for local queue
- Atomic queue writes (no corruption)
- Armed/Disarmed mode for owner
- 4-hour rate limiting
- Upload-only network trigger (no camera spam)
- Gmail alert with photo + map links
- Optional FastAPI remote server

---

## Quick Start

```bash
git clone https://github.com/ZahidHasan000/laptop-guard
cd laptop-guard/agent
sudo bash setup.sh    # Install + configure
sudo bash harden.sh   # Security hardening
```

**Pre-boot capture (optional):**
```bash
cd laptop-guard/initramfs
sudo bash install-preboot.sh
sudo reboot
```

---

## Configuration

```json
{
    "gmail":        "your@gmail.com",
    "app_password": "gmail-app-password",
    "to_email":     "alert@gmail.com",
    "api_url":      "",
    "api_token":    ""
}
```

Stored at `/etc/laptop-guard/config.json` (root-only, 600). Never commit this file.

---

## Daily Commands

```bash
sudo bash maintenance.sh status    # Check everything
sudo bash maintenance.sh arm       # Enable captures
sudo bash maintenance.sh disarm    # Disable captures (when you use it)
sudo bash maintenance.sh test      # Manual test
sudo bash maintenance.sh pre-upgrade   # Before apt upgrade
sudo bash maintenance.sh post-upgrade  # After apt upgrade
```

---

## Honest Limitations

| Scenario | Result |
|---|---|
| Thief powers on laptop | Pre-boot alert sent |
| Thief boots Ubuntu | Full evidence captured |
| OS reinstall | Agent removed (not survivable) |
| SSD replacement | Agent unavailable |
| Sophisticated physical attack | Out of scope |

---

## Architecture Principles

- Security through access control, not obscurity
- Network = evidence delivery trigger only
- Camera fires at boot only, not on WiFi reconnect
- chattr +i is tamper-resistance, not a security boundary
- Real security = UEFI + Secure Boot + DriveLock + Encryption

---

## File Structure

```
laptop-guard/
├── agent/
│   ├── tracker.py
│   ├── preboot_pickup.py
│   ├── setup.sh
│   ├── harden.sh
│   └── maintenance.sh
├── initramfs/
│   ├── lg-capture-hook
│   ├── lg-capture-script
│   └── install-preboot.sh
├── server/
│   ├── api_server.py
│   └── requirements.txt
└── docs/
    └── ARCHITECTURE.md
```

---

## License

MIT — For protecting your own device only.