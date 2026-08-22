export type AccountRow = {
  account_id: string;
  state: string;
  owner: string;
  validated: boolean;
  suffix: number | null;
  attempts: number;
  retry_in: number;
  last_error: string;
  warning?: string;
};

export type SeatRow = {
  seat: number;
  name: string;
  chips: number | null;
  playing: boolean;
};

export type TableRow = {
  table_id: number;
  table_no: number;
  state: string;
  account_id: string;
  phase: string;
  game_type: string;
  backend_health: string;
  backend_status: string;
  backend_message: string;
  fuel_quantity: number | null;
  fuel_rate_per_hand: number | null;
  hero_name: string;
  startup_attempts: number;
  startup_error: string;
  age_seconds: number;
  device_id: string;
  device_label: string;
  connected: boolean;
  hand_id?: string;
  pending_action?: boolean;
  players?: number;
  max_seats?: number;
  seats?: SeatRow[];
  hero_sitting?: boolean;
  standup_queued?: boolean;
  last_action?: string;
  last_amount?: number | null;
  last_action_source?: string;
  action_delay_ms?: number | null;
  hole_cards?: unknown[];
  hero_seat?: number;
  warning?: string;
  warning_text?: string;
  session_profit?: number | null;
  docs_status?: string;
  docs_hands?: number | null;
};

export type AutoPolicy = {
  enabled: boolean;
  play_enabled?: boolean;
  table_count: number;
  bb: number;
  watch_balance: boolean;
  watch_players: boolean;
  min_players: number;
  leave_below_bb: number;
  open_if_free_bb: number;
  ledger_enabled?: boolean;
};

export type AutomationState = {
  enabled?: boolean;
  play_enabled?: boolean;
  policy?: AutoPolicy;
  status?: string;
  wallet_cash?: number | null;
  wallet_bb?: number | null;
  catalog_bb?: number[];
  joining?: boolean;
  gradual_leave?: number;
  sitout_tables?: number[];
  ui?: {
    closed?: boolean;
    waitlist?: boolean;
    loading?: boolean;
    players?: number | null;
    timer?: number | null;
    janitor?: boolean;
    tap?: string;
    tap_age_ms?: number | null;
    nodes?: number;
    ts?: number;
  };
};

export type DeviceRow = {
  device_id: string;
  device_label: string;
  device_no?: number | null;
  connected: boolean;
  hero_name: string;
  display_name?: string;
  hands?: number;
  hands_by_type?: Record<string, number>;
  session_hands?: string;
  tables: TableRow[];
  automation?: AutomationState;
  warning?: string;
};

export type Snapshot = {
  ok?: boolean;
  build?: string;
  patch?: string;
  devices?: DeviceRow[];
  accounts?: AccountRow[];
  connected_devices?: number;
  fuel_remaining?: number | null;
  fuel_rate_per_hand?: number | null;
  fuel_hours_remaining?: number | null;
  fuel_per_minute?: number | null;
  fuel_per_hour?: number | null;
  fuel_active_tables?: number | null;
  run?: string;
  stale?: boolean;
  snapshot_error?: string;
  seated_tables?: number;
  live_tables?: number;
  staged_build?: string;
  reload_pending?: boolean;
};

export type ConsoleEvent = {
  ts?: string;
  event?: string;
  severity?: string;
  message?: string;
  reason?: string;
  device_id?: string;
  table_id?: number;
  account_id?: string;
  operator?: boolean;
  action?: string;
  amount?: number | null;
  detail?: Record<string, unknown>;
};

export type LogMode = "important" | "live" | "verbose";

function tokenFromLocation(): string {
  if (typeof window === "undefined") return "";
  const q = new URLSearchParams(window.location.search).get("token") || "";
  if (q) {
    sessionStorage.setItem("pokereye_token", q);
    return q;
  }
  return sessionStorage.getItem("pokereye_token") || "";
}

