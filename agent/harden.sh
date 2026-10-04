#!/bin/bash
# Laptop Guard — Hardening Script (v2.1 Final)
# Security through access control, not obscurity
# sudo bash harden.sh

set -euo pipefail
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[•]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
phase()   { echo -e "\n${BOLD}${CYAN}━━━ $1 ━━━${NC}"; }

[[ $EUID -ne 0 ]] && error "sudo bash harden.sh দিয়ে চালান"

TRACKER="/etc/laptop-guard/tracker.py"
KEY_FILE="/etc/laptop-guard/guard.key"
CONFIG_FILE="/etc/laptop-guard/config.json"
NM_DISP="/etc/NetworkManager/dispatcher.d/50-laptop-guard"
BACKUP_DIR="/var/backups/laptop-guard"

[[ ! -f "$TRACKER" ]]     && error "আগে setup.sh চালান"
[[ ! -f "$CONFIG_FILE" ]] && error "config.json নেই"

echo ""
echo -e "${BOLD}${RED}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   Laptop Guard — Hardening v2.1               ║${NC}"
echo -e "${BOLD}${RED}╚═══════════════════════════════════════════════╝${NC}"

# ── Phase 1: Remove existing flags ───────────────────────────────────
phase "PHASE 1 — Remove Existing Flags"
for f in "$TRACKER" "$KEY_FILE" "$NM_DISP" \
         /etc/laptop-guard/preboot_pickup.py \
         /etc/systemd/system/laptop-guard.service \
         /etc/systemd/system/laptop-guard.timer \
         /etc/systemd/system/laptop-guard-resume.service; do
    [[ -f "$f" ]] && { chattr -i "$f" 2>/dev/null && info "Cleared: $f" || true; }
done
success "Existing immutability cleared"

# ── Phase 2: Strict Permissions ───────────────────────────────────────
phase "PHASE 2 — File Permissions"
chmod 700 /etc/laptop-guard
chmod 700 "$TRACKER"
chmod 600 "$CONFIG_FILE" "$KEY_FILE"
[[ -f "/etc/laptop-guard/preboot_pickup.py" ]] && \
    chmod 700 /etc/laptop-guard/preboot_pickup.py
[[ -f "/etc/laptop-guard/device-id" ]] && \
    chmod 600 /etc/laptop-guard/device-id
chown -R root:root /etc/laptop-guard
chmod 750 /var/lib/laptop-guard \
          /var/lib/laptop-guard/queue \
          /var/lib/laptop-guard/uploaded 2>/dev/null || true
chown -R root:root /var/lib/laptop-guard 2>/dev/null || true
success "Permissions: config_dir=700, config=600, key=600, tracker=700"

# ── Phase 3: chattr +i (tamper-resistance) ────────────────────────────
phase "PHASE 3 — File Immutability (chattr +i)"
echo ""
warn "chattr +i = tamper-resistance, NOT security boundary"
warn "Root can bypass: chattr -i file → rm/modify"
warn "Real security = UEFI + Secure Boot + DriveLock + Encryption"
echo ""

imm() {
    [[ -f "$1" ]] && {
        chattr +i "$1" 2>/dev/null && success "Immutable: $1" || warn "chattr failed: $1"
    } || warn "Not found: $1"
}

imm "$TRACKER"
imm "$KEY_FILE"
imm "$NM_DISP"
imm "/etc/systemd/system/laptop-guard.service"
imm "/etc/systemd/system/laptop-guard.timer"
imm "/etc/systemd/system/laptop-guard-resume.service"
[[ -f "/etc/laptop-guard/preboot_pickup.py" ]] && imm "/etc/laptop-guard/preboot_pickup.py"

