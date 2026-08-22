<script setup lang="ts">
import type { AutoPolicy, ConsoleEvent, DeviceRow, LogMode, TableRow } from "~/composables/useConsole";

const {
  snapshot, events, run, health, error, tab, expanded, filter, paused,
  logMode, autoScroll, refresh, control, logsUrl, issuesUrl,
} = useConsole();

const clock = ref("");
const logBox = ref<HTMLElement | null>(null);
const autoOpen = ref<Record<string, boolean>>({});
const autoDraft = ref<Record<string, AutoPolicy>>({});
const gradualLeave = ref<Record<string, boolean>>({});
const BB_OPTIONS = [0.02, 0.05, 0.1, 0.2, 0.5, 1];
const PANEL_KEY = "pokereye.console.panels";

function loadPanels() {
  if (typeof window === "undefined") return;
  try {
    const raw = JSON.parse(localStorage.getItem(PANEL_KEY) || "null");
    if (!raw || typeof raw !== "object") return;
    if (raw.autoOpen && typeof raw.autoOpen === "object") autoOpen.value = raw.autoOpen;
    if (raw.autoDraft && typeof raw.autoDraft === "object") autoDraft.value = raw.autoDraft;
    if (raw.gradualLeave && typeof raw.gradualLeave === "object") gradualLeave.value = raw.gradualLeave;
  } catch {
    /* keep defaults */
  }
}

function savePanels() {
  if (typeof window === "undefined") return;
  localStorage.setItem(PANEL_KEY, JSON.stringify({
    autoOpen: autoOpen.value,
    autoDraft: autoDraft.value,
    gradualLeave: gradualLeave.value,
  }));
}

function defaultPolicy(): AutoPolicy {
  return {
    enabled: false,
    play_enabled: true,
    table_count: 5,
    bb: 0.02,
    watch_balance: true,
    watch_players: true,
    min_players: 3,
    leave_below_bb: 79,
    open_if_free_bb: 100,
    ledger_enabled: false,
  };
}

function policyOf(device: DeviceRow): AutoPolicy {
  return autoDraft.value[device.device_id] || device.automation?.policy || defaultPolicy();
}

function setDraft(device: DeviceRow, patch: Partial<AutoPolicy>) {
  autoDraft.value = {
    ...autoDraft.value,
    [device.device_id]: { ...policyOf(device), ...patch },
  };
}

async function toggleAutoPanel(device: DeviceRow, on: boolean) {
  autoOpen.value = { ...autoOpen.value, [device.device_id]: on };
  setDraft(device, { enabled: on });
  savePanels();
  await control("device/auto", {
    device_id: device.device_id,
    policy: { ...policyOf(device), enabled: on },
    apply: true,
  });
}

async function togglePlay(device: DeviceRow, on: boolean) {
  setDraft(device, { play_enabled: on });
  savePanels();
  await control("device/auto", {
    device_id: device.device_id,
    policy: { ...policyOf(device), play_enabled: on },
    apply: true,
  });
}

async function toggleWatchPlayers(device: DeviceRow, on: boolean) {
  setDraft(device, { watch_players: on });
  savePanels();
  await control("device/auto", {
    device_id: device.device_id,
    policy: { ...policyOf(device), watch_players: on, enabled: !!device.automation?.enabled },
    apply: true,
  });
}

async function toggleLedger(device: DeviceRow, on: boolean) {
  setDraft(device, { ledger_enabled: on });
  savePanels();
  await control("device/auto", {
    device_id: device.device_id,
    policy: { ...policyOf(device), ledger_enabled: on, enabled: !!device.automation?.enabled },
    apply: true,
  });
}

async function saveAuto(device: DeviceRow, apply = false) {
  const policy = { ...policyOf(device), enabled: apply ? true : (device.automation?.enabled || false) };
  await control("device/auto", { device_id: device.device_id, policy, apply });
  autoDraft.value = { ...autoDraft.value, [device.device_id]: policy };
  if (policy.enabled) autoOpen.value = { ...autoOpen.value, [device.device_id]: true };
  savePanels();
}

async function cancelAuto(device: DeviceRow) {
  const saved = device.automation?.policy || defaultPolicy();
  autoDraft.value = { ...autoDraft.value, [device.device_id]: { ...saved } };
  if (!saved.enabled) autoOpen.value = { ...autoOpen.value, [device.device_id]: false };
}

