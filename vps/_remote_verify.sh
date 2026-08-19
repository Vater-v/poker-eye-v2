#!/usr/bin/env bash
set -euo pipefail
systemctl show pokereye -p Environment --no-pager | tr ' ' '\n' | grep POKEREYE || true
python3 - <<'PY'
import json
from pathlib import Path
x=json.loads(Path("/opt/pokereye/config/backend_accounts.local.json").read_text())
print("pool", [a.get("account_id") for a in x.get("accounts") or []])
import sys
sys.path.insert(0, "/opt/pokereye")
from core.verified_v1.eye_panel_admin import EyePanelClient, client_from_env
print("panel_module_ok")
PY
curl -fsS http://127.0.0.1:19101/snapshot | python3 -c 'import json,sys; x=json.load(sys.stdin); print([a["account_id"]+"="+a["state"] for a in x.get("accounts") or []])'
