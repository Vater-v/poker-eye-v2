#!/usr/bin/env bash
set -euo pipefail
echo "=== units ==="
systemctl is-active pokereye pokereye-web nginx || true
echo "=== build ==="
cat /opt/pokereye/BUILD_ID
echo "=== web id ==="
python3 - <<'PY'
from pathlib import Path
t = Path("/opt/pokereye/vps/pokereye-web.py").read_text(encoding="utf-8", errors="replace")
for line in t.splitlines():
    if "WEB_ID" in line:
        print(line.strip())
        break
print("web_bytes", len(t))
PY
echo "=== health ==="
TOKEN="$(cat /opt/pokereye/secrets/web.token)"
echo "token_len ${#TOKEN}"
curl -fsS --max-time 4 "http://127.0.0.1:19100/health?token=${TOKEN}"; echo
curl -fsS --max-time 4 http://127.0.0.1:19101/health; echo
echo "=== snapshot ==="
curl -fsS --max-time 4 http://127.0.0.1:19101/snapshot | python3 - <<'PY'
import json,sys
x=json.load(sys.stdin)
print("build", x.get("build"), "patch", x.get("patch"))
print("devices", len(x.get("devices") or []))
print("accounts", len(x.get("accounts") or []))
states={}
for a in x.get("accounts") or []:
    states[a.get("state")] = states.get(a.get("state"),0)+1
print("states", states)
for d in x.get("devices") or []:
    tables=d.get("tables") or []
    fuels=[t.get("fuel_quantity") for t in tables]
    print("dev", str(d.get("device_id",""))[-12:], "connected", d.get("connected"), "tables", len(tables), "hero", d.get("hero_name"), "fuel", fuels)
    for t in tables:
        print("  table", t.get("table_no"), t.get("table_id"), t.get("state"), t.get("account_id"), t.get("phase"), t.get("game_type"), t.get("backend_health"), t.get("startup_error"))
PY
echo "=== leftover dirs ==="
ls -la /opt/pokereye/vps/console || true
find /opt/pokereye -maxdepth 3 -name '*bak*' -o -name '*.pyc' -o -name '__pycache__' | head
echo "=== journal trainer ==="
journalctl -u pokereye --no-pager -n 25
echo "=== disk logs ==="
du -sh /opt/pokereye/logs || true
ls /opt/pokereye/logs | wc -l
