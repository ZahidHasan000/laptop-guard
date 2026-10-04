#!/bin/bash
# Remote Server Deploy Script
# VPS-এ run করুন: bash deploy-server.sh

set -euo pipefail
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[•]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }

APP_DIR="/opt/laptop-guard-server"
DATA_DIR="/var/lib/laptop-guard-server"

echo -e "${BOLD}Laptop Guard — Server Deploy${NC}"

# ── System packages ───────────────────────────────────────────────────
info "Packages install করছি..."
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv nginx certbot python3-certbot-nginx

# ── App directory ─────────────────────────────────────────────────────
info "App directory তৈরি করছি..."
mkdir -p "$APP_DIR" "$DATA_DIR/images"
chown -R www-data:www-data "$DATA_DIR"

# ── Python venv ───────────────────────────────────────────────────────
info "Python venv তৈরি করছি..."
python3 -m venv "$APP_DIR/venv"
cp api_server.py "$APP_DIR/"
cp requirements.txt "$APP_DIR/"
"$APP_DIR/venv/bin/pip" install -q -r requirements.txt
success "Dependencies installed"

# ── Environment file ──────────────────────────────────────────────────
info "Environment setup করছি..."
echo ""
read -rp "  API Token (long random string): " TOKEN
read -rp "  Gmail address: " GMAIL
read -rsp "  Gmail App Password: " GPASS; echo ""
read -rp "  Alert To email: " GTO
read -rp "  Your domain (e.g. guard.example.com): " DOMAIN

cat > "$APP_DIR/.env" << ENVEOF
LAPTOP_GUARD_TOKEN=$TOKEN
LAPTOP_GUARD_DATA=$DATA_DIR
NOTIFY_GMAIL=$GMAIL
NOTIFY_APP_PASS=$GPASS
NOTIFY_TO=$GTO
ENVEOF
chmod 600 "$APP_DIR/.env"
success "Environment: $APP_DIR/.env"

# ── Systemd service ───────────────────────────────────────────────────
cp laptop-guard-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet laptop-guard-server
systemctl start laptop-guard-server
success "Server service started"

# ── Nginx ─────────────────────────────────────────────────────────────
sed "s/your-domain.com/$DOMAIN/g" nginx-laptop-guard.conf \
    > /etc/nginx/sites-available/laptop-guard
ln -sf /etc/nginx/sites-available/laptop-guard /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
success "Nginx configured"

# ── HTTPS ─────────────────────────────────────────────────────────────
info "HTTPS certificate setup করছি..."
read -rp "  Email for cert: " CERT_EMAIL
certbot --nginx -d "$DOMAIN" --non-interactive \
    --agree-tos -m "$CERT_EMAIL" && success "HTTPS ready!" || \
    warn "Certbot failed — domain DNS check করুন"

echo ""
success "Server deploy complete!"
echo ""
echo "  Test: curl https://$DOMAIN/health"
echo "  Log:  journalctl -u laptop-guard-server -f"
echo ""
echo "  setup.sh-এ এই values দিন:"
echo "  API URL:   https://$DOMAIN"
echo "  API Token: $TOKEN"
echo ""