export function useConsole() {
  const config = useRuntimeConfig();
  const base = String(config.app.baseURL || "/pokereye/").replace(/\/?$/, "/");
  const snapshot = ref<Snapshot | null>(null);
  const events = ref<ConsoleEvent[]>([]);
  const run = ref<string>("");
  const health = ref<"live" | "offline">("offline");
  const error = ref("");
  const tab = ref<"fleet" | "logs" | "issues" | "control">("fleet");
  const expanded = ref<Record<string, boolean>>({});
  const filter = ref("");
  const paused = ref(false);
  const logMode = ref<LogMode>("important");
  const autoScroll = ref(true);
  const UI_KEY = "pokereye.console.ui";

  function loadUi() {
    if (typeof window === "undefined") return;
    try {
      const raw = JSON.parse(localStorage.getItem(UI_KEY) || "null");
      if (!raw || typeof raw !== "object") return;
      if (["fleet", "logs", "issues", "control"].includes(raw.tab)) tab.value = raw.tab;
      if (typeof raw.expanded === "string" && raw.expanded) {
        expanded.value = { [raw.expanded]: true };
      } else if (raw.expanded && typeof raw.expanded === "object") {
        expanded.value = raw.expanded as Record<string, boolean>;
      }
      if (typeof raw.filter === "string") filter.value = raw.filter;
      if (typeof raw.paused === "boolean") paused.value = raw.paused;
      if (["important", "live", "verbose"].includes(raw.logMode)) logMode.value = raw.logMode;
      if (typeof raw.autoScroll === "boolean") autoScroll.value = raw.autoScroll;
    } catch {
      /* keep defaults */
    }
  }

  function saveUi() {
    if (typeof window === "undefined") return;
    localStorage.setItem(UI_KEY, JSON.stringify({
      tab: tab.value,
      expanded: expanded.value,
      filter: filter.value,
      paused: paused.value,
      logMode: logMode.value,
      autoScroll: autoScroll.value,
    }));
  }

  async function api(path: string, init: RequestInit = {}) {
    const token = tokenFromLocation();
    const url = new URL(base + path.replace(/^\//, ""), window.location.origin);
    if (token) url.searchParams.set("token", token);
    const res = await fetch(url.toString(), {
      cache: "no-store",
      credentials: "same-origin",
      ...init,
    });
    const body = await res.json().catch(() => ({ ok: false, error: "bad json" }));
    if (!res.ok) throw new Error(body.error || String(res.status));
    return body;
  }

  async function refresh() {
    if (paused.value) return;
    try {
      const data = await api("api/state");
      const incoming = (data.snapshot || data) as Snapshot;
      const incomingDevices = incoming?.devices || [];
      const hadDevices = (snapshot.value?.devices || []).length > 0;
      if (incomingDevices.length === 0 && hadDevices) {
        snapshot.value = {
          ...(snapshot.value as Snapshot),
          stale: true,
          snapshot_error: String(incoming?.snapshot_error || "empty_live_snapshot"),
        };
      } else {
        snapshot.value = incoming;
      }
      events.value = data.events || events.value;
      run.value = String(data.run || run.value || "");
      health.value = "live";
      error.value = "";
    } catch (exc: any) {
      health.value = "offline";
      error.value = String(exc?.message || exc);
    }
  }

  async function control(path: string, body: Record<string, unknown>) {
    try {
      const data = await api("api/control/" + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await refresh();
      return data;
    } catch (exc: any) {
      try { await refresh(); } catch { /* keep previous snapshot */ }
      return { ok: false, error: String(exc?.message || exc) };
    }
  }

  function tokenUrl(path: string) {
    const token = tokenFromLocation();
    const url = new URL(base + path.replace(/^\//, ""), window.location.origin);
    if (token) url.searchParams.set("token", token);
    return url.toString();
  }

  function logsUrl() {
    return tokenUrl("api/logs");
  }

  function issuesUrl() {
    return tokenUrl("api/issues.zip");
  }

  onMounted(() => {
    loadUi();
    refresh();
    const id = setInterval(refresh, 1000);
    onUnmounted(() => clearInterval(id));
  });

  watch([tab, expanded, filter, paused, logMode, autoScroll], saveUi);

  return {
    base,
    snapshot,
    events,
    run,
    health,
    error,
    tab,
    expanded,
    filter,
    paused,
    logMode,
    autoScroll,
    refresh,
    control,
    logsUrl,
    issuesUrl,
  };
}
