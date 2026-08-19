export type AccountRow = {
  account_id: string;
  state: string;
  owner: string;
  validated: boolean;
  suffix: number | null;
  attempts: number;
  retry_in: number;
  last_error: string;
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
  hole_cards?: unknown[];
  hero_seat?: number;
};

export type AutoPolicy = {
  enabled: boolean;
  table_count: number;
  bb: number;
  watch_balance: boolean;
  watch_players: boolean;
  min_players: number;
  leave_below_bb: number;
  open_if_free_bb: number;
};

export type AutomationState = {
  enabled?: boolean;
  policy?: AutoPolicy;
  status?: string;
  wallet_cash?: number | null;
  wallet_bb?: number | null;
  catalog_bb?: number[];
  joining?: boolean;
  gradual_leave?: number;
  sitout_tables?: number[];
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
};

export type Snapshot = {
  ok?: boolean;
  build?: string;
  patch?: string;
  devices?: DeviceRow[];
  accounts?: AccountRow[];
  connected_devices?: number;
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
  const expanded = ref<string>("");
  const filter = ref("");
  const paused = ref(false);
  const logMode = ref<LogMode>("important");
  const autoScroll = ref(true);

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
      snapshot.value = data.snapshot || data;
      events.value = data.events || [];
      run.value = data.run || "";
      health.value = "live";
      error.value = "";
    } catch (exc: any) {
      health.value = "offline";
      error.value = String(exc?.message || exc);
    }
  }

  async function control(path: string, body: Record<string, unknown>) {
    await api("api/control/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await refresh();
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
    refresh();
    const id = setInterval(refresh, 1000);
    onUnmounted(() => clearInterval(id));
  });

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
