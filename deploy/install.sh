#!/usr/bin/env bash
# Install or update mock-tdx on an Ubuntu host. Idempotent. Run as root (sudo) from a checkout.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"; APP=/opt/mock-tdx; STATE=/var/lib/mock-tdx; CONF=/etc/mock-tdx/env
id -u mocktdx >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin mocktdx
apt-get install -y -qq python3-venv rsync >/dev/null
mkdir -p "$APP" "$STATE" /etc/mock-tdx
rsync -a --delete --exclude .venv --exclude .git --exclude data --exclude .env "$SRC/" "$APP/"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet "$APP"
if [ ! -f "$CONF" ]; then
  KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))'); PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))'); SEC=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  cat > "$CONF" <<ENV
TENANT_NAME="Harbor & Main Bank"
TICKET_APP_ID=431
TIMEZONE=America/Los_Angeles
HOST=0.0.0.0
PORT=8090
DB_PATH=$STATE/mocktdx.db
TDX_BEID=7f2c1a44-6c3e-4b7b-9a1e-2d5f8c0b1e21
TDX_WEB_SERVICES_KEY=$KEY
TDX_TECH_PASSWORD=$PW
TDX_TOKEN_SECRET=$SEC
TOKEN_TTL_HOURS=24
WEBHOOK_URL=
ENV
  echo "wrote $CONF with generated secrets. Put TDX_WEB_SERVICES_KEY and TDX_TECH_PASSWORD in Bitwarden now:"; grep -E "KEY|PASSWORD" "$CONF"
fi
chmod 0600 "$CONF"; chown -R mocktdx:mocktdx "$STATE"; chown root:root "$CONF"
install -m 0644 "$SRC/deploy/mock-tdx.service" /etc/systemd/system/mock-tdx.service
systemctl daemon-reload; systemctl enable --now mock-tdx; systemctl restart mock-tdx; sleep 2
systemctl --no-pager --lines=3 status mock-tdx || true
echo "health: $(curl -s http://127.0.0.1:8090/healthz || echo unreachable)"
