#!/usr/bin/env bash
set -euo pipefail
SRC="${1:-/tmp/pokereye-clear}"
ROOT=/opt/pokereye

[[ -d "$ROOT" ]] || { echo '[ERROR] /opt/pokereye missing'; exit 2; }
[[ -f "$SRC/core/production_runtime.py" ]] || { echo '[ERROR] recovery bundle incomplete'; exit 2; }
[[ -f "$SRC/config/backend_accounts.clean.json" ]] || { echo '[ERROR] clean registry missing'; exit 2; }

# Requested clean deployment: do not create or retain PokerEye clear-deploy backups.
rm -rf /var/backups/pokereye-clear
rm -f "$ROOT/core/production_runtime.py.pre-clear.bak"
mkdir -p "$ROOT/vps"

systemctl stop pokereye || true
systemctl reset-failed pokereye || true

echo '[PokerEye] Checking grpcio...'
if ! python3 -c 'import grpc; print(grpc.__version__)' >/dev/null 2>&1; then
  echo '[PokerEye] Installing grpcio...'
  apt-get update -qq
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-grpcio; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip
    python3 -m pip install --break-system-packages grpcio
  fi
fi
python3 - <<'PY'
import grpc
print('[OK] grpcio', grpc.__version__)
PY

install -m 0644 "$SRC/core/v6router/accounts.py" "$ROOT/core/v6router/accounts.py"
install -m 0644 "$SRC/core/verified_v1/eye_direct_proxy.py" "$ROOT/core/verified_v1/eye_direct_proxy.py"
install -m 0644 "$SRC/core/production_runtime.py" "$ROOT/core/production_runtime.py"
install -m 0755 "$SRC/vps/hmn1_selftest.py" "$ROOT/vps/hmn1_selftest.py"
install -m 0755 "$SRC/vps/probe_eye_credential.py" "$ROOT/vps/probe_eye_credential.py"
install -m 0640 "$SRC/config/backend_accounts.clean.json" "$ROOT/config/backend_accounts.local.json"
chown pokereye:pokereye \
  "$ROOT/core/v6router/accounts.py" \
  "$ROOT/core/verified_v1/eye_direct_proxy.py" \
  "$ROOT/core/production_runtime.py" \
  "$ROOT/vps/hmn1_selftest.py" \
  "$ROOT/vps/probe_eye_credential.py" \
  "$ROOT/config/backend_accounts.local.json"

cd "$ROOT"
python3 -m py_compile \
  core/v6router/accounts.py \
  core/verified_v1/eye_direct_proxy.py \
  core/production_runtime.py \
  vps/hmn1_selftest.py \
  vps/probe_eye_credential.py

# Never synthesize -1..-150 until the real admin CREATE API is implemented.
mkdir -p /etc/systemd/system/pokereye.service.d
cat >/etc/systemd/system/pokereye.service.d/account-pool.conf <<'EOF'
[Service]
Environment=POKEREYE_ACCOUNT_AUTOGROW=0
Environment=POKEREYE_ACCOUNT_MAX_SUFFIX=150
EOF

systemctl daemon-reload
systemctl start pokereye

ok=0
for i in $(seq 1 15); do
  if systemctl is-active --quiet pokereye && ss -lnt | grep -q ':19037 '; then
    ok=1
    break
  fi
  sleep 1
done
if [[ "$ok" != 1 ]]; then
  echo '[ERROR] Trainer failed after clear deploy'
  systemctl --no-pager --full status pokereye || true
  journalctl -u pokereye -n 100 --no-pager || true
  exit 1
fi

python3 "$ROOT/vps/hmn1_selftest.py"

echo '=== clean registry ==='
python3 - <<'PY'
import json
p=json.load(open('/opt/pokereye/config/backend_accounts.local.json', encoding='utf-8'))
for r in p['accounts']:
    print(r['account_id'], r['state'], 'validated='+str(r['validated']))
assert len(p['accounts']) == 7, len(p['accounts'])
assert all(r['state'] == 'AVAILABLE' and r['validated'] is True for r in p['accounts'])
print('accounts=', len(p['accounts']))
PY

echo '=== listeners ==='
ss -lntp | grep -E ':(19037|19100)\b' || true
echo '=== service ==='
systemctl is-active pokereye
echo '[OK] clear deploy complete; no backups; autogrow=OFF'
