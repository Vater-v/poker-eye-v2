@echo off
ssh root@5.42.124.216 "cd /opt/pokereye && export PYTHONPATH=/opt/pokereye && python3 /opt/pokereye/vps/probe_eye_credential.py --credential-file /opt/pokereye/secrets/eye.agent --include-invalid --limit 7"
