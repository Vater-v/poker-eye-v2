# Legacy Bridge → v2 Migration Map

**Generated:** 2026-08-17  
**Source:** `C:\projects\pokereye\ready_v6\` (2154 lines of `coin_bridge_live.py`, ~3500 lines total across 7 core .py files)  
**Target:** `C:\projects\pokereye\poker-eye-v2\core\` (existing: actions, discovery, logging, protocol, sessions, transport)  
**Status:** READ-ONLY analysis — no files modified.

---

## 1. Port As-Is (Verified, stdlib-Portable)

These are pure Python 3.10+ standard-library components with no external dependencies beyond `struct`, `json`, `zlib`, and `hashlib`. All are proven by the `selftest.py` regression suite (170/170 PASS).

### 1.1 SFS2X Packet Wire Encoding/Decoding

**Source:** `coin_action_wire.py`

| Item | Lines | Description |
|------|-------|-------------|
| `SFS_BYTE=2; SFS_SHORT=3; SFS_INT=4; SFS_UTF=8; SFS_OBJECT=18` | 7 | SmartFox 2X type tag constants used by CoinPoker's ExtensionRequest |
| `_Byte`, `_Short`, `_Int`, `_Str`, `_Obj` | 9–18 | Typed value wrappers — pure `@dataclass(frozen=True)` |
| `_enc_value(v) → bytes` | 24–35 | Recursive SFS2X type encoder; used by `encode_packet` |
| `encode_packet(value: dict) → bytes` | 37–41 | 0x80 header + 2-byte big-endian length + SFS-encoded payload |
| `decode_packet(raw: bytes) → dict` | 62–89 | 0x80/0x88 header parse, optional zlib decompress, SFS object decode |
| `_u16`, `_i16`, `_i32` helpers | 20–22 | Fixed-width big-endian integer serialization |

**Verdict:** Zero external dependencies. Verified by `selftest.py` line 5 (`build_game_user_action_packet(49453,5,.15)` matches a captured wire fixture byte-for-byte). Port as one module (`core/sfs_wire.py`).

### 1.2 Coin user_action Packet Builder

**Source:** `coin_action_wire.py:43–57`

```python
def build_game_user_action_packet(room_id: int, user_action: int, bet_amount: float = 0.0) -> bytes:
```

Encodes `{"userAction": <code>, "betAmount": <amount>}` as JSON inside the SFS2X ExtensionRequest envelope `{c:1, a:13, p:{c:"game.user_action", r:<room>, p:{data:...}}}`.

**Critical detail:** When `bet_amount == 0.0`, the json.dumps output replaces `0.0` with `0` (line 47: `.replace('0.0}', '0}')`) — this matches the captured Coin wire exactly.

### 1.3 CC Action Codes

**Source:** `coin_action_wire.py:7` (implied), `coin_bridge_live.py:10`

| Action | Coin `userAction` code | `ResolvedAction.coin_code` | `build_game_user_action_packet` uses |
|--------|----------------------|---------------------------|--------------------------------------|
| FOLD | 1 (in `ACTION` dict) | 7 | `resolve_eye_cc_action` returns code 7 |
| CHECK | 2 | 3 | `resolve_eye_cc_action` returns code 3 |
| CALL | 3 | 4 | code 4, `betAmount=0` |
| RAISE | 4 | 5 | code 5, `betAmount=<target>` |
| BET | 7 | 5 (mapped to RAISE) | same as RAISE |
| ALLIN | — | 5 or 4 (distance-projected) | same logic |

**Observed fact:** The `ACTION` dictionary at `coin_bridge_live.py:10` maps PPP action names to Coin wire codes. The CC action codes used in `build_game_user_action_packet` come from `resolve_eye_cc_action` which returns different numeric codes (3=CHECK, 4=CALL, 5=RAISE, 7=FOLD).

**Important:** The numeric values in `resolve_eye_cc_action.coin_code` are the ones that go on the actual wire. The `ACTION` dict at `coin_bridge_live.py:10` is for PPP-synthesis bookkeeping, not for the coin wire.

### 1.4 EYE→Coin CC Action Resolution

**Source:** `coin_action_wire.py:97–140`

```python
def resolve_eye_cc_action(cc: Dict[str, Any], *, user_turn_options=None,
                          current_street_bet=0.0, chip_scale=100) -> ResolvedAction:
```

**Algorithm (from code, lines 98–140):**

1. Extract `type`/`action`/`message` from the CC dict, normalize to uppercase (line 99).
2. If `FOLD`/`FAST_FOLD` → return `ResolvedAction('FOLD', 7, 0.0)` (line 101).
3. If `CHECK` → return `ResolvedAction('CHECK', 3, 0.0)` (line 102).
4. If `CALL` → return `ResolvedAction('CALL', 4, 0.0)` (line 103).
5. For `RAISE`/`BET`/`ALLIN`:
   - Parse `subtype` (integer, 0 or 1; `subtype=1` means all-in).
   - Parse `message` (normalized by stripping `-`, `_`, space). `message=="ALLIN"` also implies all-in.
   - Parse `amount` (the EYE "additional chip-in" amount).
   - Compute `desired = current_street_bet + amount/chip_scale`.
   - Evaluate distance to Coin's option 4 (CALL target) and option 5 (RAISE range `[lo, hi]`).
   - Clamp to `[lo, hi]` for raises, prefer closest monetary distance.
   - Prefer the aggressive action (RAISE) only on exact distance tie (`distance, 0` sorts before `distance, 1`).
   - If no paid action exists in Coin options → `ValueError`.

**Verified edge cases (from `selftest.py:1–44`):**
- PLO6 cap raise clamped to Coin max (`bet_amount=0.09` from range `[0.04, 0.09]`).
- All-in CALL when Coin only offers CALL/FOLD: never fabricate option 5.
- All-in RAISE when both CALL and RAISE available: use closest monetary target in raise range.
- Nominal RAISE closer to CALL than min-raise: emit CALL (never cancel).
- Fold/Check are intentionally excluded from the paid-action distance projection.

**Verdict:** Pure Python, fully deterministic, zero side effects. Port as-is.

---

## 2. Port With Adaptation

These components are verified correct in the legacy bridge but must be adapted for the v2 threading model (threading.Lock vs asyncio.Lock), protocol choices, and smaller scope.

### 2.1 EYE Hint/CC Message Frame Parsing

**Source:** `coin_bridge_live.py:198–221` (`_eye_reader`) and `eye_direct_proxy.py:870–880`

The EYE backend (or EYE app proxy) sends length-prefixed JSON frames over TCP:

```python
# lp_pack / lp_read (identical in coin_bridge_live.py:29-40 and eye_direct_proxy.py)
def lp_pack(obj: dict) -> bytes:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw

async def lp_read(reader: asyncio.StreamReader) -> Optional[bytes]:
    header = await reader.readexactly(4)  # 4-byte big-endian length
    size = struct.unpack(">I", header)[0]
    if not 0 < size <= 20_000_000: raise ValueError(f"bad frame {size}")
    return await reader.readexactly(size)
```

**CC frame shape** (observed in `_eye_reader` lines 210–213):

```json
{"tag": "cc", "msg": "", "data": "<SCAction JSON string>", "packageName": "com.lein.pppoker.android"}
```

The `data` field contains the serialized SCAction dict with these exact fields:

| EYE SCAction Field | Type | Role | Maps to |
|-------------------|------|------|---------|
| `type` | string | Aggressive action kind: `"FOLD"`, `"CHECK"`, `"CALL"`, `"RAISE"`, `"BET"` | `resolve_eye_cc_action` → Coin code |
| `action` | string | Alternate key for `type` (fallback in `resolve_eye_cc_action:99`) | Same |
| `message` | string | "all-in" marker or display text | `subtype=1` equivalent if `message=="ALLIN"` |
| `subtype` | int | 0=normal raise, 1=all-in raise/CALL | All-in classification in `resolve_eye_cc_action:105–108` |
| `amount` | float | **Additional chips** to put in (PPP ActionBRC.Chips semantics) | Converted to absolute Coin street total via `_quantize_pending_cc_amount` |
| `delay` | int (ms) | Requested human-think delay (0–15000 ms) | Capped by Coin turn deadline margin (`schedule_cc:141–148`) |
| `lifetime` | int (ms) | EYE UI message lifetime (default 4000) | **NOT** the action validity window; stock EYE captures execute actions whose delay > lifetime (`selftest.py:457–465` proves this) |

**Inference:** The `lifetime` field is a UI-message duration, not a poker-action deadline. The only safety boundary is the Coin turn timer minus `turn_deadline_margin_ms`.

**v2 adaptation:** Port `lp_pack`/`lp_read` to the v2 protocol module (already has `frame()`/`recv_frame()`); adapt the CC frame parsing to extract the SCAction dict and feed it to `resolve_eye_cc_action`.

### 2.2 Turn Identity and Deduplication

**Source:** `coin_autoplay.py:52–64` (`_turn_id`)

```python
@staticmethod
def _turn_id(data: Dict[str, Any]) -> str:
    """Stable identity for one Coin turn, including replay/reconnect copies."""
    for key in ('initTimeStamp', 'turnId', 'turnID', 'actionId'):
        value = data.get(key)
        if value not in (None, ''):
            return f'{key}:{value}'
    stable = {
        'whoseTurn': data.get('whoseTurn') or data.get('userName'),
        'turnTime': data.get('turnTime'),
        'callAmount': data.get('callAmount'),
        'userTurnOptions': data.get('userTurnOptions') or {},
    }
    return 'shape:' + hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(',', ':'), default=str).encode()
    ).hexdigest()
