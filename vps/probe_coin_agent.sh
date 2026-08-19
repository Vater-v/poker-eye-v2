#!/usr/bin/env bash
set -euo pipefail
cd /opt/pokereye
exec python3 /opt/pokereye/vps/probe_eye_credential.py --stop-on-pass "$@"