async function leaveAll(device: DeviceRow) {
  const gradual = gradualLeave.value[device.device_id] === true;
  const label = gradual ? "Покинуть все столы постепенно (2–10 мин на стол)?" : "Покинуть все столы сразу?";
  if (!confirm(label)) return;
  const result = await control("device/leave-all", { device_id: device.device_id, gradual }) as {
    ok?: boolean; error?: string; queued?: number;
  };
  if (!result || result.ok === false) {
    const why = String(result?.error || "ошибка");
    window.alert(`Не удалось поставить выход со всех столов: ${why}`);
  }
}
onMounted(() => {
  loadPanels();
  const tick = () => { clock.value = new Date().toLocaleTimeString(); };
  tick();
  const id = setInterval(tick, 1000);
  onUnmounted(() => clearInterval(id));
});
watch([autoOpen, autoDraft, gradualLeave], savePanels, { deep: true });

function fmtFuel(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toFixed(1);
}

function fmtAmount(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return "";
  const v = Number(n);
  if (Math.abs(v) < 1e-9) return "";
  return String(v);
}

function fmtProfit(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const sign = v > 0 ? "+" : "";
  return sign + v.toFixed(2);
}

function handsText(device: DeviceRow) {
  const by = device.hands_by_type || {};
  const order = ["NLH", "PLO4", "PLO5", "PLO6"];
  const parts = order
    .filter((name) => Number(by[name] || 0) > 0)
    .map((name) => `${name}: ${by[name]}`);
  for (const [name, count] of Object.entries(by)) {
    if (!order.includes(name) && Number(count) > 0) parts.push(`${name}: ${count}`);
  }
  if (parts.length) return parts.join(" | ");
  return "—";
}

function deviceName(device: DeviceRow) {
  return device.display_name || device.hero_name || device.device_label || `Устройство ${device.device_no || "?"}`;
}

function statusClass(state: string) {
  const s = String(state || "").toUpperCase();
  if (s === "READY" || s === "LEASED" || s === "AVAILABLE") return "ok";
  if (s === "FAILED" || s === "INVALID" || s === "RED") return "bad";
  return "warn";
}

function games(rows: TableRow[]) {
  const c: Record<string, number> = {};
  for (const t of rows) {
    const g = t.game_type || "—";
    c[g] = (c[g] || 0) + 1;
  }
  return Object.entries(c).map(([k, v]) => `${k} ${v}`).join(" · ") || "—";
}

function hintAmount(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return "0.0";
  const v = Number(n);
  if (Math.abs(v) < 1e-9) return "0.0";
  const s = v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  return s.includes(".") ? s : `${s}.0`;
}

function docsBadge(t: TableRow) {
  if (!t.hero_sitting) return "";
  const hands = Number(t.docs_hands || 0);
  if (hands >= 50) return "";
  const status = String(t.docs_status || "");
  if (status === "written") return "записали в docs";
  if (status === "dry" || status === "error" || status === "missing") return "docs не записались";
  return "";
}

function lastAction(t: TableRow) {
  const action = String(t.last_action || "").toUpperCase().replace(/\s+ACK$/, "");
  const source = String(t.last_action_source || "").toLowerCase();
  if (!action) return { empty: t.pending_action ? "ждём ход" : "—" };
  if (action === "PREFOLD" || source === "prefold") {
    return { action: "PREFOLD", amount: "", delay: "", delayWarn: false, source: "prefold" };
  }
  if (action === "FALLBACK" || source === "failsafe" || source === "fallback") {
    return { action: "FALLBACK", amount: "", delay: "", delayWarn: false, source: "failsafe" };
  }
  const delay = Math.max(0, Math.round(Number(t.action_delay_ms || 0) || 0));
  const delayWarn = delay === 0 && source && source !== "cc";
  return { action, amount: hintAmount(t.last_amount), delay, delayWarn, source };
}

function cardText(cards: unknown[] | undefined) {
  if (!Array.isArray(cards) || !cards.length) return "—";
  const values: Record<string, string> = {
    ACE: "A", KING: "K", QUEEN: "Q", JACK: "J", TEN: "10",
    NINE: "9", EIGHT: "8", SEVEN: "7", SIX: "6", FIVE: "5",
    FOUR: "4", THREE: "3", TWO: "2",
  };
  const suits: Record<string, string> = {
    SPADES: "♠", HEARTS: "♥", DIAMONDS: "♦", CLUBS: "♣",
  };
  return cards.map((row) => {
    if (!row || typeof row !== "object") return String(row);
    const item = row as Record<string, unknown>;
    const value = values[String(item.value || "").toUpperCase()] || String(item.value || "?");
    const suit = suits[String(item.suit || "").toUpperCase()] || "";
    return `${value}${suit}`;
  }).join(" ");
}