```

**Logic:** Prefers an explicit timestamp/ID from Coin (initTimeStamp, turnId, turnID, actionId). Falls back to a content hash of the turn's shape (whoseTurn, turnTime, callAmount, userTurnOptions). This ensures reconnects/replays that re-emit the same `game.user_turn` do not spawn duplicate hints.

**v2 adaptation:** Use `_turn_id` as-is. The v2 logging module should record the `turn_id` with every `hint.requested` event.

### 2.3 Three-Attempt Action Semantics

**Source (legacy):** `coin_bridge_live.py:1142–1154` (retry), `coin_bridge_live.py:2047–2052` (pending_action_ack)

**Source (v2 target):** `core/actions.py:35` — `ActionScheduler.MAX_ATTEMPTS = 3`

**Legacy behavior:**
1. **Attempt 1:** Inject via `lobby.dummy` replacement (`maybe_inject` → `HookResult(inject_raw=...)`). Hook v3 schedules the send at `delay_ms` in the app process.
2. **Attempt 2 (retry):** If no Coin server ACK arrives within `action_retry_delay` (default 2.5s), retry once on the same WebSocket and hand (`_maybe_inject_async:1142–1154`). Marked `retries=1`.
3. **After retry exhaustion:** If still no ACK, the action enters `NEEDS_OPERATOR` or is timed out.

**v2 CONCEPT.md confirms:** "make exactly three explicit attempts: attempt 1 after the calculated delay, attempt 2 after 1 second, attempt 3 after another 1 second."

**v2 adaptation:** The legacy bridge effectively has **2 sends** (initial + 1 retry) with the third "attempt" being the ACK verification. The v2 `ActionScheduler.next_attempt()` already implements 3 attempts with 1-second retry gaps. The existing `core/actions.py` is the correct v2 target; adapt the legacy's WebSocket-specific retry to the generic trainer device channel.

### 2.4 Human-Like Delay and Turn-Deadline-Margin Logic

**Source:** `coin_autoplay.py:123–163` (`schedule_cc`)

```python
requested_delay_ms = max(0, min(15000, int(cc.get('delay') or 0)))  # line 141
turn_deadline_at = observed + turn_seconds                            # line 143
remaining_ms = max(0, int(round((observed + turn_seconds - now) * 1000.0))
                   - self.turn_deadline_margin_ms)                     # line 144
