#!/bin/bash
# ════════════════════════════════════════════════════════════════════
# Laptop Guard — Complete Setup Script (v2.1 Final)
# HP EliteBook 845 G10 / Ubuntu 24.04
#
# Installs:
#   ✅ laptop-guard agent (tracker.py)
#   ✅ preboot_pickup.py
#   ✅ systemd service + timer (boot trigger)
#   ✅ NM dispatcher (upload-only — NOT camera trigger)
#   ✅ laptop-guard-resume.service (upload on resume)
#   ✅ Logrotate config
#   ✅ Fernet encryption key
#   ✅ Device UUID
#
# sudo bash setup.sh
# ════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[•]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
phase()   { echo -e "\n${BOLD}${CYAN}━━━ $1 ━━━${NC}"; }

[[ $EUID -ne 0 ]] && error "sudo bash setup.sh দিয়ে চালান"

# ─── Paths ─────────────────────────────────────────────────────────
CONFIG_DIR="/etc/laptop-guard"
DATA_DIR="/var/lib/laptop-guard"
QUEUE_DIR="$DATA_DIR/queue"
UPLOAD_DIR="$DATA_DIR/uploaded"
BACKUP_DIR="/var/backups/laptop-guard"
CONFIG_FILE="$CONFIG_DIR/config.json"
KEY_FILE="$CONFIG_DIR/guard.key"
DEV_FILE="$CONFIG_DIR/device-id"
TRACKER="$CONFIG_DIR/tracker.py"
PICKUP="$CONFIG_DIR/preboot_pickup.py"
DISARM_EXAMPLE="$CONFIG_DIR/.disarmed.example"

SVC="laptop-guard.service"
TIMER="laptop-guard.timer"
RESUME_SVC="laptop-guard-resume.service"
NM_DISP="/etc/NetworkManager/dispatcher.d/50-laptop-guard"
LOGROTATE="/etc/logrotate.d/laptop-guard"

echo ""
echo -e "${BOLD}${RED}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   Laptop Guard — Setup v2.1 Final             ║${NC}"
echo -e "${BOLD}${RED}║   HP EliteBook 845 G10 / Ubuntu 24.04         ║${NC}"
echo -e "${BOLD}${RED}╚═══════════════════════════════════════════════╝${NC}"

# ════════════════════════════════════════════════════════════════════
# PHASE 1 — BIOS Checklist
# ════════════════════════════════════════════════════════════════════
phase "PHASE 1 — BIOS Checklist (HP F10)"
echo ""
echo "  HP BIOS-এ এগুলো confirm করুন:"
echo "  [ ] UEFI Admin Password   → SET"
echo "  [ ] Secure Boot           → ENABLED"
echo "  [ ] DriveLock             → ENABLED (recommended)"
echo "  [ ] USB Boot              → DISABLED"
echo "  [ ] Boot Order            → Internal SSD first"
echo ""
echo -e "  ${YELLOW}Architecture Principle:${NC}"
echo "  UEFI + Secure Boot + DriveLock + Encryption = Primary security"
echo "  laptop-guard = Evidence collection layer"
echo "  These are complementary, not alternatives."
echo ""
read -rp "  BIOS check করেছেন? (y/n): " C
[[ "$C" != "y" && "$C" != "Y" ]] && {
    warn "setup চালু রাখছি — কিন্তু BIOS আগে set করুন"
}

# ════════════════════════════════════════════════════════════════════
# PHASE 2 — Dependencies
# ════════════════════════════════════════════════════════════════════
phase "PHASE 2 — Dependencies"
apt-get update -qq
command -v fswebcam &>/dev/null || apt-get install -y -qq fswebcam
python3 -c "from cryptography.fernet import Fernet" 2>/dev/null || \
    pip3 install cryptography --break-system-packages -q
success "fswebcam: $(fswebcam --version 2>&1 | head -1)"
success "python3: $(python3 --version)"
success "cryptography: OK"
command -v v4l2-ctl &>/dev/null || {
    apt-get install -y -qq v4l-utils 2>/dev/null && success "v4l-utils: installed" || \
    warn "v4l-utils not available — camera auto-detect will use existence check"
}
command -v nmcli &>/dev/null && success "nmcli: OK" || warn "nmcli missing — WiFi BSSID unavailable"