const fleet = computed(() => snapshot.value?.devices || []);

const stats = computed(() => {
  const devices = fleet.value;
  const tables = devices.flatMap((d) => (d.tables || []).filter((t) => t.state !== "closing" && t.phase !== "coin"));
  const snap = snapshot.value;
  const fuels = devices.map((d) => {
    const xs = (d.tables || []).map((t) => t.fuel_quantity).filter((n): n is number => n != null);
    return xs.length ? Math.max(...xs) : null;
  }).filter((n): n is number => n != null);
  const issues = events.value.filter((e) => isIssue(e)).length;
  return {
    devices: devices.length,
    online: devices.filter((d) => d.connected).length,
    tables: tables.length,
    ready: tables.filter((t) => t.state === "ready" || t.hero_sitting).length,
    fuel: snap?.fuel_remaining ?? (fuels.length ? Math.max(...fuels) : null),
    hours: snap?.fuel_hours_remaining ?? null,
    perHour: snap?.fuel_per_hour ?? null,
    activeTables: snap?.fuel_active_tables ?? null,
    issues,
  };
});

function isOperator(e: ConsoleEvent) {
  return e.operator === true || String(e.event || "").startsWith("operator.");
}

function isLiveAction(e: ConsoleEvent) {
  const ev = String(e.event || "");
  const tag = String((e.detail as { tag?: string } | undefined)?.tag || "");
  return [
    "operator.action_ready", "operator.action_sent", "operator.action_confirmed",
    "operator.hero_turn", "operator.cards", "operator.hand_started",
    "operator.hand_completed", "operator.prefold_ready", "operator.seated",
    "operator.standup_queued", "operator.automation.leave_all", "operator.automation.policy",
    "operator.automation.join_rejected", "operator.table_observing", "v6.bridge_diag",
    "operator.ui.dump",
  ].includes(ev) || ["action_ready", "hero_turn", "cards", "prefold_ready", "seated"].includes(tag);
}

function isIssue(e: ConsoleEvent) {
  const sev = String(e.severity || "").toUpperCase();
  const ev = String(e.event || "");
  return sev === "WARN" || sev === "ERROR" || ev.startsWith("error.");
}

function formatLog(e: ConsoleEvent) {
  const raw = String(e.message || "").replace(/^\d{2}:\d{2}:\d{2}\s+/, "").trim();
  if (raw) return raw;
  const action = String(e.action || (e.detail as { action?: string } | undefined)?.action || "").toUpperCase();
  const amount = e.amount ?? (e.detail as { amount?: number } | undefined)?.amount;
  if (action) {
    const amt = fmtAmount(typeof amount === "number" ? amount : null);
    return amt ? `ACTION ${action} · AMOUNT ${amt}` : `ACTION ${action}`;
  }
  return String(e.reason || e.event || "");
}

const filteredLogs = computed(() => {
  const q = filter.value.trim().toLowerCase();
  return (events.value || []).filter((e) => {
    if (logMode.value === "important" && !isOperator(e) && !isIssue(e)) return false;
    if (logMode.value === "live" && !isOperator(e) && !isLiveAction(e) && !isIssue(e)) return false;
    if (!q) return true;
    return formatLog(e).toLowerCase().includes(q) || JSON.stringify(e).toLowerCase().includes(q);
  }).slice(-500);
});

const issueRows = computed(() => {
  const q = filter.value.trim().toLowerCase();
  return (events.value || []).filter((e) => {
    if (!isIssue(e)) return false;
    if (!q) return true;
    return formatLog(e).toLowerCase().includes(q);
  }).slice(-300);
});

watch(filteredLogs, async () => {
  if (!autoScroll.value || tab.value !== "logs") return;
  await nextTick();
  const el = logBox.value;
  if (el) el.scrollTop = el.scrollHeight;
});

watch(tab, async (value) => {
  if (value !== "logs" || !autoScroll.value) return;
  await nextTick();
  const el = logBox.value;
  if (el) el.scrollTop = el.scrollHeight;
});

function toggle(id: string) {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] };
}

function allDevicesOpen(): boolean {
  const rows = fleet.value || [];
  return rows.length > 0 && rows.every((d) => !!expanded.value[d.device_id]);
}

function toggleAllDevices() {
  const rows = fleet.value || [];
  if (allDevicesOpen()) {
    expanded.value = {};
    return;
  }
  const next: Record<string, boolean> = {};
  for (const d of rows) next[d.device_id] = true;
  expanded.value = next;
}