# ── Phase 4: Backup (guard.key excluded) ─────────────────────────────
phase "PHASE 4 — Backup"
chattr -i "$BACKUP_DIR/tracker.py" 2>/dev/null || true
cp "$TRACKER" "$BACKUP_DIR/tracker.py"
chmod 700 "$BACKUP_DIR/tracker.py"
chattr +i "$BACKUP_DIR/tracker.py" 2>/dev/null || true
[[ -f "/etc/laptop-guard/preboot_pickup.py" ]] && \
    cp /etc/laptop-guard/preboot_pickup.py "$BACKUP_DIR/"
success "Backup updated (guard.key NOT included — keep external copy)"

# ── Phase 5: GRUB Password ────────────────────────────────────────────
phase "PHASE 5 — GRUB Password"
echo "  GRUB password set করলে:"
echo "  ✓ Recovery mode → password লাগবে"
echo "  ✓ Kernel params modify → blocked"
echo "  ✓ Normal boot → password ছাড়াই হবে"
echo ""
read -rp "  GRUB password set করবেন? (y/n): " G
if [[ "$G" =~ ^[Yy]$ ]]; then
    echo "  Password দিন (2 বার):"
    HASH_OUT=$(grub-mkpasswd-pbkdf2 2>&1)
    HASH=$(echo "$HASH_OUT" | grep -o 'grub\.pbkdf2\.[^ ]*' | tail -1)
    if [[ -n "$HASH" ]]; then
        [[ -f /etc/grub.d/40_custom ]] && cp /etc/grub.d/40_custom /etc/grub.d/40_custom.bak
        cat > /etc/grub.d/40_custom << GRUBEOF
#!/bin/sh
exec tail -n +3 \$0
set superusers="admin"
password_pbkdf2 admin $HASH
GRUBEOF
        LGRUB="/etc/grub.d/10_linux"
        [[ -f "$LGRUB" ]] && ! grep -q "unrestricted" "$LGRUB" && \
            sed -i 's/CLASS="--class gnu-linux/CLASS="--unrestricted --class gnu-linux/g' \
            "$LGRUB" 2>/dev/null || true
        update-grub 2>/dev/null && success "GRUB password set" || warn "update-grub error"
        warn "এই password ভুলবেন না — recovery-তে লাগবে!"
    else
        warn "Hash generate হয়নি — GRUB skip"
    fi
else
    info "GRUB password skipped"
fi

# ── Phase 6: Status Report ────────────────────────────────────────────
phase "PHASE 6 — Security Report"
echo ""
chk() { eval "$2" &>/dev/null && echo -e "  ${GREEN}✓${NC} $1" || echo -e "  ${RED}✗${NC} $1"; }

echo -e "  ${CYAN}[File Security]${NC}"
chk "tracker.py immutable"    "lsattr /etc/laptop-guard/tracker.py 2>/dev/null | grep -q 'i'"
chk "guard.key 600 root-only" "[[ \$(stat -c%a /etc/laptop-guard/guard.key 2>/dev/null)=='600' ]]"
chk "config.json 600"         "[[ \$(stat -c%a /etc/laptop-guard/config.json 2>/dev/null)=='600' ]]"

echo -e "\n  ${CYAN}[Services]${NC}"
chk "Boot timer active"       "systemctl is-active --quiet laptop-guard.timer"
chk "Resume service enabled"  "systemctl is-enabled --quiet laptop-guard-resume.service"
chk "NM dispatcher exists"    "[[ -f '$NM_DISP' ]]"

echo -e "\n  ${CYAN}[Architecture Principle Check]${NC}"
chk "NM dispatch = upload only" "grep -q 'mode upload' '$NM_DISP'"
chk "Boot service = full mode"  "grep -q 'mode full' /etc/systemd/system/laptop-guard.service"

echo -e "\n  ${YELLOW}[Manual BIOS Verification Required]${NC}"
echo "    F10 → Security → Secure Boot: ON"
echo "    F10 → Security → DriveLock: ON"
echo "    F10 → Boot → USB Boot: OFF"
echo ""
echo -e "${GREEN}  Hardening complete!${NC}"
echo "  Next (optional): sudo bash install-preboot.sh"
echo ""