delay_ms = min(requested_delay_ms, remaining_ms)                       # line 145
```

**Key invariant:** `turn_deadline_margin_ms` defaults to 750ms (env `POKER_TURN_DEADLINE_MARGIN_MS`). The actual delay is `min(requested_delay_ms, remaining_ms_in_turn - margin)`. When `turn_seconds > 0` but the remaining time is exhausted, `delay_ms` can be 0 (immediate send).

**Tested:** `selftest.py:457–465` — A `FOLD` with `delay=5545, lifetime=4000` is NOT capped by lifetime; it is capped only by Coin turn time. When the turn has 0.5s remaining, `delay_ms` becomes 0.

**v2 adaptation:** Port the deadline math. The v2 `ActionScheduler.next_attempt()` returns `(action, delay)` where delay is 0.0 for the first attempt; the caller should apply the computed human delay before the first send.

### 2.5 `call_need==0 && CHECK legal` State-Gap Guard

**Source:** `poker-eye-v2/docs/LOGGING_AND_CASES.md:3–15`, `poker-eye-v2/docs/RETURN_PLAN.md:5,45–49`

**Observed incident:** When Coin reports `callAmount=0` and `userTurnOptions={'3': None}` (CHECK is the only legal option), the bridge path `call=0 → backend CHECK amount=0 → Coin CALL betAmount=0` repeats. This is a policy/ordering inconsistency.

**The guard (v2 requirement):** If `call_need == 0` while CHECK is in `userTurnOptions`, the runtime must emit a `state.gap` event rather than silently normalizing the selected action to `CALL amount=0`. The resolution is to prefer CHECK or require explicit reconciliation according to action policy.

**Where the gap manifests in legacy code:**
- `coin_ppp_bridge.py:1290`: `call_need = max(0, current_max - street_contributed[h.ppp_hero_seat])` — derived from internal state.
- `coin_ppp_bridge.py:1294`: Validated against `h.turn.get("callAmount")` — but only in cold replay; no live guard when state and Coin diverge.
- `coin_autoplay.py:123–163`: `schedule_cc` calls `resolve_eye_cc_action` which does check `typ == 'CALL'` and returns code 4 regardless of `user_turn_options`.

**v2 adaptation:** Before calling `resolve_eye_cc_action`, check whether `call_need == 0` and `CHECK` (option 3) is in `userTurnOptions`. If so, emit `state.gap` and prefer CHECK. This guard must go in the hint→action translation layer that calls `resolve_eye_cc_action`.

---

## 3. Do NOT Port (Out of Scope for v2)

These components belong to the legacy orchestrator/supervisor layer and are explicitly excluded from the minimal v2 core.

| Component | Source File(s) | Reason |
|-----------|---------------|--------|
| **ADB reverse ingress** | `supervisor/runtime/adb.py`, `supervisor/automation/navigation_control.py`, `supervisor/automation/prefold.py` | v2 uses operator-managed emulator connectivity; no ADB automation |
| **PyQt6 GUI** | `supervisor/gui.py` | CONCEPT.md: "No GUI, HTTP server, browser admin..." |
| **Web admin (HTTP)** | `supervisor/web_admin.py` | Out of scope; only console + JSONL logging |
| **Google Sheets ledger** | `supervisor/automation/sheets_ledger.py` | Out of scope; replaced by `logging.py` JSONL ledger |
| **Telegram notifications** | `supervisor/automation/telegram_outbox.py` | Out of scope |
| **gRPC backend connection** | `eye_backend_probe.py`, `eye_direct_proxy.py` | v2 receives CC frames via authenticated TCP from the EYE app proxy; no direct gRPC |
| **Backend-account probing** | `eye_backend_probe.py:_connect()`, `config.txt` AccountProbe* keys | Legacy pre-flights SCLogin for accounts 9-19; irrelevant for single-table trainer |
| **Supervisor orchestration** | `supervisor/__main__.py`, `supervisor/controller_pump.py`, `supervisor/event_stream.py`, `supervisor/fuel.py`, `supervisor/model.py` | Replaced by `main.py` + `transport.py` |
| **Navigation/canary** | `config.txt` Navigation* keys | Legacy dry-run Coin navigation; not needed |
| **Bombpot tracker** | `bombpot_support.py`, `coin_bridge_live.py:692` | Bombpot support requires separate capture verification; defer |
| **PPPBuilder synthesis** | `coin_ppp_bridge.py:1250–1560` (hint building), `coin_ppp_bridge.py:160–1200` (frame synthesis) | v2 is a trainer endpoint, not a PPP bridge; the EYE backend builds the hint |
| **Config file** | `config.txt` | All runtime configuration moves to v2 `config.toml` or environment variables |

### Config Keys Classification

From `config.txt` (97 lines, all key=value):

| Category | Keys | v2 Fate |
|----------|------|---------|
| **Wire/protocol-relevant** | `AccountBase`, `KnownAccountSuffixes`, `CredentialFile` | Migrate (eye account identity) |
| **Runtime/infra** | `RuntimeStateDirectory`, `LogDirectory`, `TelemetryDirectory`, `KeepCaptures` | Migrate (paths) |
| **Probe (skip)** | `ProbeAccountSuffixes`, `AccountProbeConcurrency`, `AccountProbeAttempts`, `AccountProbeBackoff`, `AccountInvalidRetry`, `AccountRegistry` | Drop |
| **Coin navigation (skip)** | `NavigationMode`, `NavigationLiveCanaryEnabled`, `NavigationCanaryConfigId`, `NavigationCanaryBigBlind`, `NavigationCanaryBuyin`, `NavigationCanaryMiniGameType`, `NavigationCanaryCoinType` | Drop |
| **Sheets (skip)** | `GoogleSheets*` (8 keys) | Drop |
| **Telegram (skip)** | `TelegramSecret` | Drop |
| **ADB (skip)** | `AdbPath`, `AdbHost`, `AdbPort` | Drop |
| **Socks/Coin (skip)** | `SocksDroidPackage`, `SocksDroidActivity`, `CoinPackage`, `CoinActivity`, `ProxyReadyTimeout`, `ProxyPollInterval` | Drop |
| **Web admin (skip)** | `WebAdminHost`, `WebAdminPort` | Drop |
| **Fuel (skip)** | `FuelLowThreshold` | Drop |
| **Shadow (skip)** | `AutomationShadow`, `AutomationDatabase`, `MaxTablesPerPlayer` | Drop |

---

## 4. Critical Invariants That Must Survive Migration

### I1: One in-flight CC per device
**Source:** `core/actions.py:21` (`ActionScheduler._active: dict[str, Action]`)  
The v2 `ActionScheduler.create()` already enforces this: `if action.device_id in self._active: return False`. The legacy equivalent is `CoinAutoplayCoordinator.pending` (one `Optional[dict]` at `coin_autoplay.py:21`). Must be preserved.

### I2: Exactly 3 attempts, no unbounded loop
**Source:** `core/actions.py:26` (`MAX_ATTEMPTS = 3`)  
The legacy bridge has exactly 2 wire sends (initial + 1 retry at `coin_bridge_live.py:1142–1154`) with the third "attempt" being the ACK wait. v2 explicitly codifies this as 3 attempts with 1-second gaps.

### I3: Never silently normalize `call=0 → CALL` when CHECK is legal
**Source:** `poker-eye-v2/docs/LOGGING_AND_CASES.md:13`, `poker-eye-v2/docs/RETURN_PLAN.md:49`  
If `call_need == 0` and `userTurnOptions` contains option 3, the runtime must emit `state.gap` rather than sending `CALL amount=0`. The `resolve_eye_cc_action` function does NOT perform this check itself — it trusts its caller to validate the semantic match between the hint's implied call amount and the CC decision. This guard must live in the calling layer.

### I4: Stale-ACK protection
**Source:** `coin_bridge_live.py:2001–2026` and `core/actions.py:55–61`  
The legacy bridge: a pending ACK is cancelled when a manual outbound action, turn change, hand reset, or table leave occurs before the action's due time. The v2 `ActionScheduler.acknowledge()` validates `correlation_id` and `generation` — a stale ACK from a previous generation or duplicate send is rejected (returns `False`).

### I5: Turn-change cancellation
**Source:** `coin_autoplay.py:79–97` (`observe` → `TURN_CHANGED`)  
When `game.user_turn` arrives for a non-hero actor while `pending` exists, the pending action is cancelled with reason `TURN_CHANGED`. The v2 equivalent must cancel any scheduled send whose target turn no longer matches the current `_turn_id`.

### I6: Injected-echo fingerprint suppression
**Source:** `coin_autoplay.py:33–37,108–114` (`_fingerprint`, `is_recent_injected_echo`)  
When the bridge injects a `game.user_action` via `lobby.dummy` replacement, that outgoing packet re-enters the hook. The fingerprint cache (`recent_injected: Dict[str, float]`, SHA-256 of raw bytes, 8-second TTL) prevents the injected packet from being treated as a manual cancellation. The v2 device channel must provide equivalent echo suppression (the `correlation_id` in `Action` already serves this purpose).

### I7: `build_game_user_action_packet` exact `betAmount=0` serialization
**Source:** `coin_action_wire.py:46–47`  
When `bet_amount == 0.0`, the JSON must contain `"betAmount":0` (integer), not `"betAmount":0.0`. This is achieved by string replacement after `json.dumps`. The v2 port must preserve this.

### I8: `resolve_eye_cc_action` Fold/Check exclusion from monetary projection
**Source:** `coin_action_wire.py:110–111`  
Fold and Check are excluded from the paid-action distance projection. Even if a CHECK has equal monetary distance to a CALL, it is never substituted. "Fold/check are deliberately excluded: equal monetary distance does not make them equivalent to a paid action."

### I9: Active room wins over newer background turn for cc scheduling
**Source:** `coin_autoplay.py:128–133` and `selftest.py:420–427`  
When scheduling a CC, the bridge selects the turn from the active room (state `_hook_room`), never a newer background table. If the active room's turn is unavailable, it raises — it does not silently switch tables.

### I10: `FinishRoundHintRSP` idempotent emission
**Source:** `coin_bridge_live.py:1291–1303` (`finish_hint`)  
The `_pending_finish_hint` state is atomically claimed (set to `None` before I/O) so that scheduled, manual, and ACK paths racing cannot produce duplicate FinishRoundHintRSP frames. Verified by `selftest.py:430–455`.

---

## 5. Suggested v2 Module Layout

```
poker-eye-v2/core/
  sfs_wire.py          ← Port as-is: coin_action_wire.py:1–89
  cc_resolver.py       ← Port as-is: coin_action_wire.py:90–140
  cc_scheduler.py      ← Adapted: coin_autoplay.py (schedule_cc + maybe_inject logic)
  turn_state.py        ← Adapted: coin_autoplay.py (_turn_id, observe dedup)
  eye_cc_frame.py      ← Adapted: coin_bridge_live.py (lp_pack/lp_read, _eye_reader cc parsing)
  hint_guard.py        ← New: call_need==0 && CHECK legal check
  actions.py           ← EXISTS: already has ActionScheduler with MAX_ATTEMPTS=3
  protocol.py          ← EXISTS: already has frame/recv_frame/send_frame
  transport.py         ← EXISTS: TrainerServer
  sessions.py          ← EXISTS: SessionRegistry
  discovery.py         ← EXISTS: Broadcaster
  logging.py           ← EXISTS: SessionLogger