async function restartTable(t: TableRow, device?: DeviceRow) {
  await control("table/restart", {
    device_id: t.device_id || device?.device_id || "",
    table_id: t.table_id,
  });
}
async function closeTable(t: TableRow, device?: DeviceRow) {
  if (!confirm(`Закрыть стол ${t.table_no || t.table_id}?`)) return;
  await control("table/close", {
    device_id: t.device_id || device?.device_id || "",
    table_id: t.table_id,
  });
}
async function resetDevice(deviceId: string) {
  if (!confirm("Сбросить роутер этого устройства?")) return;
  await control("device/reset", { device_id: deviceId });
}

const tabs: Array<[typeof tab.value, string]> = [
  ["fleet", "Флот"],
  ["logs", "Логи"],
  ["issues", "Проблемы"],
  ["control", "Управление"],
];
const modes: Array<[LogMode, string]> = [
  ["important", "Важное"],
  ["live", "Столы"],
  ["verbose", "Всё"],
];
</script>

<template>
  <div class="h-full min-h-0 grid grid-rows-[auto_40px_1fr] bg-[#0b0d10] text-[#e8edf4]">
    <header class="grid grid-cols-1 lg:grid-cols-[1fr_auto_1fr] items-center gap-2 px-3 py-1 border-b border-[#262c35] bg-[#0e1115]"
      :class="stats.fuel != null && stats.fuel < 1000 ? 'pe-fuel-low' : ''">
      <div class="flex items-center gap-2 min-w-0">
        <div class="w-5 h-5 shrink-0 rounded-md bg-[#1b2430] border border-[#2c3644] grid place-items-center text-[9px] font-extrabold tracking-wide">PE</div>
        <div class="min-w-0">
          <div class="text-[12px] font-semibold leading-none">PokerEye</div>
          <div class="text-[10px] text-[#8b95a3] mt-0.5 truncate">
            <span :class="health === 'live' ? 'text-[#53d18c]' : 'text-[#ff6b73]'">{{ health === "live" ? "онлайн" : "оффлайн" }}</span>
            · {{ snapshot?.build || "—" }}
            <span v-if="snapshot?.reload_pending" class="text-[#ffb020]">
              · staged {{ snapshot.staged_build }} (после пустых столов)
            </span>
            · {{ stats.online }}/{{ stats.devices }} устройств
            · {{ stats.ready }}/{{ stats.tables }} столов
          </div>
        </div>
      </div>
      <div class="text-center px-2">
        <div class="text-[9px] tracking-[0.18em] font-bold" :class="stats.fuel != null && stats.fuel < 1000 ? 'text-[#ff6b73]' : 'text-[#8b95a3]'">FUEL</div>
        <div class="text-[18px] font-semibold tabular-nums leading-none mt-0.5">{{ stats.hours != null ? ("хватит на " + stats.hours.toFixed(1) + " ч") : "—" }}</div>
        <div class="text-[11px] text-[#8b95a3] mt-1">
          <span v-if="stats.perHour != null">{{ stats.perHour.toFixed(1) }} /ч</span>
          <span v-if="stats.activeTables != null"> · {{ stats.activeTables }} {{ stats.activeTables === 1 ? "стол" : "столов" }}</span>
          <span v-if="(stats.perHour != null || stats.activeTables != null) && stats.fuel != null"> · </span>
          <span v-if="stats.fuel != null">{{ fmtFuel(stats.fuel) }} F</span>
          <span v-if="stats.perHour == null && stats.fuel == null">live pool</span>
        </div>
      </div>
      <div class="flex items-center justify-end gap-3 text-[12px] text-[#c5ccd6]">
        <div class="text-right leading-tight">
          <div>{{ clock }}</div>
          <div :class="stats.issues ? 'text-[#ff6b73]' : 'text-[#8b95a3]'">{{ stats.issues }} проблем</div>
        </div>
        <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#4a5565]" @click="paused = !paused">{{ paused ? "Продолжить" : "Пауза" }}</button>
      </div>
    </header>

    <nav class="flex items-stretch gap-1 px-3 border-b border-[#262c35] bg-[#10141a] min-w-0">
      <button v-for="item in tabs" :key="item[0]"
        class="px-3 text-[12px] border-b-2 shrink-0"
        :class="tab === item[0] ? 'border-[#70a7ff] text-white' : 'border-transparent text-[#8b95a3] hover:text-white'"
        @click="tab = item[0]"
      >{{ item[1] }}</button>
      <div class="flex-1" />
      <div v-if="error" class="self-center text-[11px] text-[#ff6b73] truncate max-w-[40%]">{{ error }}</div>
      <div class="self-center text-[11px] text-[#8b95a3] truncate">{{ run || "нет сессии" }}</div>
    </nav>

    <main class="min-h-0 overflow-hidden p-3">
      <section v-if="tab === 'fleet'" class="h-full min-h-0 overflow-auto rounded-lg border border-[#262c35]">
        <table class="w-full text-[12px] table-fixed">
          <thead class="bg-[#10141a] text-[#8b95a3] text-left sticky top-0">
            <tr>
              <th class="px-3 py-2 font-medium w-[20%]">
                <div class="flex items-center justify-between gap-2">
                  <span>Устройство</span>
                  <button type="button" class="text-[10px] text-[#70a7ff] hover:text-white shrink-0" @click.stop="toggleAllDevices">
                    {{ allDevicesOpen() ? "скрыть все" : "раскрыть все" }}
                  </button>
                </div>
              </th>
              <th class="px-3 py-2 font-medium w-[8%]">Auto</th>
              <th class="px-3 py-2 font-medium w-[10%]">Связь</th>
              <th class="px-3 py-2 font-medium w-[8%]">Столы</th>
              <th class="px-3 py-2 font-medium w-[26%]">Руки</th>
              <th class="px-3 py-2 font-medium w-[10%]">Fuel</th>
              <th class="px-3 py-2 font-medium w-[18%]">Игры</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="d in fleet" :key="d.device_id">
              <tr class="border-t border-[#1c232c] cursor-pointer hover:bg-[#12171e]" @click="toggle(d.device_id)">
                <td class="px-3 py-2 min-w-0">
                  <div class="font-semibold truncate flex items-center gap-2">
                    <span v-if="d.warning === 'DB' || (d.tables || []).some(t => t.warning === 'DB')" class="inline-block w-2.5 h-2.5 rounded-full bg-[#ff6b73] shrink-0" title="Warning: DB"></span>
                    {{ deviceName(d) }}
                  </div>
                  <div class="text-[10px] text-[#8b95a3] font-mono truncate">{{ d.device_label || d.device_id }}</div>
                </td>
                <td class="px-3 py-2" @click.stop>
                  <label class="inline-flex items-center gap-1 text-[11px] mr-2">
                    <input type="checkbox" :checked="!!d.automation?.enabled" @change="toggleAutoPanel(d, ($event.target as HTMLInputElement).checked)" />
                    auto
                  </label>
                  <label class="inline-flex items-center gap-1 text-[11px]">
                    <input type="checkbox" :checked="policyOf(d).play_enabled !== false" @change="togglePlay(d, ($event.target as HTMLInputElement).checked)" />
                    play
                  </label>
                  <label class="inline-flex items-center gap-1 text-[11px]">
                    <input type="checkbox" :checked="policyOf(d).watch_players !== false" @change="toggleWatchPlayers(d, ($event.target as HTMLInputElement).checked)" />
                    &lt;3 игрока
                  </label>
                  <label class="inline-flex items-center gap-1 text-[11px]">
                    <input type="checkbox" :checked="!!policyOf(d).ledger_enabled" @change="toggleLedger(d, ($event.target as HTMLInputElement).checked)" />
                    учет
                  </label>
                </td>
                <td class="px-3 py-2">
                  <span class="uppercase tracking-wide text-[10px] font-bold" :class="d.connected ? 'text-[#53d18c]' : 'text-[#ff6b73]'">
                    {{ d.connected ? "online" : "offline" }}
                  </span>
                </td>
                <td class="px-3 py-2 tabular-nums">{{ (d.tables || []).filter(t => t.phase !== 'coin' && t.state !== 'closing').length }}</td>
                <td class="px-3 py-2 tabular-nums text-[#c5ccd6] truncate">{{ handsText(d) }}</td>
                <td class="px-3 py-2 tabular-nums">{{ fmtFuel((d.tables || []).map(t => t.fuel_quantity).filter((n): n is number => n != null)[0]) }}</td>
                <td class="px-3 py-2 text-[#c5ccd6] truncate">{{ games(d.tables || []) }}</td>
              </tr>
              <tr v-if="autoOpen[d.device_id] || d.automation?.enabled">
                <td colspan="7" class="bg-[#10151c] px-3 py-3 border-t border-[#1c232c]" @click.stop>
                  <div class="flex flex-wrap items-end gap-3 text-[12px]">
                    <label class="grid gap-1">
                      <span class="text-[10px] text-[#8b95a3]">Столов</span>
                      <input class="w-16 bg-[#0d1014] border border-[#262c35] rounded px-2 py-1" type="number" min="1" max="8"
                        :value="policyOf(d).table_count" @input="setDraft(d, { table_count: Number(($event.target as HTMLInputElement).value) || 1 })" />
                    </label>
                    <label class="grid gap-1">
                      <span class="text-[10px] text-[#8b95a3]">Лимит BB</span>
                      <select class="bg-[#0d1014] border border-[#262c35] rounded px-2 py-1"
                        :value="policyOf(d).bb" @change="setDraft(d, { bb: Number(($event.target as HTMLSelectElement).value) })">
                        <option v-for="bb in (d.automation?.catalog_bb || BB_OPTIONS)" :key="bb" :value="bb">{{ bb }}</option>
                      </select>
                    </label>
                    <label class="flex items-center gap-2 py-1">
                      <input type="checkbox" :checked="policyOf(d).watch_balance" @change="setDraft(d, { watch_balance: ($event.target as HTMLInputElement).checked })" />
                      баланс: выйти &lt; 79BB, сесть если &gt; 100BB свободно
                    </label>
                    <label class="flex items-center gap-2 py-1">
                      <input type="checkbox" :checked="policyOf(d).watch_players" @change="setDraft(d, { watch_players: ($event.target as HTMLInputElement).checked })" />
                      игроки: выйти если &lt; 3 включая hero
                    </label>
                  </div>
                  <div class="flex flex-wrap items-center gap-2 mt-3 text-[12px]">
                    <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#70a7ff]" @click="saveAuto(d, false)">Сохранить</button>
                    <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#53d18c]" @click="saveAuto(d, true)">Применить</button>
                    <button class="px-2 py-1 rounded border border-[#2c3644]" @click="cancelAuto(d)">Отмена</button>
                    <span class="text-[11px] text-[#8b95a3]">{{ d.automation?.status || "idle" }}<span v-if="d.automation?.wallet_bb != null"> · кошелёк {{ d.automation.wallet_bb }} BB</span></span>
                    <span class="text-[11px] font-mono" :class="d.automation?.ui?.janitor ? 'text-[#53d18c]' : 'text-[#ff6b73]'">
                      ui {{ d.automation?.ui?.janitor ? "dump" : "нет dump" }}
                      <span v-if="d.automation?.ui?.closed"> · TABLE CLOSED</span>
                      <span v-if="d.automation?.ui?.waitlist"> · waitlist</span>
                      <span v-if="d.automation?.ui?.timer"> · {{ d.automation.ui.timer }}s</span>
                      <span v-if="d.automation?.ui?.tap"> · tap {{ d.automation.ui.tap }}</span>
                      <span v-if="d.automation?.ui?.nodes"> · {{ d.automation.ui.nodes }}n</span>
                    </span>
                    <div class="flex-1" />
                    <label class="flex items-center gap-1 text-[11px] text-[#8b95a3]">
                      <input type="checkbox" :checked="gradualLeave[d.device_id] === true" @change="gradualLeave = { ...gradualLeave, [d.device_id]: ($event.target as HTMLInputElement).checked }" />
                      постепенно
                    </label>
                    <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#ff6b73]" @click="leaveAll(d)">Покинуть все столы</button>
                  </div>
                </td>
              </tr>
              <tr v-if="expanded[d.device_id]">
                <td colspan="7" class="bg-[#0c1015] px-3 py-2">
                  <div v-if="!(d.tables || []).length" class="text-[#8b95a3] text-[12px] py-2">Нет живых столов на этом устройстве.</div>
                  <div v-for="t in d.tables || []" :key="t.table_id" class="grid grid-cols-1 xl:grid-cols-[220px_1fr_auto] gap-3 items-start py-3 border-t border-[#1c232c] first:border-0">
                    <div>
                      <div class="font-semibold flex items-center gap-2">
                        <span v-if="t.warning === 'DB'" class="inline-block w-2.5 h-2.5 rounded-full bg-[#ff6b73] shrink-0" title="Warning: DB"></span>
                        Стол {{ t.table_no || "?" }} <span class="text-[#8b95a3] font-normal">{{ t.game_type || "—" }}</span>
                      </div>
                      <div v-if="t.warning === 'DB'" class="text-[11px] text-[#ff6b73] font-semibold mt-0.5">Warning: DB</div>
                      <div class="text-[11px] text-[#8b95a3]">{{ t.hero_name || deviceName(d) }} · {{ d.connected ? "online" : "offline" }}</div>
                      <div class="text-[11px] mt-1">{{ t.state === 'starting' ? "подключаем Eye" : (t.state === 'failed' ? "Eye не поднялся" : (t.phase === 'coin' ? "вкладка Coin без сессии" : (t.state === 'closing' ? "вкладка Coin" : (t.hero_sitting ? "сидим" : "наблюдаем")))) }}<span v-if="t.state === 'observing' && t.phase !== 'coin'" class="text-[#8b95a3]"> · лобби</span><span v-if="t.state === 'closing'" class="text-[#8b95a3]"> · ещё открыта</span><span v-if="t.standup_queued" class="text-[#e7bb55]"> · стендап после руки</span></div>
                      <div v-if="docsBadge(t)" class="text-[11px] mt-0.5" :class="docsBadge(t) === 'записали в docs' ? 'text-[#53d18c]' : 'text-[#e7bb55]'">{{ docsBadge(t) }}</div>
                    </div>
                    <div class="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1 text-[11px] min-w-0">
                      <div><span class="text-[#8b95a3]">игроки</span> {{ t.players ?? "—" }}<span v-if="t.max_seats"> / {{ t.max_seats }}</span></div>
                      <div><span class="text-[#8b95a3]">фаза</span> {{ t.phase || "—" }}</div>
                      <div><span class="text-[#8b95a3]">раздача</span> {{ t.hand_id || "—" }}</div>
                      <div>
                        <span class="text-[#8b95a3]">ход</span>
                        <template v-if="lastAction(t).empty"> {{ lastAction(t).empty }}</template>
                        <template v-else>
                          {{ lastAction(t).action }} {{ lastAction(t).amount }}
                          <span :class="lastAction(t).delayWarn ? 'text-[#ff6b73] font-semibold' : 'text-[#5d6670] font-normal'"> {{ lastAction(t).delay }}</span>
                        </template>
                      </div>
                      <div><span class="text-[#8b95a3]">карты</span> {{ cardText(t.hole_cards) }}</div>
                      <div><span class="text-[#8b95a3]">fuel</span> {{ fmtFuel(t.fuel_quantity) }}</div>
                      <div v-if="t.session_profit != null"><span class="text-[#8b95a3]">профит</span> {{ fmtProfit(t.session_profit) }}</div>
                      <div class="col-span-2 md:col-span-3 flex items-center gap-2">
                        <span v-if="t.warning === 'DB'" class="inline-block w-2.5 h-2.5 rounded-full bg-[#ff6b73] shrink-0"></span>
                        <span class="text-[#8b95a3]">PokerEYE</span> {{ t.account_id || "—" }} · {{ t.backend_health || "—" }} / {{ t.backend_status || "—" }}
                      </div>
                      <div v-if="(t.seats || []).length" class="col-span-2 md:col-span-3 text-[#c5ccd6]">
                        {{ (t.seats || []).map(s => `${s.seat}:${s.name || "—"}`).join(" · ") }}
                      </div>
                      <div class="col-span-2 md:col-span-3 text-[#ff6b73]">{{ t.startup_error || t.backend_message || "" }}</div>
                    </div>
                    <div class="flex gap-2 shrink-0">
                      <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#70a7ff]" @click.stop="restartTable(t, d)">↻ PokerEYE</button>
                      <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#ff6b73]" @click.stop="closeTable(t, d)">× Закрыть</button>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="!fleet.length" class="p-8 text-center text-[#8b95a3]">Нет подключённых устройств</div>
      </section>

      <section v-else-if="tab === 'logs'" class="h-full min-h-0 flex flex-col gap-2">
        <div class="flex flex-wrap gap-2 items-center">
          <div class="flex rounded border border-[#262c35] overflow-hidden text-[11px]">
            <button v-for="mode in modes" :key="mode[0]" class="px-3 py-2"
              :class="logMode === mode[0] ? 'bg-[#1b2430] text-white' : 'text-[#8b95a3]'"
              @click="logMode = mode[0]">{{ mode[1] }}</button>
          </div>
          <input v-model="filter" class="flex-1 min-w-[160px] bg-[#0d1014] border border-[#262c35] rounded px-3 py-2 text-[12px]" placeholder="фильтр" />
          <label class="text-[11px] text-[#8b95a3] flex items-center gap-1">
            <input v-model="autoScroll" type="checkbox" /> автоскролл
          </label>
          <button class="px-3 py-2 rounded border border-[#2c3644]" @click="refresh">Обновить</button>
        </div>
        <div ref="logBox" class="flex-1 min-h-0 overflow-auto rounded-lg border border-[#262c35] bg-[#090b0e] font-mono text-[11px]">
          <div v-for="(e, i) in filteredLogs" :key="i" class="grid grid-cols-[64px_52px_minmax(0,1fr)] gap-2 px-2 py-1 border-b border-[#181d24]">
            <span class="text-[#8b95a3] tabular-nums">{{ String(e.ts || "").slice(11,19) }}</span>
            <span :class="{
              'text-[#ff6b73]': String(e.severity).toUpperCase()==='ERROR',
              'text-[#e7bb55]': String(e.severity).toUpperCase()==='WARN',
              'text-[#8b95a3]': !['ERROR','WARN'].includes(String(e.severity).toUpperCase()),
            }">{{ e.severity || "INFO" }}</span>
            <span class="break-words whitespace-pre-wrap">{{ formatLog(e) }}</span>
          </div>
          <div v-if="!filteredLogs.length" class="p-8 text-center text-[#8b95a3]">Нет событий</div>
        </div>
      </section>

      <section v-else-if="tab === 'issues'" class="h-full min-h-0 flex flex-col gap-2">
        <div class="flex gap-2">
          <input v-model="filter" class="flex-1 bg-[#0d1014] border border-[#262c35] rounded px-3 py-2 text-[12px]" placeholder="фильтр проблем" />
          <a class="px-3 py-2 rounded border border-[#2c3644] hover:border-[#70a7ff]" :href="issuesUrl()">Скачать zip</a>
        </div>
        <div class="flex-1 min-h-0 overflow-auto rounded-lg border border-[#262c35] bg-[#090b0e] font-mono text-[11px]">
          <div v-for="(e, i) in issueRows" :key="i" class="grid grid-cols-[64px_52px_minmax(0,1fr)] gap-2 px-2 py-1 border-b border-[#181d24]">
            <span class="text-[#8b95a3] tabular-nums">{{ String(e.ts || "").slice(11,19) }}</span>
            <span :class="String(e.severity).toUpperCase()==='ERROR' ? 'text-[#ff6b73]' : 'text-[#e7bb55]'">{{ e.severity || "WARN" }}</span>
            <span class="break-words whitespace-pre-wrap">{{ formatLog(e) }}</span>
          </div>
          <div v-if="!issueRows.length" class="p-8 text-center text-[#8b95a3]">Проблем нет</div>
        </div>
      </section>

      <section v-else class="h-full min-h-0 overflow-auto grid gap-3 max-w-3xl">
        <div class="rounded-lg border border-[#262c35] p-4 bg-[#111419]">
          <h2 class="text-[14px] font-semibold mb-2">Сессия</h2>
          <p class="text-[12px] text-[#8b95a3] mb-3">Тренер отдельно. Эта страница говорит только с token API.</p>
          <div class="flex flex-wrap gap-2">
            <a class="px-3 py-2 rounded border border-[#2c3644] hover:border-[#70a7ff]" :href="logsUrl()">events.jsonl</a>
            <a class="px-3 py-2 rounded border border-[#2c3644] hover:border-[#70a7ff]" :href="issuesUrl()">zip проблем</a>
            <button class="px-3 py-2 rounded border border-[#2c3644]" @click="refresh">Обновить</button>
          </div>
        </div>
        <div class="rounded-lg border border-[#262c35] p-4 bg-[#111419]">
          <h2 class="text-[14px] font-semibold mb-2">Устройства</h2>
          <div v-for="d in fleet" :key="d.device_id" class="flex items-center gap-3 py-2 border-t border-[#1c232c] first:border-0">
            <div class="flex-1 min-w-0">
              <div class="font-medium truncate">{{ deviceName(d) }}</div>
              <div class="text-[11px] text-[#8b95a3] font-mono truncate">{{ d.device_id }} · {{ (d.tables||[]).length }} столов · {{ handsText(d) }}</div>
            </div>
            <button class="px-2 py-1 rounded border border-[#2c3644] hover:border-[#ff6b73]" @click="resetDevice(d.device_id)">Сброс</button>
          </div>
          <div v-if="!fleet.length" class="text-[#8b95a3] text-[12px]">Нет устройств</div>
        </div>
      </section>
    </main>
  </div>
</template>

<style>
@keyframes pe-fuel-blink {
  0%, 100% { background-color: #3a1014; color: #ffe8ea; }
  50% { background-color: #f4f4f4; color: #1a1a1a; }
}
.pe-fuel-low {
  animation: pe-fuel-blink 0.9s steps(2, end) infinite;
}
.pe-fuel-low .text-\[18px\] {
  color: inherit;
}
</style>
