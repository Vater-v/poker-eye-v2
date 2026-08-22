#!/usr/bin/env bash
# Apply a staged trainer tree after the last sitting/live table is gone.
# Safe to run while an older trainer (without in-process exec) is still up.
set -u
DST="${POKEREYE_ROOT:-/opt/pokereye}"
cd "$DST" || exit 1
export POKEREYE_ROOT="$DST"
export PYTHONPATH="$DST${PYTHONPATH:+:$PYTHONPATH}"
REQUEST="$DST/data/reload_requested"
LOG="$DST/logs/deferred_reload.log"
mkdir -p "$DST/logs" "$DST/data"

log() {
  echo "$(date -Is) $*" >> "$LOG" 2>/dev/null || true
  echo "[deferred-reload] $*"
}

if [[ ! -f "$REQUEST" ]]; then
  log "no reload request; exit"
  exit 0
fi

log "watching for empty fleet; staged=$(tr -d '\n' < "$DST/BUILD_ID" 2>/dev/null || true)"
restarts=0
while [[ -f "$REQUEST" ]]; do
  action="$(python3 -m core.deploy_reload watch "$DST" 2>/dev/null || true)"
  action="${action//$'\r'/}"
  action="${action##*$'\n'}"
  [[ -n "$action" ]] || action=wait
  case "$action" in
    stop)
      log "live build matches disk or request cleared"
      rm -f "$REQUEST"
      exit 0
      ;;
    restart)
      restarts=$((restarts + 1))
      if [[ "$restarts" -gt 8 ]]; then
        log "too many restarts; giving up"
        exit 1
      fi
      log "fleet empty; systemctl restart pokereye.service ($restarts)"
      systemctl restart pokereye.service
      sleep 8
      ;;
    *)
      sleep 5
      ;;
  esac
done
log "request gone; exit"
exit 0