# ════════════════════════════════════════════════════════════════════
# PHASE 3 — Directory Structure
# ════════════════════════════════════════════════════════════════════
phase "PHASE 3 — Directories"
mkdir -p "$CONFIG_DIR" "$QUEUE_DIR" "$UPLOAD_DIR" "$BACKUP_DIR"
chmod 700 "$CONFIG_DIR" "$BACKUP_DIR"
chmod 750 "$DATA_DIR" "$QUEUE_DIR" "$UPLOAD_DIR"
chown -R root:root "$CONFIG_DIR" "$DATA_DIR" "$BACKUP_DIR"
success "Config:  $CONFIG_DIR (700, root)"
success "Data:    $DATA_DIR"
success "Queue:   $QUEUE_DIR"
success "Backup:  $BACKUP_DIR"

# ════════════════════════════════════════════════════════════════════
# PHASE 4 — Encryption Key
# ════════════════════════════════════════════════════════════════════
phase "PHASE 4 — Encryption Key (Fernet)"
if [[ -f "$KEY_FILE" ]]; then
    warn "Key already exists — skipping"
else
    python3 -c "
from cryptography.fernet import Fernet
key = Fernet.generate_key()
open('$KEY_FILE','wb').write(key)
print('Key generated')
"
    chmod 600 "$KEY_FILE"; chown root:root "$KEY_FILE"
    success "Key: $KEY_FILE (600, root)"
fi

echo ""
echo -e "  ${YELLOW}⚠ CRITICAL: Backup your key NOW — outside the laptop!${NC}"
echo "  sudo cat $KEY_FILE"
echo "  → Password manager বা printed copy রাখুন"
echo "  → Key হারালে queue decrypt করা যাবে না"
echo ""
read -rp "  Key backup করেছেন? (y/n): " KB
[[ "$KB" != "y" && "$KB" != "Y" ]] && warn "Key backup করুন এখনই — setup শেষ হলেও"

# ════════════════════════════════════════════════════════════════════
# PHASE 5 — Device ID
# ════════════════════════════════════════════════════════════════════
phase "PHASE 5 — Device UUID"
if [[ ! -f "$DEV_FILE" ]]; then
    python3 -c "import uuid; open('$DEV_FILE','w').write(str(uuid.uuid4()))"
    chmod 600 "$DEV_FILE"; chown root:root "$DEV_FILE"
    success "Device ID generated: $(cat $DEV_FILE)"
else
    success "Device ID exists: $(cat $DEV_FILE)"
fi

