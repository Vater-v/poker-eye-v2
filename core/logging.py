"""Small append-only session logger: compact operator view plus JSONL evidence."""
from __future__ import annotations
import json, os, re, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
def safe_component(value: str, fallback: str = "unknown") -> str:
    text = _SAFE.sub("_", str(value)).strip("._")
    return text[:96] or fallback
class SessionLogger:
    def __init__(self, root="logs", *, run_id=None, emulator_name=None, hero_ref=None):
        self.run_id=run_id or uuid.uuid4().hex[:12]; self.directory=Path(root)/f"run_{safe_component(self.run_id)}"; self.directory.mkdir(parents=True,exist_ok=True)
        self.events_path=self.directory/"events.jsonl"; self.operator_path=self.directory/"operator.txt"; self.manifest_path=self.directory/"manifest.json"; self._lock=threading.Lock(); self._closed=False
        self._write_manifest(emulator_name,hero_ref)
    def _write_manifest(self, emulator_name, hero_ref):
        self.manifest_path.write_text(json.dumps({"schema_version":1,"run_id":self.run_id,"started_at":datetime.now(timezone.utc).isoformat(),"emulator_name":emulator_name,"hero_ref":hero_ref},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    def emit(self,event,*,severity="INFO",message=None,flush=False,**fields):
        record={"schema_version":1,"ts":datetime.now(timezone.utc).isoformat(),"event":event,"severity":severity,"run_id":self.run_id,**{k:v for k,v in fields.items() if v is not None}}
        with self._lock:
            if self._closed: raise RuntimeError("session logger is closed")
            with self.events_path.open("a",encoding="utf-8",newline="\n") as f: f.write(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n"); f.flush() if flush or severity in {"WARN","ERROR"} else None
            with self.operator_path.open("a",encoding="utf-8",newline="\n") as f: f.write(f"{record['ts']} [{severity}] {message or event}\n")
        return record
    def close(self,reason="shutdown"):
        if not self._closed: self.emit("trainer.stopped",message=f"Trainer остановлен: {reason}",flush=True,reason=reason); self._closed=True
