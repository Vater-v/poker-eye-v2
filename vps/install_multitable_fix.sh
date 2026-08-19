#!/usr/bin/env bash
set -euo pipefail

SRC="${1:-/tmp/pokereye-mtfix}"
DST=/opt/pokereye

echo "[PokerEye] multitable runtime patch MTABLE-20260818-A"

for f in \
  "$SRC/core/production_runtime.py" \
  "$SRC/core/v6router/router.py" \
  "$SRC/core/v6router/accounts.py" \
  "$SRC/core/verified_v1/eye_direct_proxy.py" \
  "$SRC/vps/pokereye-web.py" \
  "$SRC/vps/hmn1_selftest.py" \
  "$SRC/vps/probe_parallel_eye.py"; do
  [[ -s "$f" ]] || { echo "[ERROR] missing $f" >&2; exit 2; }
done

# Compile the upload before touching the live runtime. No backup/rollback files.
python3 -m py_compile \
  "$SRC/core/production_runtime.py" \
  "$SRC/core/v6router/router.py" \
  "$SRC/core/v6router/accounts.py" \
  "$SRC/core/verified_v1/eye_direct_proxy.py" \
  "$SRC/vps/pokereye-web.py" \
  "$SRC/vps/hmn1_selftest.py" \
  "$SRC/vps/probe_parallel_eye.py"

echo "[PokerEye] stopping Trainer..."
systemctl stop pokereye.service

install -m 0644 "$SRC/core/production_runtime.py" "$DST/core/production_runtime.py"
install -m 0644 "$SRC/core/v6router/router.py" "$DST/core/v6router/router.py"
install -m 0644 "$SRC/core/v6router/accounts.py" "$DST/core/v6router/accounts.py"
install -m 0644 "$SRC/core/verified_v1/eye_direct_proxy.py" "$DST/core/verified_v1/eye_direct_proxy.py"
install -m 0755 "$SRC/vps/pokereye-web.py" "$DST/vps/pokereye-web.py"
install -m 0755 "$SRC/vps/hmn1_selftest.py" "$DST/vps/hmn1_selftest.py"
install -m 0755 "$SRC/vps/probe_parallel_eye.py" "$DST/vps/probe_parallel_eye.py"
chown pokereye:pokereye \
  "$DST/core/production_runtime.py" \
  "$DST/core/v6router/router.py" \
  "$DST/core/v6router/accounts.py" \
  "$DST/core/verified_v1/eye_direct_proxy.py" \
  "$DST/vps/pokereye-web.py" \
  "$DST/vps/hmn1_selftest.py" \
  "$DST/vps/probe_parallel_eye.py"

# Compile in the real tree so import/path errors fail before we claim success.
cd "$DST"
python3 -m py_compile \
  core/production_runtime.py \
  core/v6router/router.py \
  core/v6router/accounts.py \
  core/verified_v1/eye_direct_proxy.py \
  vps/pokereye-web.py \
  vps/hmn1_selftest.py \
  vps/probe_parallel_eye.py

echo "[PokerEye] starting Trainer..."
systemctl start pokereye.service
for _ in $(seq 1 30); do
  if systemctl is-active --quiet pokereye.service && ss -lnt | grep -q ':19037 '; then
    break
  fi
  sleep .25
done

if ! systemctl is-active --quiet pokereye.service; then
  systemctl --no-pager --full status pokereye.service || true
  journalctl -u pokereye.service -n 80 --no-pager || true
  exit 3
fi
if ! ss -lnt | grep -q ':19037 '; then
  echo "[ERROR] Trainer active but :19037 is not listening" >&2
  journalctl -u pokereye.service -n 80 --no-pager || true
  exit 4
fi

python3 "$DST/vps/hmn1_selftest.py"

CONTROL="$(curl -fsS --max-time 3 http://127.0.0.1:19101/health)"
echo "[OK] control: $CONTROL"
echo "$CONTROL" | grep -q 'MTABLE-20260818-A' || {
  echo "[ERROR] trainer control plane is not the multitable patch" >&2
  exit 5
}

systemctl restart pokereye-web.service
for _ in $(seq 1 20); do
  systemctl is-active --quiet pokereye-web.service && break
  sleep .25
done
systemctl is-active --quiet pokereye-web.service || {
  systemctl --no-pager --full status pokereye-web.service || true
  exit 6
}

TOKEN="$(cat "$DST/secrets/web.token")"
WEB="$(curl -fsS --max-time 3 "http://127.0.0.1:19100/health?token=$TOKEN")"
echo "[OK] web: $WEB"
echo "$WEB" | grep -q 'console-v3-multitable' || {
  echo "[ERROR] web console v3 did not start" >&2
  exit 7
}

echo
echo "=== LIVE ACCOUNT POOL ==="
curl -fsS http://127.0.0.1:19101/snapshot | python3 -c 'import json,sys; x=json.load(sys.stdin); print("free=%d leased=%d probing=%d quarantined=%d invalid=%d" % tuple(sum(1 for a in x.get("accounts",[]) if a.get("state")==s) for s in ("AVAILABLE","LEASED","PROBING","QUARANTINED","INVALID")))'
echo
echo "[OK] MTABLE-20260818-A installed"
echo "[OK] APK rebuild: NOT REQUIRED"
echo "[OK] Console: https://admin.aktan.pro/pokereye/"
echo "[OK] Manual table controls: enabled"
