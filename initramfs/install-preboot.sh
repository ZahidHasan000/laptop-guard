#!/bin/bash
# Laptop Guard — Pre-boot Capture Install
# sudo bash install-preboot.sh

set -euo pipefail
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[•]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Please run as root: sudo bash install-preboot.sh"

echo ""
echo -e "${BOLD}Laptop Guard — Pre-boot Capture Setup${NC}"
echo ""
echo "  Actions taken:"
echo "  ✅ Pre-LUKS camera capture before passphrase prompt"
echo "  ✅ Direct server upload if Ethernet is connected"
echo "  ✅ Save evidence to /run/lg-handoff/ (RAM) if offline"
echo "  ✅ Ubuntu picks up handoff on boot and enqueues it"
echo ""
echo -e "${YELLOW}  HP EliteBook 845 G10 Note:${NC}"
echo "  No built-in Ethernet — requires USB-C adapter for instant upload"
echo "  Without adapter: capture → RAM → Ubuntu boot → pickup → upload"
echo ""
read -rp "  Continue? (y/n): " C
[[ "$C" != "y" && "$C" != "Y" ]] && exit 0

# Files check
[[ ! -f "./lg-capture-hook" ]]   && err "lg-capture-hook not found"
[[ ! -f "./lg-capture-script" ]] && err "lg-capture-script not found"

PICKUP_SRC=""
if [[ -f "../agent/preboot_pickup.py" ]]; then
    PICKUP_SRC="../agent/preboot_pickup.py"
elif [[ -f "./preboot_pickup.py" ]]; then
    PICKUP_SRC="./preboot_pickup.py"
elif [[ -f "/etc/laptop-guard/preboot_pickup.py" ]]; then
    PICKUP_SRC="/etc/laptop-guard/preboot_pickup.py"
else
    err "preboot_pickup.py not found in agent/ directory"
fi

[[ ! -f "/etc/laptop-guard/config.json" ]] && err "Please run setup.sh first"

# Dependencies
command -v fswebcam &>/dev/null || apt-get install -y -qq fswebcam
ok "fswebcam: OK"

# Read API config
API_URL=$(python3 -c "import json; c=json.load(open('/etc/laptop-guard/config.json')); print(c.get('api_url',''))" 2>/dev/null || echo "")
API_TOKEN=$(python3 -c "import json; c=json.load(open('/etc/laptop-guard/config.json')); print(c.get('api_token',''))" 2>/dev/null || echo "")

# Patch API config into capture script
sed "s|__API_URL__|${API_URL:-}|g; s|__API_TOKEN__|${API_TOKEN:-}|g" \
    ./lg-capture-script > /tmp/lg-cap-patched

# Install hook
cp ./lg-capture-hook /etc/initramfs-tools/hooks/lg-capture
chmod +x /etc/initramfs-tools/hooks/lg-capture
ok "Hook: /etc/initramfs-tools/hooks/lg-capture"

# Install script
cp /tmp/lg-cap-patched /etc/initramfs-tools/scripts/local-top/lg-capture
chmod +x /etc/initramfs-tools/scripts/local-top/lg-capture
ok "Script: /etc/initramfs-tools/scripts/local-top/lg-capture"

# Install pickup module
sudo chattr -i /etc/laptop-guard/tracker.py 2>/dev/null || true
cp "$PICKUP_SRC" /etc/laptop-guard/preboot_pickup.py
chmod 700 /etc/laptop-guard/preboot_pickup.py
chown root:root /etc/laptop-guard/preboot_pickup.py
ok "Pickup: /etc/laptop-guard/preboot_pickup.py"

# Patch tracker.py if not already patched
if ! grep -q "PreBootPickup" /etc/laptop-guard/tracker.py 2>/dev/null; then
    python3 << 'PYEOF'
with open('/etc/laptop-guard/tracker.py','r') as f:
    content = f.read()
patch = '''        # Pre-boot handoff pickup
        try:
            import sys as _sys
            _sys.path.insert(0, '/etc/laptop-guard')
            from preboot_pickup import PreBootPickup
            PreBootPickup(self.cfg, self.q).process()
        except Exception as _e:
            import logging; logging.getLogger("laptop-guard").warning(f"Preboot: {_e}")

        '''
target = '        log.info(f"=== FULL MODE start'
if target in content:
    content = content.replace(target, patch + target, 1)
    with open('/etc/laptop-guard/tracker.py','w') as f:
        f.write(content)
    print("tracker.py patched")
else:
    print("Already patched or pattern not found")
PYEOF
    ok "tracker.py patched for pre-boot pickup"
else
    ok "tracker.py already has pre-boot pickup"
fi

sudo chattr +i /etc/laptop-guard/tracker.py 2>/dev/null || true

# Rebuild initramfs
info "Rebuilding initramfs (1-2 minutes)..."
update-initramfs -u -k all 2>&1 | tail -3
ok "initramfs rebuilt"

echo ""
echo -e "${GREEN}Pre-boot Setup Complete!${NC}"
echo ""
echo "  Verification Steps:"
echo "  1. sudo reboot"
echo "  2. Enter LUKS passphrase"  
echo "  3. Log in to Ubuntu"
echo "  4. sudo tail -20 /var/lib/laptop-guard/laptop-guard.log | grep -i preboot"
echo "  5. Check Gmail — 'Pre-boot' email alert should arrive"
echo ""