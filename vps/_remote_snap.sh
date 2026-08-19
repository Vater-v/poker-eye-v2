#!/usr/bin/env bash
set -euo pipefail
curl -fsS --max-time 4 http://127.0.0.1:19101/snapshot > /tmp/pe-snap.json
python3 - <<'PY'
import json
from pathlib import Path
x=json.loads(Path("/tmp/pe-snap.json").read_text())
print("build", x.get("build"), "patch", x.get("patch"))
print("devices", len(x.get("devices") or []))
print("accounts", len(x.get("accounts") or []))
states={}
for a in x.get("accounts") or []:
    states[a.get("state")] = states.get(a.get("state"),0)+1
print("states", states)
for a in x.get("accounts") or []:
    print("acc", a.get("account_id"), a.get("state"), a.get("owner") or "-", a.get("last_error") or "")
for d in x.get("devices") or []:
    tables=d.get("tables") or []
    print("dev", str(d.get("device_id",""))[-12:], "connected", d.get("connected"), "tables", len(tables), "hero", d.get("hero_name"))
    for t in tables:
        print("  T", t.get("table_no"), t.get("table_id"), t.get("state"), t.get("account_id"), t.get("phase"), t.get("game_type"), t.get("backend_health"), t.get("fuel_quantity"), t.get("startup_error") or "")
PY
echo "=== leftover ==="
ls -la /opt/pokereye/vps/console || true
du -sh /opt/pokereye/logs
ls /opt/pokereye/logs | wc -l
journalctl -u pokereye --no-pager -n 20
