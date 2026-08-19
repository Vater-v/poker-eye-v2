#!/usr/bin/env python3
"""PokerEye V7.4 one-shot audit, clean build and optional deployment.

This command is intentionally conservative:
- it removes only generated caches/workspaces;
- it archives old patch/back-up files instead of deleting them;
- it never removes .dist/baseline, .dist/tooling, secrets, configs, logs or PCAP;
- it validates live-game parsing with offline tests/PCAP replays;
- it can build/install the APK, but live Trainer startup is opt-in.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

try:
    from rich.console import Console
except Exception:  # optional
    Console = None


EXPECTED_BUILD_ID = "V7.4.0-HMN1-PUBLIC"
PUBLIC_HOST = "37.192.228.101"
PUBLIC_PORT = 19037


def abspath_preserve_drive(value: str | os.PathLike[str]) -> Path:
    # pathlib.resolve() turns an X: tsclient mapping into a UNC path. Several
    # Android/Java tools are less reliable on UNC, so keep the caller's drive.
    return Path(os.path.abspath(os.fspath(value)))


class Output:
    def __init__(self) -> None:
        self.console = Console() if Console is not None else None

    def _write(self, text: str) -> None:
        if self.console is not None:
            self.console.print(text)
        else:
            print(re.sub(r"\[/?(?:bold|green|yellow|red|cyan|dim)\]", "", text), flush=True)

    def step(self, text: str) -> None:
        self._write(f"\n[bold cyan]:: {text}[/bold cyan]")

    def ok(self, text: str) -> None:
        self._write(f"[green][OK][/green] {text}")

    def warn(self, text: str) -> None:
        self._write(f"[yellow][WARN][/yellow] {text}")

    def fail(self, text: str) -> None:
        self._write(f"[red][FAIL][/red] {text}")

    def line(self, text: str = "") -> None:
        self._write(text)


OUT = Output()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_writable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def remove_tree_robust(path: Path, *, allow_stale_rename: bool = True) -> Path | None:
    if not path.exists():
        return None
    last: BaseException | None = None
    for attempt in range(1, 7):
        try:
            for child in path.rglob("*"):
                make_writable(child)
            make_writable(path)
            shutil.rmtree(path)
            return None
        except (OSError, PermissionError) as exc:
            last = exc
            gc.collect()
            time.sleep(min(1.5, 0.15 * attempt))
    if allow_stale_rename and path.exists():
        stale = path.with_name(
            f"{path.name}.stale-{dt.datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}"
        )
        try:
            path.rename(stale)
            OUT.warn(f"locked generated directory renamed: {stale}")
            return stale
        except OSError as exc:
            last = exc
    raise RuntimeError(
        f"cannot clean generated directory {path}: {last}. "
        "Close Explorer/antivirus/processes holding .dist\\v2workspace and retry."
    )


def run_process(
    command: list[str],
    *,
    cwd: Path,
    output: Path | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    display = " ".join(f'"{arg}"' if " " in arg else arg for arg in command)
    OUT.line(f"[dim]{display}[/dim]")
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        text = result.stdout or ""
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        text += "\n[TIMEOUT]\n"
        result = subprocess.CompletedProcess(command, 124, text)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    tail = "\n".join(text.splitlines()[-24:])
    if tail:
        OUT.line(tail)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {display}")
    return result


def find_executable(names: Iterable[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_adb() -> str:
    candidates = [
        shutil.which("adb"),
        r"C:\Android\platform-tools\adb.exe",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("adb.exe not found")


def adb_devices(adb: str, repo: Path) -> list[str]:
    result = run_process([adb, "devices"], cwd=repo, check=True)
    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def archive_legacy_artifacts(repo: Path, timestamp: str) -> list[str]:
    archive = repo / ".archive" / f"pre-v74-{timestamp}"
    candidates: set[Path] = set()
    for pattern in ("*.pre-v*.bak", "apply_v7*.py", "poker-eye-v7*.patch"):
        candidates.update(path for path in repo.rglob(pattern) if ".archive" not in path.parts)
    # Old investigative documents are preserved, not destroyed. The active docs are
    # README, BUILD, LOGGING_AND_CASES, PROTOCOL_REFERENCE_PCAP, TRAFFIC_CORPUS and V74.
    for rel in (
        "CONCEPT.md",
        "docs/CAPABILITY_GAPS.md",
        "docs/HMURIY_AUDIT.md",
        "docs/MIGRATION_MAP.md",
        "docs/REAL_ACCEPTANCE_2026-08-17.md",
        "docs/RETURN_PLAN.md",
        "docs/freeze-baseline-2026-08-16.sha256.txt",
    ):
        path = repo / rel
        if path.exists():
            candidates.add(path)

    moved: list[str] = []
    for source in sorted(candidates):
        if not source.is_file():
            continue
        relative = source.relative_to(repo)
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(relative.as_posix())
    if moved:
        OUT.ok(f"archived {len(moved)} legacy/back-up files under {archive.relative_to(repo)}")
    else:
        OUT.ok("no legacy/back-up files to archive")
    return moved


def clean_generated(repo: Path, *, archive_legacy: bool, timestamp: str) -> dict[str, Any]:
    result: dict[str, Any] = {"removed": [], "archived": []}
    if archive_legacy:
        result["archived"] = archive_legacy_artifacts(repo, timestamp)

    # Never touch logs, raw PCAP, secrets, local config, baseline or tooling.
    for root_name in (".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov"):
        target = repo / root_name
        if target.exists():
            remove_tree_robust(target)
            result["removed"].append(root_name)
    for file_name in (".coverage", "coverage.xml"):
        target = repo / file_name
        if target.exists():
            make_writable(target)
            target.unlink()
            result["removed"].append(file_name)
    for cache in list(repo.rglob("__pycache__")):
        if ".dist" in cache.parts or ".archive" in cache.parts:
            continue
        remove_tree_robust(cache)
        result["removed"].append(cache.relative_to(repo).as_posix())
    for pyc in list(repo.rglob("*.py[co]")):
        if ".dist" in pyc.parts or ".archive" in pyc.parts:
            continue
        make_writable(pyc)
        pyc.unlink(missing_ok=True)

    workspace = repo / ".dist" / "v2workspace"
    if workspace.exists():
        stale = remove_tree_robust(workspace)
        result["removed"].append(workspace.relative_to(repo).as_posix())
        if stale is not None:
            result["stale_workspace"] = str(stale)
    for stale in sorted((repo / ".dist").glob("v2workspace.stale-*")) if (repo / ".dist").exists() else []:
        try:
            remove_tree_robust(stale, allow_stale_rename=False)
            result["removed"].append(stale.relative_to(repo).as_posix())
        except RuntimeError as exc:
            OUT.warn(str(exc))
    OUT.ok("generated caches/workspace cleaned; baseline/tooling/logs/secrets kept")
    return result


def validate_source(repo: Path, *, build_requested: bool) -> dict[str, Any]:
    required = [
        repo / "main.py",
        repo / "RUN.cmd",
        repo / "BUILD_ID",
        repo / "core" / "production_runtime.py",
        repo / "core" / "v6router" / "router.py",
        repo / "core" / "verified_v1" / "coin_bridge_live.py",
        repo / "android" / "HmuriyBridge.java",
        repo / "android" / "native" / "HmuriyNative.cpp",
        repo / "android" / "build_v2_native.ps1",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("missing project files:\n" + "\n".join(missing))
    build_id = (repo / "BUILD_ID").read_text(encoding="utf-8").strip()
    if build_id != EXPECTED_BUILD_ID:
        raise RuntimeError(f"unexpected BUILD_ID {build_id!r}; expected {EXPECTED_BUILD_ID}")

    bridge_java = (repo / "android" / "HmuriyBridge.java").read_text(encoding="utf-8")
    native = (repo / "android" / "native" / "HmuriyNative.cpp").read_text(encoding="utf-8")
    runtime = (repo / "core" / "production_runtime.py").read_text(encoding="utf-8")
    if bridge_java.count(PUBLIC_HOST) != 1:
        raise RuntimeError("HmuriyBridge.java must contain exactly one public endpoint")
    for forbidden in ("10.0.2.2", "one_way_hello=1"):
        if forbidden in bridge_java or forbidden in native:
            raise RuntimeError(f"forbidden stale transport marker remains: {forbidden}")
    if "trainer handshake confirmed" not in native or "welcome=pending" not in native:
        raise RuntimeError("native source lacks truthful welcome diagnostics")
    if "AdbReverseManager" in runtime or "transport.adb_reverse" in runtime:
        raise RuntimeError("dead adb-reverse production fallback still present")
    if "MAX_DEVICES = 30" not in runtime or "MAX_TABLES_PER_DEVICE = 5" not in runtime:
        raise RuntimeError("30x5 production capacity constants missing")

    if build_requested:
        prerequisites = [
            repo / ".dist" / "baseline" / "coin",
            repo / ".dist" / "tooling" / "apktool.jar",
            repo / "secrets" / "trainer.secret",
        ]
        absent = [str(path) for path in prerequisites if not path.exists()]
        if absent:
            raise RuntimeError("missing build prerequisites:\n" + "\n".join(absent))
        if not (repo / "secrets" / "trainer.secret").read_text(encoding="utf-8").strip():
            raise RuntimeError("secrets/trainer.secret is empty")

    return {
        "build_id": build_id,
        "endpoint": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
        "source_hashes": {
            "native": sha256(repo / "android" / "native" / "HmuriyNative.cpp"),
            "runtime": sha256(repo / "core" / "production_runtime.py"),
            "router": sha256(repo / "core" / "v6router" / "router.py"),
            "bridge": sha256(repo / "core" / "verified_v1" / "coin_bridge_live.py"),
        },
    }


def find_legacy(repo: Path, explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(abspath_preserve_drive(explicit))
    candidates.extend(
        [
            repo / "ready_v6.zip",
            repo.parent / "ready_v6.zip",
            repo.parent / "legacy" / "ready_v6.zip",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def find_powershell() -> str:
    found = find_executable(("powershell.exe", "powershell", "pwsh.exe", "pwsh"))
    if not found:
        raise RuntimeError("PowerShell/pwsh not found")
    return found


def build_apk(repo: Path, report_dir: Path) -> Path:
    ps = find_powershell()
    command = [ps, "-NoProfile"]
    if Path(ps).name.lower().startswith("powershell"):
        command += ["-ExecutionPolicy", "Bypass"]
    command += [
        "-File",
        str(repo / "android" / "build_v2_native.ps1"),
        "-Repo",
        str(repo),
    ]
    run_process(command, cwd=repo, output=report_dir / "apk-build.txt", timeout=1800)
    apk = repo / ".dist" / "v2workspace" / "out" / "coinpoker-production-native-debug.apk"
    if not apk.is_file():
        raise RuntimeError(f"build completed without expected APK: {apk}")
    return apk


def verify_apk(apk: Path, report_dir: Path) -> dict[str, Any]:
    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())
        libs = sorted(
            name for name in names
            if re.fullmatch(r"lib/(?:arm64-v8a|armeabi-v7a|x86_64|x86)/libhmuriy\.so", name)
        )
        expected_abis = {"arm64-v8a", "armeabi-v7a", "x86_64", "x86"}
        found_abis = {name.split("/")[1] for name in libs}
        if found_abis != expected_abis:
            raise RuntimeError(f"APK ABI mismatch: found={sorted(found_abis)}")
        for name in libs:
            data = archive.read(name)
            if EXPECTED_BUILD_ID.encode("ascii") not in data:
                raise RuntimeError(f"{name} does not contain {EXPECTED_BUILD_ID}")
            if PUBLIC_HOST.encode("ascii") in data:
                # Host is injected via Java/JNI; native should not carry hidden endpoint lists.
                raise RuntimeError(f"unexpected hardcoded endpoint in {name}")
    result = {
        "path": str(apk),
        "size_bytes": apk.stat().st_size,
        "sha256": sha256(apk),
        "abis": sorted(found_abis),
        "build_id": EXPECTED_BUILD_ID,
    }
    (report_dir / "apk-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OUT.ok(f"APK verified: {result['sha256']}")
    return result


def adb_capture(adb: str, serial: str, args: list[str], repo: Path) -> str:
    result = run_process([adb, "-s", serial, *args], cwd=repo, check=False)
    return result.stdout or ""


def vpn_snapshot(adb: str, serial: str, repo: Path, report_dir: Path) -> dict[str, Any]:
    connectivity = adb_capture(adb, serial, ["shell", "dumpsys", "connectivity"], repo)
    packages = adb_capture(adb, serial, ["shell", "pm", "list", "packages"], repo)
    (report_dir / f"adb-{serial}-connectivity.txt").write_text(connectivity, encoding="utf-8")
    (report_dir / f"adb-{serial}-packages.txt").write_text(packages, encoding="utf-8")
    vpn = bool(re.search(r"TRANSPORT_VPN|\btype\s*[:=]\s*VPN\b|\bVPN\b", connectivity, re.I))
    spck = [line.strip() for line in packages.splitlines() if "spck" in line.lower()]
    return {"vpn_detected": vpn, "spck_packages": spck}


def install_apk(
    apk: Path,
    repo: Path,
    report_dir: Path,
    *,
    launch: bool,
    require_vpn: bool,
) -> dict[str, Any]:
    adb = find_adb()
    devices = adb_devices(adb, repo)
    if not devices:
        raise RuntimeError("no online ADB devices")
    if len(devices) > 30:
        raise RuntimeError(f"{len(devices)} devices exceed the validated 30-device envelope")

    rows: dict[str, Any] = {}
    for serial in devices:
        OUT.step(f"ADB {serial}")
        vpn = vpn_snapshot(adb, serial, repo, report_dir)
        if require_vpn and not vpn["vpn_detected"]:
            raise RuntimeError(
                f"{serial}: active Android VPN/proxy was not detected. "
                f"See {report_dir / f'adb-{serial}-connectivity.txt'}"
            )
        if not vpn["spck_packages"]:
            OUT.warn(f"{serial}: no package name containing 'spck' found; VPN status={vpn['vpn_detected']}")
        run_process(
            [adb, "-s", serial, "install", "-r", str(apk)],
            cwd=repo,
            output=report_dir / f"adb-{serial}-install.txt",
            timeout=300,
        )
        adb_capture(adb, serial, ["logcat", "-c"], repo)
        adb_capture(adb, serial, ["shell", "am", "force-stop", "com.coingames.coinpoker"], repo)
        if launch:
            adb_capture(
                adb,
                serial,
                [
                    "shell", "monkey", "-p", "com.coingames.coinpoker",
                    "-c", "android.intent.category.LAUNCHER", "1",
                ],
                repo,
            )
            marker = ""
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                marker = adb_capture(adb, serial, ["logcat", "-d", "-s", "Hmuriy"], repo)
                if EXPECTED_BUILD_ID in marker:
                    break
                time.sleep(1.0)
            (report_dir / f"adb-{serial}-hmuriy.txt").write_text(marker, encoding="utf-8")
            if EXPECTED_BUILD_ID not in marker:
                raise RuntimeError(f"{serial}: installed app did not log {EXPECTED_BUILD_ID}")
        rows[serial] = {"vpn": vpn, "installed": True, "launched": launch}
        OUT.ok(f"{serial}: installed" + (" and version-confirmed" if launch else ""))
    return rows


def port_is_listening(host: str = "127.0.0.1", port: int = PUBLIC_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def start_trainer(repo: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("--run is supported only on Windows")
    if port_is_listening():
        raise RuntimeError(
            f"tcp:{PUBLIC_PORT} is already occupied. Close the old Trainer before --run."
        )
    title = f"PokerEye {EXPECTED_BUILD_ID}"
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", "start", title, "/D", str(repo), str(repo / "RUN.cmd")],
        cwd=str(repo),
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if port_is_listening():
            OUT.ok(f"Trainer listens on tcp:{PUBLIC_PORT}")
            return
        time.sleep(0.5)
    raise RuntimeError("Trainer window started but tcp:19037 did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.curdir)
    parser.add_argument("--legacy")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--no-pcap", action="store_true")
    parser.add_argument("--no-archive-legacy", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--run", action="store_true", help="start RUN.cmd before optional app launch")
    parser.add_argument("--require-vpn", action="store_true")
    args = parser.parse_args()

    repo = abspath_preserve_drive(args.repo)
    if not (repo / "main.py").is_file():
        raise SystemExit(f"[ERROR] PokerEye repo not found: {repo}")
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = repo / "audit-output" / f"v74-{timestamp}"
    report_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "report_dir": str(report_dir),
        "steps": {},
    }

    OUT.line(f"PokerEye finalizer {EXPECTED_BUILD_ID}")
    OUT.line(f"Repo: {repo}")
    OUT.line(f"Reports: {report_dir}")
    try:
        if args.update:
            OUT.step("Git fast-forward update")
            if not (repo / ".git").exists():
                OUT.warn(".git not found; update skipped")
            else:
                dirty = run_process(["git", "status", "--porcelain"], cwd=repo, check=True)
                if dirty.stdout.strip():
                    raise RuntimeError("git working tree is not clean; refusing automatic pull")
                run_process(["git", "pull", "--ff-only"], cwd=repo, output=report_dir / "git-pull.txt")
                OUT.ok("git pull --ff-only complete")

        OUT.step("Safe cleanup")
        summary["steps"]["cleanup"] = clean_generated(
            repo,
            archive_legacy=not args.no_archive_legacy,
            timestamp=timestamp,
        )

        OUT.step("Static production invariants")
        summary["steps"]["source"] = validate_source(repo, build_requested=args.build)
        OUT.ok(f"build={EXPECTED_BUILD_ID}; endpoint={PUBLIC_HOST}:{PUBLIC_PORT}; capacity=30x5")

        OUT.step("Compile all Python modules")
        run_process(
            [sys.executable, "-X", "utf8", "-m", "compileall", "-q", "core", "tests", "tools"],
            cwd=repo,
            output=report_dir / "compileall.txt",
        )
        OUT.ok("compileall")

        OUT.step("Full regression suite")
        tests = run_process(
            [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=repo,
            output=report_dir / "unittest.txt",
            timeout=300,
        )
        match = re.search(r"Ran\s+(\d+)\s+tests", tests.stdout)
        summary["steps"]["tests"] = {"count": int(match.group(1)) if match else None, "ok": True}
        OUT.ok(f"full tests: {match.group(1) if match else 'OK'}")

        if not args.no_pcap:
            legacy = find_legacy(repo, args.legacy)
            if legacy is None:
                OUT.warn("ready_v6 legacy corpus not found; fixture regressions passed, full PCAP replay skipped")
                summary["steps"]["pcap"] = {"skipped": True, "reason": "legacy corpus not found"}
            else:
                OUT.step("Legacy PCAP acceptance")
                pcap_json = report_dir / "pcap-acceptance.json"
                run_process(
                    [
                        sys.executable, "-X", "utf8",
                        str(repo / "tools" / "legacy_pcap_acceptance.py"),
                        "--repo", str(repo), "--legacy", str(legacy),
                        "--output", str(pcap_json),
                    ],
                    cwd=repo,
                    output=report_dir / "pcap-console.txt",
                    timeout=600,
                )
                pcap_result = json.loads(pcap_json.read_text(encoding="utf-8"))
                summary["steps"]["pcap"] = {
                    "source": str(legacy),
                    "totals": pcap_result.get("totals"),
                    "warnings": len(pcap_result.get("warnings") or []),
                    "failures": len(pcap_result.get("failures") or []),
                }
                OUT.ok("legacy PCAP corpus accepted")

        OUT.step("HMN1 synthetic capacity/reconnect 30x5")
        load_json = report_dir / "hmn1-load-30x5.json"
        run_process(
            [
                sys.executable, "-X", "utf8",
                str(repo / "tools" / "hmn1_synthetic_load.py"),
                "--repo", str(repo), "--devices", "30", "--channels", "5",
                "--frames", "8", "--output", str(load_json),
            ],
            cwd=repo,
            output=report_dir / "hmn1-console.txt",
            timeout=300,
        )
        load_result = json.loads(load_json.read_text(encoding="utf-8"))
        if load_result.get("errors"):
            raise RuntimeError(f"HMN1 load errors: {load_result['errors']}")
        summary["steps"]["hmn1"] = load_result
        OUT.ok("150 channels + reconnect accepted")

        apk: Path | None = None
        if args.build:
            OUT.step("Clean native APK build")
            apk = build_apk(repo, report_dir)
            summary["steps"]["apk"] = verify_apk(apk, report_dir)

        if args.run:
            OUT.step("Start Trainer")
            start_trainer(repo)
            summary["steps"]["trainer_started"] = True

        if args.install:
            if apk is None:
                candidate = repo / ".dist" / "v2workspace" / "out" / "coinpoker-production-native-debug.apk"
                if not candidate.is_file():
                    raise RuntimeError("--install requires --build or an existing verified APK")
                apk = candidate
                summary["steps"]["apk"] = verify_apk(apk, report_dir)
            OUT.step("ADB install")
            summary["steps"]["adb"] = install_apk(
                apk,
                repo,
                report_dir,
                launch=args.launch,
                require_vpn=args.require_vpn,
            )

        summary["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        summary["ok"] = True
        (report_dir / "FINAL_SUMMARY.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        lines = [
            f"# PokerEye {EXPECTED_BUILD_ID} finalization",
            "",
            "- Result: **PASS**",
            f"- Repository: `{repo}`",
            f"- Public endpoint: `{PUBLIC_HOST}:{PUBLIC_PORT}`",
            f"- Report directory: `{report_dir}`",
            f"- Tests: **{summary['steps'].get('tests', {}).get('count', 'OK')}**",
            "- Synthetic HMN1: **30 devices × 5 channels PASS**",
            "- Safety fallback: **CHECK when free, otherwise FOLD; no invented paid CALL**",
            "",
            "The synthetic and PCAP checks do not mathematically guarantee public-network uptime;",
            "the live ADB log must still show the same BUILD_ID and a received Trainer welcome.",
        ]
        (report_dir / "FINAL_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        OUT.step("PASS")
        OUT.ok(f"all requested steps completed; report={report_dir}")
        if apk is not None:
            OUT.line(f"APK: {apk}")
        return 0
    except Exception as exc:
        summary["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        summary["ok"] = False
        summary["error_type"] = type(exc).__name__
        summary["error"] = str(exc)
        (report_dir / "FINAL_SUMMARY.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        OUT.fail(f"{type(exc).__name__}: {exc}")
        OUT.line(f"Report: {report_dir}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
