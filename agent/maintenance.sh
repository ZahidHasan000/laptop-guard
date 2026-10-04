#!/bin/bash
# Laptop Guard — Maintenance Script (v2.1)
# sudo bash maintenance.sh [command]

set -euo pipefail
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${BLUE}[•]${NC} $1"; }

[[ $EUID -ne 0 ]] && { echo "sudo bash maintenance.sh [command]"; exit 1; }

CMD="${1:-help}"

case "$CMD" in

status)
    echo -e "\n${BOLD}Laptop Guard v2.1 — Status${NC}\n"
    chk() { eval "$2" &>/dev/null && ok "$1" || err "$1"; }

    echo -e "${BLUE}[Services]${NC}"
    chk "Boot timer"         "systemctl is-active --quiet laptop-guard.timer"
    chk "Resume service"     "systemctl is-enabled --quiet laptop-guard-resume.service"
    chk "NM dispatcher"      "[[ -f '/etc/NetworkManager/dispatcher.d/50-laptop-guard' ]]"

    echo -e "\n${BLUE}[Architecture Check]${NC}"
    chk "NM = upload only"   "grep -q 'mode upload' /etc/NetworkManager/dispatcher.d/50-laptop-guard"
    chk "Boot = full mode"   "grep -q 'mode full' /etc/systemd/system/laptop-guard.service"
    chk "No Persistent=true" "! grep -q 'Persistent' /etc/systemd/system/laptop-guard.timer"

    echo -e "\n${BLUE}[Security]${NC}"
    chk "tracker.py immutable" "lsattr /etc/laptop-guard/tracker.py 2>/dev/null | grep -q 'i'"
    chk "guard.key 600"        "[[ \$(stat -c%a /etc/laptop-guard/guard.key 2>/dev/null)=='600' ]]"
    ARMED=$( [[ -f /etc/laptop-guard/.disarmed ]] && echo "DISARMED" || echo "ARMED" )
    echo "  System status:    $ARMED"

    echo -e "\n${BLUE}[Queue]${NC}"
    PENDING=$(ls /var/lib/laptop-guard/queue/*.lgq 2>/dev/null | wc -l || echo 0)
    UPLOADED=$(ls /var/lib/laptop-guard/uploaded/*.lgq 2>/dev/null | wc -l || echo 0)
    QSIZE=$(du -sh /var/lib/laptop-guard/queue/ 2>/dev/null | cut -f1 || echo "?")
    echo "  Pending:   $PENDING events ($QSIZE)"
    echo "  Uploaded:  $UPLOADED events"

    echo -e "\n${BLUE}[Disk]${NC}"
    du -sh /var/lib/laptop-guard/ 2>/dev/null | awk '{print "  Data dir:  "$1}'
    du -sh /var/lib/laptop-guard/laptop-guard.log 2>/dev/null | awk '{print "  Log:       "$1}'

    echo -e "\n${BLUE}[Last 5 Log Lines]${NC}"
    tail -5 /var/lib/laptop-guard/laptop-guard.log 2>/dev/null | sed 's/^/  /'
    echo ""
    ;;

test)
    info "Rate limit bypass করছি..."
    rm -f /var/lib/laptop-guard/.last_run
    info "Full capture test চালাচ্ছি..."
    python3 /etc/laptop-guard/tracker.py --mode full
    ok "Test done"
    tail -10 /var/lib/laptop-guard/laptop-guard.log | sed 's/^/  /'
    ;;

test-upload)
    info "Upload-only test (no camera)..."
    python3 /etc/laptop-guard/tracker.py --mode upload
    ok "Upload test done"
    ;;

arm)
    rm -f /etc/laptop-guard/.disarmed
    ok "System ARMED — পরবর্তী boot-এ capture চলবে"
    ;;

disarm)
    touch /etc/laptop-guard/.disarmed
    ok "System DISARMED — camera capture off (queue upload চলবে)"
    echo "  Re-arm করতে: sudo bash maintenance.sh arm"
    ;;

show-key)
    echo -e "${YELLOW}Encryption Key (backup করুন!):${NC}"
    cat /etc/laptop-guard/guard.key
    echo ""
    warn "এই key laptop-এর বাইরে password manager-এ রাখুন"
    ;;

pre-upgrade)
    info "apt upgrade-এর আগে immutability সরাচ্ছি..."
    for f in /etc/laptop-guard/tracker.py /etc/laptop-guard/guard.key \
              /etc/systemd/system/laptop-guard.service \
              /etc/systemd/system/laptop-guard.timer \
              /etc/systemd/system/laptop-guard-resume.service \
              /etc/NetworkManager/dispatcher.d/50-laptop-guard; do
        [[ -f "$f" ]] && { chattr -i "$f" 2>/dev/null && ok "Cleared: $f" || true; }
    done
    warn "Upgrade শেষে: sudo bash maintenance.sh post-upgrade"
    ;;

post-upgrade)
    info "Post-upgrade hardening..."
    systemctl daemon-reload
    systemctl enable --quiet laptop-guard.timer laptop-guard-resume.service
    systemctl start laptop-guard.timer
    for f in /etc/laptop-guard/tracker.py /etc/laptop-guard/guard.key \
              /etc/systemd/system/laptop-guard.service \
              /etc/systemd/system/laptop-guard.timer \
              /etc/systemd/system/laptop-guard-resume.service \
              /etc/NetworkManager/dispatcher.d/50-laptop-guard; do
        [[ -f "$f" ]] && { chattr +i "$f" 2>/dev/null && ok "Immutable: $f" || true; }
    done
    ok "Post-upgrade done"
    ;;

clean)
    info "30 দিনের পুরনো uploaded events মুছছি..."
    COUNT=$(find /var/lib/laptop-guard/uploaded/ -name "*.lgq" -mtime +30 2>/dev/null | wc -l)
    find /var/lib/laptop-guard/uploaded/ -name "*.lgq" -mtime +30 -delete 2>/dev/null || true
    ok "Removed $COUNT old event(s)"
    ;;

log)
    tail -50 /var/lib/laptop-guard/laptop-guard.log
    ;;

help|*)
    echo ""
    echo -e "${BOLD}Laptop Guard Maintenance Commands:${NC}"
    echo ""
    echo "  status       → সব কিছু check করুন"
    echo "  test         → manual full capture test"
    echo "  test-upload  → upload-only test (no camera)"
    echo "  arm          → capture চালু করুন"
    echo "  disarm       → capture বন্ধ করুন (queue upload চলবে)"
    echo "  show-key     → encryption key দেখুন (backup করুন)"
    echo "  pre-upgrade  → apt upgrade-এর আগে run করুন"
    echo "  post-upgrade → apt upgrade-এর পরে run করুন"
    echo "  clean        → পুরনো events মুছুন"
    echo "  log          → শেষ 50 log line দেখুন"
    echo ""
    ;;
esac