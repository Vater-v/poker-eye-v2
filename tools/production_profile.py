#!/usr/bin/env python3
import argparse
import datetime as dt
import subprocess
from pathlib import Path

PACKAGE = "com.coingames.coinpoker"

def run(cmd, input_text=None, timeout=None):
    try:
        return subprocess.run(
            cmd, input=input_text, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, (exc.stdout or "") + "\nTIMEOUT")

def online_devices():
    rows = []
    for line in run(["adb", "devices"]).stdout.splitlines()[1:]:
        if "\tdevice" in line:
            rows.append(line.split("\t", 1)[0].strip())
    return rows

PERFETTO_CONFIG = '\nbuffers: {\n  size_kb: 16384\n  fill_policy: RING_BUFFER\n}\ndata_sources: {\n  config {\n    name: "linux.ftrace"\n    ftrace_config {\n      ftrace_events: "sched/sched_switch"\n      ftrace_events: "sched/sched_wakeup"\n      ftrace_events: "sched/sched_waking"\n      ftrace_events: "power/cpu_frequency"\n      ftrace_events: "power/cpu_idle"\n      atrace_categories: "view"\n      atrace_categories: "gfx"\n      atrace_categories: "binder_driver"\n      atrace_categories: "dalvik"\n      atrace_apps: "com.coingames.coinpoker"\n    }\n  }\n}\ndata_sources: {\n  config {\n    name: "linux.process_stats"\n    process_stats_config {\n      scan_all_processes_on_start: true\n    }\n  }\n}\nduration_ms: 15000\n'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    rows = online_devices()
    if not rows:
        raise SystemExit("no online ADB devices")
    serial = args.serial or rows[0]
    if serial not in rows:
        raise SystemExit(f"ADB device not online: {serial}")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = repo / "logs" / f"native_profile_{stamp}_{serial.replace(':','_')}"
    out.mkdir(parents=True, exist_ok=True)

    prefix = ["adb", "-s", serial]
    pid_rows = run(prefix + ["shell", "pidof", PACKAGE]).stdout.strip().split()
    pid = pid_rows[0] if pid_rows else ""
    if not pid:
        raise SystemExit("CoinPoker process is not running")

    def save(name, result):
        (out / name).write_text(result.stdout or "", encoding="utf-8")

    save("abi.txt", run(prefix + ["shell", "sh", "-c",
        "echo ABI=$(getprop ro.product.cpu.abi); "
        "echo ABILIST=$(getprop ro.product.cpu.abilist); "
        f"echo PID={pid}"]))
    save("maps_hmuriy.txt", run(prefix + ["shell", "sh", "-c",
        f"grep -i 'libhmuriy\\|houdini\\|ndk_translation' /proc/{pid}/maps || true"]))
    save("status.txt", run(prefix + ["shell", "cat", f"/proc/{pid}/status"]))
    save("threads_before.txt", run(prefix + ["shell", "top", "-H", "-b", "-n", "1", "-p", pid]))
    save("cpuinfo_before.txt", run(prefix + ["shell", "dumpsys", "cpuinfo", PACKAGE]))
    save("logcat_before.txt", run(prefix + ["logcat", "-d", "-v", "threadtime", "-s", "Hmuriy"]))

    remote = "/data/misc/perfetto-traces/hmuriy_native.perfetto-trace"
    perf = run(
        prefix + ["shell", "perfetto", "--txt", "-c", "-", "-o", remote],
        input_text=PERFETTO_CONFIG, timeout=25,
    )
    save("perfetto_command.txt", perf)
    if perf.returncode == 0:
        save("perfetto_pull.txt", run(prefix + ["pull", remote, str(out / "hmuriy_native.perfetto-trace")]))
        run(prefix + ["shell", "rm", "-f", remote])

    simple = run(prefix + [
        "shell", "simpleperf", "stat", "-p", pid, "--duration", "5",
        "-e", "cpu-cycles,instructions,task-clock,context-switches,page-faults"
    ], timeout=10)
    save("simpleperf_stat.txt", simple)

    save("threads_after.txt", run(prefix + ["shell", "top", "-H", "-b", "-n", "1", "-p", pid]))
    save("cpuinfo_after.txt", run(prefix + ["shell", "dumpsys", "cpuinfo", PACKAGE]))
    logcat = run(prefix + ["logcat", "-d", "-v", "threadtime", "-s", "Hmuriy"])
    save("logcat_after.txt", logcat)

    perf_lines = [line for line in (logcat.stdout or "").splitlines() if "[perf]" in line]
    summary = [
        f"device={serial}",
        f"pid={pid}",
        "",
        "Native Hmuriy perf lines:",
        *perf_lines[-20:],
        "",
        "Targets:",
        "- tap p99 << 1000us; tens of us preferred.",
        "- drop/contention = 0 during normal play.",
        "- queue/high-water must not grow continuously.",
        "- copy/tx are background costs, not Coin WebSocket-thread costs.",
        "- reconnect = 0 while the public Trainer path is healthy.",
        "",
        "Open hmuriy_native.perfetto-trace in Perfetto UI if captured.",
    ]
    (out / "SUMMARY.txt").write_text("\n".join(summary), encoding="utf-8")
    print(f"[+] profile written: {out}")
    print("\n".join(perf_lines[-6:]) if perf_lines else "[~] no [perf] lines captured yet")

if __name__ == "__main__":
    main()
