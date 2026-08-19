#!/usr/bin/env bash
set -euo pipefail

SRC="${1:-/tmp/pokereye-deploy}"
DST=/opt/pokereye
VPS_IP=5.42.124.216
NGINX_SITE=/etc/nginx/sites-enabled/admin.aktan.pro.conf
NGINX_BACKUP_DIR=/var/backups/pokereye-nginx

if [[ ! -f "$SRC/main.py" || ! -d "$SRC/core" ]]; then
  echo "[ERROR] bad deploy source: $SRC" >&2
  exit 2
fi
[[ -f "$SRC/vps/pokereye-web.py" && -f "$SRC/vps/web-dist/index.html" ]] || { echo "[ERROR] missing Nuxt console build" >&2; exit 2; }

for f in "$SRC/config/backend_accounts.local.json" "$SRC/secrets/trainer.secret" "$SRC/secrets/eye.agent"; do
  [[ -s "$f" ]] || { echo "[ERROR] missing/empty $f" >&2; exit 2; }
done

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 nginx openssl >/dev/null

if ! id pokereye >/dev/null 2>&1; then
  useradd --system --home "$DST" --shell /usr/sbin/nologin pokereye
fi

mkdir -p "$DST" "$DST/config" "$DST/secrets" "$DST/logs" "$DST/vps"
rm -rf "$DST/core"
cp -a "$SRC/core" "$DST/core"
install -m 0644 "$SRC/main.py" "$DST/main.py"
install -m 0644 "$SRC/BUILD_ID" "$DST/BUILD_ID"
install -m 0640 "$SRC/config/backend_accounts.local.json" "$DST/config/backend_accounts.local.json"
install -m 0640 "$SRC/secrets/trainer.secret" "$DST/secrets/trainer.secret"
install -m 0640 "$SRC/secrets/eye.agent" "$DST/secrets/eye.agent"
install -m 0755 "$SRC/vps/pokereye-web.py" "$DST/vps/pokereye-web.py"
rm -rf "$DST/vps/console" "$DST/vps/web-dist"
cp -a "$SRC/vps/web-dist" "$DST/vps/web-dist"

if [[ ! -s "$DST/secrets/web.token" ]]; then
  umask 077
  openssl rand -hex 24 > "$DST/secrets/web.token"
fi
chown -R pokereye:pokereye "$DST"
chmod 0750 "$DST" "$DST/secrets"
chmod 0640 "$DST/secrets/"* "$DST/config/backend_accounts.local.json"
find "$DST/vps/web-dist" -type d -exec chmod 0755 {} +
find "$DST/vps/web-dist" -type f -exec chmod 0644 {} +

install -m 0644 "$SRC/vps/pokereye.service" /etc/systemd/system/pokereye.service
install -m 0644 "$SRC/vps/pokereye-web.service" /etc/systemd/system/pokereye-web.service

# Never leave backups inside sites-enabled: nginx.conf includes every file there.
# Older PokerEye deploys did that and created a duplicate admin.aktan.pro vhost.
mkdir -p "$NGINX_BACKUP_DIR"
shopt -s nullglob
for old_backup in "${NGINX_SITE}.bak."*; do
  mv "$old_backup" "$NGINX_BACKUP_DIR/$(basename "$old_backup")"
done
shopt -u nullglob

# Add one path to the already-certificated admin.aktan.pro vhost. Existing
# /api/ and / routes are left untouched. The Python service enforces its token.
if [[ -f "$NGINX_SITE" ]] && ! grep -q 'POKEREYE_LOCATION_BEGIN' "$NGINX_SITE"; then
  cp -a "$NGINX_SITE" "$NGINX_BACKUP_DIR/admin.aktan.pro.conf.$(date +%Y%m%d-%H%M%S)"
  python3 - "$NGINX_SITE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()
needle='    client_max_body_size 100M;\n'
block='''    # POKEREYE_LOCATION_BEGIN\n    location ^~ /pokereye/ {\n        proxy_pass http://127.0.0.1:19100/;\n        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;\n        proxy_buffering off;\n        access_log off;\n        add_header Cache-Control "no-store" always;\n    }\n    # POKEREYE_LOCATION_END\n\n'''
if needle not in s:
    raise SystemExit('cannot find insertion point in admin.aktan.pro.conf')
s=s.replace(needle, needle+'\n'+block, 1)
p.write_text(s)
PY
fi

nginx -t
systemctl reload nginx
systemctl daemon-reload
systemctl enable pokereye.service pokereye-web.service >/dev/null

# enable --now does NOT reload an already-running Python process.  Always
# restart after replacing runtime files so the browser cannot keep seeing v1.
systemctl restart pokereye.service
systemctl restart pokereye-web.service
sleep 1

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 19037/tcp >/dev/null
fi

systemctl --no-pager --full status pokereye.service | sed -n '1,12p' || true
systemctl --no-pager --full status pokereye-web.service | sed -n '1,10p' || true
ss -lntp | grep -E ':(19037|19100)\b' || true
TOKEN="$(cat "$DST/secrets/web.token")"

python3 - "$TOKEN" <<'PY'
import json, sys, urllib.request
token=sys.argv[1]
url="http://127.0.0.1:19100/health?token=" + token
with urllib.request.urlopen(url, timeout=4) as r:
    data=json.load(r)
if data.get("web") != "console-nuxt-v6":
    raise SystemExit(f"[ERROR] wrong web runtime after restart: {data!r}")
print("[OK] Web runtime: console-nuxt-v6")
PY

echo
echo "[OK] Trainer: ${VPS_IP}:19037"
echo "[OK] Console: https://admin.aktan.pro/pokereye/?token=${TOKEN}"
echo "[OK] Build: $(cat "$DST/BUILD_ID")"
