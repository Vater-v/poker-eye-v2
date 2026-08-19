from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


EVENT_SCHEMA_VERSION = 1


class Health(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


@dataclass(frozen=True)
class DeviceInfo:
    """One Android transport as reported by ``adb devices -l``.

    ``display_name`` is the user-assigned LDPlayer instance title when it can be
    correlated without guessing.  Otherwise it deliberately falls back to the
    model/serial so two emulators are never silently swapped in the GUI.
    """

    serial: str
    state: str = "device"
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    display_name: str = ""
    ld_index: Optional[int] = None
    boot_id: str = ""

    @property
    def device_id(self) -> str:
        return self.serial

    def with_display_name(self, name: str, ld_index: Optional[int] = None) -> "DeviceInfo":
        return DeviceInfo(
            serial=self.serial,
            state=self.state,
            model=self.model,
            product=self.product,
            device=self.device,
            transport_id=self.transport_id,
            display_name=name,
            ld_index=ld_index,
            boot_id=self.boot_id,
        )


@dataclass(frozen=True)
class DevicePorts:
    bridge_host_port: int
    bridge_device_port: int = 18010
    eye_host_port: Optional[int] = None
    eye_device_port: int = 17770


@dataclass(frozen=True)
class AccountLease:
    owner: str
    account_id: str
    token: str
    leased_at: float


@dataclass
class TableState:
    device_id: str
    table_id: Optional[int] = None
    account_id: Optional[str] = None
    game_type: Optional[str] = None
    coin_bb: Optional[float] = None
    hand_id: Optional[str] = None
    pending: bool = False
    status: Health = Health.YELLOW
    reason: str = "starting"
    completed_hands: int = 0
    backend_status: Optional[str] = None
    backend_message: str = ""
    backend_hash: str = ""
    fuel_quantity: Optional[float] = None
    fuel_rate_per_hand: Optional[float] = None
    fuel_reason_code: str = "FUEL_PENDING"
    fuel_sequence: int = 0
    fuel_low_threshold: float = 1500.0
    fuel_observed: bool = False


@dataclass
class DeviceState:
    info: DeviceInfo
    ports: Optional[DevicePorts] = None
    online: bool = True
    status: Health = Health.YELLOW
    reason: str = "discovered"
    session_started_at: float = field(default_factory=time.time)
    session_hands: int = 0
    worker_pid: Optional[int] = None
    account_id: Optional[str] = None
    table: Optional[TableState] = None
    # `table` remains a compatibility pointer for the legacy singleton worker.
    # Router mode owns all concurrent rows here and never aliases their state.
    tables: dict[int, TableState] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RuntimeEvent:
    """Stable GUI boundary, serialized as one compact JSON object per line."""

    kind: str
    device_id: str
    display_name: str
    status: str
    reason: str = ""
    device_status: Optional[str] = None
    device_reason: str = ""
    table_id: Optional[int] = None
    account_id: Optional[str] = None
    game_type: Optional[str] = None
    coin_bb: Optional[float] = None
    hand_id: Optional[str] = None
    hand_completed: bool = False
    session_hands: int = 0
    pending: bool = False
    worker_pid: Optional[int] = None
    ingress_port: Optional[int] = None
    sequence: int = 0
    ts: float = field(default_factory=time.time)
    schema: int = EVENT_SCHEMA_VERSION
    detail: dict[str, Any] = field(default_factory=dict)
    backend_status: Optional[str] = None
    backend_message: str = ""
    backend_hash: str = ""
    fuel_quantity: Optional[float] = None
    fuel_rate_per_hand: Optional[float] = None
    fuel_reason_code: str = "FUEL_PENDING"
    fuel_sequence: int = 0
    fuel_low_threshold: float = 1500.0
    fuel_observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "RuntimeEvent":
        data = json.loads(raw)
        if int(data.get("schema", 0)) != EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported runtime event schema {data.get('schema')!r}")
        return cls(**data)


@dataclass(frozen=True)
class RuntimeCapabilities:
    adb_per_device_routing: bool = True
    arbitrary_ldplayer_titles: bool = True
    one_worker_per_emulator: bool = True
    true_multitable_per_emulator: bool = True
    table_account_leases: bool = True
    active_table_only_leasing: bool = True
    structured_json_events: bool = True
    multitable_blocker: str = ""