```

---

## 6. Observed Facts vs. Inference

| Statement | Classification | Evidence |
|-----------|---------------|----------|
| `build_game_user_action_packet(49453,5,.15)` produces exact 130-byte wire fixture | **Fact** | `selftest.py:5–8` — byte comparison against `base64.b64decode(...)` |
| SFS2X packet header is always `0x80` + 2-byte big-endian length | **Fact** | `coin_action_wire.py:39` — "exact form observed in CoinPoker captures" |
| `SCAction.lifetime` is a UI-message duration, not an action deadline | **Fact** | `selftest.py:457–465` — stock EYE captures execute actions whose delay > lifetime |
| `call=0 → backend CHECK 0 → Coin CALL 0` is a repeated inconsistency | **Fact** | Documented in `LOGGING_AND_CASES.md:3–8` with capture evidence |
| The retry delay default (2.5s) comes from p90 Coin ACK latency of 1.603s | **Inference** | `coin_bridge_live.py:122` comment attributes it to multitable trace; the exact p90 value is stated but not independently verified |
| `PPP_CLUB_ID = 3663333` is the target club for all synthesized PPP traffic | **Fact** | `coin_ppp_bridge.py:26` — hardcoded constant |
| The `ACTION` dict codes (1=FOLD, 2=CHECK, 3=CALL, 4=RAISE) differ from `resolve_eye_cc_action` codes (7=FOLD, 3=CHECK, 4=CALL, 5=RAISE) | **Fact** | Compared `coin_bridge_live.py:10` vs `coin_action_wire.py:101–102,139` |
| `call_need == 0` when hero is already matched and CHECK is legal is a valid wire value | **Fact** | `LOGGING_AND_CASES.md:5` — "ActionNotifyBRC.CallNeedChips=0 is a valid wire value" |
| The three-attempt semantics in v2 (1s gaps) match the legacy's intent | **Inference** | Legacy has 2 wire sends; v2 CONCEPT.md expands to 3 with 1s gaps. Not directly observed in legacy code but documented as v2 requirement. |

---

*End of migration map. All file:line references are to `C:\projects\pokereye\ready_v6\` unless prefixed with `poker-eye-v2/`.*