# ════════════════════════════════════════════════════════════════════
# PHASE 6 — Configuration
# ════════════════════════════════════════════════════════════════════
phase "PHASE 6 — Configuration"
echo ""
echo "  Gmail App Password পেতে:"
echo "  myaccount.google.com → Security → 2-Step Verification"
echo "  → App Passwords → Mail + Linux → Generate"
echo ""
read -rp "  Gmail (sender): " GMAIL
[[ "$GMAIL" != *@gmail.com ]] && error "Gmail address দিন (@gmail.com)"
read -rsp "  Gmail App Password: " APPPASS; echo ""
[[ ${#APPPASS} -lt 8 ]] && error "App Password ঠিকমতো দিন"
read -rp "  Alert পাঠাবেন কোথায়? (Enter = same): " TO
[[ -z "$TO" ]] && TO="$GMAIL"
echo ""
read -rp "  Remote API URL (Enter = skip, Gmail-only): " APIURL
read -rp "  API Token (Enter = skip): " APITOKEN

DEVID=$(cat "$DEV_FILE")
cat > "$CONFIG_FILE" << CFEOF
{
    "gmail":        "$GMAIL",
    "app_password": "$APPPASS",
    "to_email":     "$TO",
    "api_url":      "${APIURL:-}",
    "api_token":    "${APITOKEN:-}",
    "device_id":    "$DEVID"
}
CFEOF
chmod 600 "$CONFIG_FILE"; chown root:root "$CONFIG_FILE"
success "Config: $CONFIG_FILE (600, root)"

# ════════════════════════════════════════════════════════════════════
# PHASE 7 — Install Agent Files
# ════════════════════════════════════════════════════════════════════
phase "PHASE 7 — Install Agent"

# tracker.py
[[ -f "tracker.py" ]] || error "tracker.py এখানে নেই — সব file একসাথে রাখুন"
cp tracker.py "$TRACKER"
chmod 700 "$TRACKER"; chown root:root "$TRACKER"
success "tracker.py → $TRACKER"

# preboot_pickup.py (optional but recommended)
if [[ -f "preboot_pickup.py" ]]; then
    cp preboot_pickup.py "$PICKUP"
    chmod 700 "$PICKUP"; chown root:root "$PICKUP"
    success "preboot_pickup.py → $PICKUP"
else
    warn "preboot_pickup.py নেই — pre-boot pickup disabled"
fi

# ════════════════════════════════════════════════════════════════════
# PHASE 8 — Systemd: Boot Service + Timer
# ════════════════════════════════════════════════════════════════════
phase "PHASE 8 — Systemd Boot Service"

# laptop-guard.service
cat > "/etc/systemd/system/$SVC" << SVCEOF
[Unit]
Description=Laptop Guard Security Agent
Documentation=
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $TRACKER --mode full
User=root
PrivateTmp=yes
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
SVCEOF

# laptop-guard.timer
# NOTE: Persistent=true removed (only useful for OnCalendar= timers)
cat > "/etc/systemd/system/$TIMER" << TIMEOF
[Unit]
Description=Laptop Guard Security Agent — Boot + 4h Timer

[Timer]
OnBootSec=90s
OnUnitActiveSec=4h
RandomizedDelaySec=60s

[Install]
WantedBy=timers.target
TIMEOF

success "Service: $SVC | Timer: $TIMER"

# ════════════════════════════════════════════════════════════════════
# PHASE 9 — Systemd: Resume Service
# Upload-only on resume (no camera — prevents spam on lid open)
# ════════════════════════════════════════════════════════════════════
phase "PHASE 9 — Resume Upload Service"

cat > "/etc/systemd/system/$RESUME_SVC" << RESEOF
[Unit]
Description=Laptop Guard — Resume Queue Upload
Documentation=
After=suspend.target hibernate.target hybrid-sleep.target sleep.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $TRACKER --mode upload
User=root
StandardOutput=null
StandardError=null

[Install]
WantedBy=suspend.target hibernate.target
RESEOF

success "Resume service: $RESUME_SVC (upload-only, no camera on lid open)"

# ════════════════════════════════════════════════════════════════════
# PHASE 10 — NM Dispatcher (UPLOAD ONLY — not capture trigger)
# ════════════════════════════════════════════════════════════════════
phase "PHASE 10 — NetworkManager Dispatcher"

echo ""
echo -e "  ${CYAN}Architecture principle:${NC}"
echo "  Network = Evidence DELIVERY trigger (not generation trigger)"
echo "  Camera captures happen at BOOT only"
echo "  Network reconnect = queue upload only"
echo ""

cat > "$NM_DISP" << 'NMEOF'
#!/bin/bash
# Laptop Guard — NetworkManager Dispatcher
# v2.1: UPLOAD ONLY — no camera on network events
#
# Architectural rule:
#   Network available = trigger queue upload
#   Network available ≠ trigger new camera capture
#
# Camera captures: boot only (laptop-guard.service)
# Queue delivery:  network available (this dispatcher)

INTERFACE="$1"
EVENT="$2"

# Only act on network becoming available
[ "$EVENT" = "up" ] || [ "$EVENT" = "connectivity-change" ] || exit 0

# Wait for network to stabilize
sleep 8

# Upload pending queue — no camera, no new evidence generation
/usr/bin/python3 /etc/laptop-guard/tracker.py --mode upload &
NMEOF

chmod 755 "$NM_DISP"; chown root:root "$NM_DISP"
success "NM Dispatcher: $NM_DISP (upload-only)"

# ════════════════════════════════════════════════════════════════════
# PHASE 11 — Logrotate
# ════════════════════════════════════════════════════════════════════
phase "PHASE 11 — Log Rotation"
cat > "$LOGROTATE" << LREOF
/var/lib/laptop-guard/laptop-guard.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    size 20M
    create 640 root root
}
LREOF
success "Logrotate: $LOGROTATE"

# ════════════════════════════════════════════════════════════════════
# PHASE 12 — Armed/Disarmed Documentation
# ════════════════════════════════════════════════════════════════════
phase "PHASE 12 — Armed/Disarmed Setup"
cat > "$DISARM_EXAMPLE" << ARMEOF
# Laptop Guard — Armed/Disarmed Control
#
# DISARM (আপনি নিজে use করলে captures বন্ধ করুন):
#   sudo touch /etc/laptop-guard/.disarmed
#   → Queue এখনো upload হবে, camera capture হবে না
#
# ARM (চুরির আশঙ্কা থাকলে):
#   sudo rm -f /etc/laptop-guard/.disarmed
#   → পরবর্তী boot-এ capture শুরু হবে
#
# Status check:
#   [ -f /etc/laptop-guard/.disarmed ] && echo DISARMED || echo ARMED
#
# NOTE: .disarmed file মুছে দিলে system আবার armed হয়
# NOTE: Thief .disarmed file create করবে না (জানে না)
# NOTE: এটা security boundary নয় — usability control
ARMEOF
chmod 644 "$DISARM_EXAMPLE"
success "Armed/Disarmed docs: $DISARM_EXAMPLE"

# ════════════════════════════════════════════════════════════════════
# PHASE 13 — Backup (guard.key excluded)
# ════════════════════════════════════════════════════════════════════
phase "PHASE 13 — Backup"
cp "$TRACKER" "$BACKUP_DIR/"
[[ -f "$PICKUP" ]] && cp "$PICKUP" "$BACKUP_DIR/"
cp "/etc/systemd/system/$SVC"        "$BACKUP_DIR/"
cp "/etc/systemd/system/$TIMER"      "$BACKUP_DIR/"
cp "/etc/systemd/system/$RESUME_SVC" "$BACKUP_DIR/"
cp "$NM_DISP" "$BACKUP_DIR/50-laptop-guard"
chmod 700 "$BACKUP_DIR/tracker.py"
success "Backup: $BACKUP_DIR (guard.key NOT here — keep external copy)"

# ════════════════════════════════════════════════════════════════════
# PHASE 14 — Enable All Services
# ════════════════════════════════════════════════════════════════════
phase "PHASE 14 — Enable Services"
systemctl daemon-reload
systemctl enable --quiet "$TIMER"
systemctl start "$TIMER"
systemctl enable --quiet "$RESUME_SVC"
success "Timer enabled: $TIMER"
success "Resume service enabled: $RESUME_SVC"

# ════════════════════════════════════════════════════════════════════
# PHASE 15 — Test
# ════════════════════════════════════════════════════════════════════
phase "PHASE 15 — Test Run"
info "Manual test চালাচ্ছি..."
python3 "$TRACKER" --mode full && \
    success "Test OK! Gmail check করুন" || \
    warn "Test failed — log দেখুন: tail -20 $DATA_DIR/laptop-guard.log"

# ════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Laptop Guard v2.1 — Setup Complete!             ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Architecture Summary:${NC}"
echo "  BOOT    → laptop-guard.service --mode full"
echo "            (capture + queue + upload)"
echo "  NETWORK → NM dispatcher --mode upload"
echo "            (queue upload only — no camera)"
echo "  RESUME  → laptop-guard-resume.service --mode upload"
echo "            (queue upload only — no camera)"
echo ""
echo -e "  ${CYAN}Useful Commands:${NC}"
echo "  Status:   sudo bash maintenance.sh status"
echo "  Disarm:   sudo touch $CONFIG_DIR/.disarmed"
echo "  Arm:      sudo rm -f $CONFIG_DIR/.disarmed"
echo "  Test:     sudo python3 $TRACKER --mode full"
echo "  Log:      sudo tail -f $DATA_DIR/laptop-guard.log"
echo "  Queue:    ls $QUEUE_DIR/"
echo ""
echo -e "  ${YELLOW}Next Steps:${NC}"
echo "  1. sudo bash harden.sh          ← security hardening"
echo "  2. sudo bash install-preboot.sh ← pre-boot capture (optional)"
echo ""
echo -e "  ${YELLOW}Guard Key Backup (CRITICAL):${NC}"
echo "  sudo cat $KEY_FILE"
echo "  → Save this outside the laptop!"
echo ""