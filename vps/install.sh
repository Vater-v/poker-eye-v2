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
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 nginx openssl redis-server >/dev/null
systemctl enable --now redis-server >/dev/null 2>&1 || true

if ! id pokereye >/dev/null 2>&1; then
  useradd --system --home "$DST" --shell /usr/sbin/nologin pokereye
fi

mkdir -p "$DST" "$DST/config" "$DST/secrets" "$DST/logs" "$DST/data" "$DST/vps"
# Atomic core swap so a sitting trainer keeps already-imported modules while
# the directory is replaced. Copying files does not hot-swap running Python.
rm -rf "$DST/core.next" "$DST/core.prev"
cp -a "$SRC/core" "$DST/core.next"
if [[ -d "$DST/core" ]]; then
  mv "$DST/core" "$DST/core.prev"
fi
mv "$DST/core.next" "$DST/core"
rm -rf "$DST/core.prev"
install -m 0644 "$SRC/main.py" "$DST/main.py"
install -m 0644 "$SRC/BUILD_ID" "$DST/BUILD_ID"
if [[ ! -s "$DST/config/backend_accounts.local.json" ]]; then
  install -m 0640 "$SRC/config/backend_accounts.local.json" "$DST/config/backend_accounts.local.json"
else
  # Keep live pool state; union blocked_accounts so 17/19 stay unleased.
  python3 - "$SRC/config/backend_accounts.local.json" "$DST/config/backend_accounts.local.json" <<'PY'
import json, sys
from pathlib import Path
src = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
dest_path = Path(sys.argv[2])
dst = json.loads(dest_path.read_text(encoding="utf-8-sig"))
blocked = sorted({
    str(item).strip()
    for item in list(dst.get("blocked_accounts") or []) + list(src.get("blocked_accounts") or [])
    if str(item).strip()
})
blockset = set(blocked)
dst["blocked_accounts"] = blocked
dst["accounts"] = [
    row for row in (dst.get("accounts") or [])
    if isinstance(row, dict) and str(row.get("account_id") or "").strip() not in blockset
]
dest_path.write_text(json.dumps(dst, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi
install -m 0640 "$SRC/secrets/trainer.secret" "$DST/secrets/trainer.secret"
install -m 0640 "$SRC/secrets/eye.agent" "$DST/secrets/eye.agent"
if [[ -s "$SRC/secrets/google-sheets.json" ]]; then
  install -m 0640 "$SRC/secrets/google-sheets.json" "$DST/secrets/google-sheets.json"
fi
python3 -m pip install -q --disable-pip-version-check cryptography >/dev/null 2>&1 || true
install -m 0755 "$SRC/vps/pokereye-web.py" "$DST/vps/pokereye-web.py"
if [[ -f "$SRC/vps/debug_dump.py" ]]; then
  install -m 0755 "$SRC/vps/debug_dump.py" "$DST/vps/debug_dump.py"
fi
if [[ -f "$SRC/vps/deferred_reload.sh" ]]; then
  install -m 0755 "$SRC/vps/deferred_reload.sh" "$DST/vps/deferred_reload.sh"
fi
rm -rf "$DST/vps/console" "$DST/vps/web-dist"
cp -a "$SRC/vps/web-dist" "$DST/vps/web-dist"
rm -f "$DST/vps/probe_coin_agent.sh" "$DST/vps/probe_eye_credential.py" \
  "$DST/vps/probe_parallel_eye.py" "$DST/vps/hmn1_selftest.py" \
  "$DST/vps/pool_status.py" "$DST/vps/install_multitable_fix.sh" \
  "$DST/vps/clean_recover.sh" "$DST/vps/_remote_diag.sh" \
  "$DST/vps/_remote_snap.sh" "$DST/vps/_remote_verify.sh"

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

# Console static files + proxy. Safe while phones sit: this is not :19037.
systemctl restart pokereye-web.service

TRAINER_ACTIVE=0
if systemctl is-active --quiet pokereye.service; then
  TRAINER_ACTIVE=1
fi
FORCE_RESTART=0
case "${POKEREYE_FORCE_RESTART:-0}" in
  1|true|TRUE|yes|YES|on|ON) FORCE_RESTART=1 ;;
esac

DECISION="$(
  POKEREYE_FORCE_RESTART="$FORCE_RESTART" POKEREYE_TRAINER_ACTIVE="$TRAINER_ACTIVE" \
    PYTHONPATH="$SRC${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m core.deploy_reload decide "$DST" 2>/dev/null || true
)"
ACTION="$(python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    print(json.loads(raw).get("action") or "")
except Exception:
    print("")
' <<<"$DECISION")"
SEATED="$(python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    d=json.loads(raw); print(d.get("seated_tables") if d.get("seated_tables") is not None else "")
except Exception:
    print("")
' <<<"$DECISION")"
LIVE_TABLES="$(python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    d=json.loads(raw); print(d.get("live_tables") if d.get("live_tables") is not None else "")
except Exception:
    print("")
' <<<"$DECISION")"
LIVE_BUILD="$(python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    print(json.loads(raw).get("live_build") or "")
except Exception:
    print("")
' <<<"$DECISION")"
STAGED_BUILD="$(cat "$DST/BUILD_ID" 2>/dev/null | tr -d '\r\n')"

if [[ -z "$ACTION" && "$TRAINER_ACTIVE" == "1" && "$FORCE_RESTART" != "1" ]]; then
  ACTION=hold
fi

systemctl stop pokereye-deferred-reload.service >/dev/null 2>&1 || true
systemctl reset-failed pokereye-deferred-reload.service >/dev/null 2>&1 || true

if [[ "$ACTION" != "hold" ]]; then
  rm -f "$DST/data/reload_requested"
  systemctl restart pokereye.service
  echo "[OK] Trainer restarted (seated=${SEATED:-0} live=${LIVE_TABLES:-0})"
else
  # Sitting/live tables keep the old process and :19037. New code is on disk
  # only until the fleet is empty — not an in-process hot-swap.
  printf '%s\n' "$STAGED_BUILD" > "$DST/data/reload_requested"
  chown pokereye:pokereye "$DST/data/reload_requested" >/dev/null 2>&1 || true
  if [[ -x "$DST/vps/deferred_reload.sh" ]]; then
    systemd-run --unit=pokereye-deferred-reload \
      --description="PokerEye deferred trainer reload" \
      --working-directory="$DST" \
      --property=Environment=POKEREYE_ROOT="$DST" \
      "$DST/vps/deferred_reload.sh" >/dev/null \
      || nohup "$DST/vps/deferred_reload.sh" >>"$DST/logs/deferred_reload.log" 2>&1 &
  fi
  pid="$(systemctl show -p MainPID --value pokereye.service 2>/dev/null || true)"
  if [[ -n "${pid:-}" && "$pid" != "0" ]]; then
    kill -USR1 "$pid" >/dev/null 2>&1 || true
  fi
  echo "[HOLD] Trainer not restarted (seated=${SEATED:-?} live=${LIVE_TABLES:-?} live_build=${LIVE_BUILD:-unknown}). Staged ${STAGED_BUILD}. Applies when last table stands up. Force: POKEREYE_FORCE_RESTART=1"
fi
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
if data.get("reload_pending"):
    print("[HOLD] trainer live %s staged %s" % (data.get("build") or "", data.get("staged_build") or ""))
PY

echo
echo "[OK] Trainer: ${VPS_IP}:19037"
echo "[OK] Console: https://admin.aktan.pro/pokereye/?token=${TOKEN}"
echo "[OK] Build: $(cat "$DST/BUILD_ID")"
