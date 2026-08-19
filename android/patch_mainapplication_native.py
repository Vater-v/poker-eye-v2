#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path

SIGNATURE = ".method public onCreate()V"

REPLACEMENT = r""".method public onCreate()V
    .locals 3

    .line 44
    invoke-super {p0}, Landroid/app/Application;->onCreate()V

    :try_start_hmuriy
    const-string v0, "Hmuriy"

    const-string v1, "[+] application bootstrap reached"

    invoke-static {v0, v1}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    invoke-static {}, Lcom/hmuriy/HmuriyBridge;->bootstrap()V
    :try_end_hmuriy
    .catch Ljava/lang/Throwable; {:try_start_hmuriy .. :try_end_hmuriy} :catch_hmuriy

    :hmuriy_done
    .line 45
    move-object v0, p0

    check-cast v0, Landroid/content/Context;

    sget-object v1, Lcom/facebook/react/soloader/OpenSourceMergedSoMapping;->INSTANCE:Lcom/facebook/react/soloader/OpenSourceMergedSoMapping;

    check-cast v1, Lcom/facebook/soloader/ExternalSoMapping;

    invoke-static {v0, v1}, Lcom/facebook/soloader/SoLoader;->init(Landroid/content/Context;Lcom/facebook/soloader/ExternalSoMapping;)V

    .line 50
    move-object v0, p0

    check-cast v0, Landroid/app/Application;

    invoke-static {v0}, Lexpo/modules/ApplicationLifecycleDispatcher;->onApplicationCreate(Landroid/app/Application;)V

    return-void

    :catch_hmuriy
    move-exception v0

    const-string v1, "Hmuriy"

    const-string v2, "[!] application bootstrap invocation failed; Coin stays fail-open"

    invoke-static {v1, v2, v0}, Landroid/util/Log;->w(Ljava/lang/String;Ljava/lang/String;Ljava/lang/Throwable;)I

    goto :hmuriy_done
.end method"""


def replace_method(source: str) -> str:
    start = source.find(SIGNATURE)
    if start < 0:
        raise RuntimeError("MainApplication.onCreate not found")
    end = source.find(".end method", start)
    if end < 0:
        raise RuntimeError("MainApplication.onCreate unterminated")
    end += len(".end method")
    return source[:start] + REPLACEMENT + source[end:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("smali", type=Path)
    args = ap.parse_args()
    path = args.smali
    source = path.read_text(encoding="utf-8")
    if "application bootstrap reached" in source:
        print("[+] MainApplication Hmuriy bootstrap already present")
        return
    source = replace_method(source)
    if source.count("HmuriyBridge;->bootstrap()V") != 1:
        raise RuntimeError("expected exactly one Hmuriy bootstrap call")
    path.write_text(source, encoding="utf-8", newline="\n")
    print("[+] MainApplication patched: eager Hmuriy bootstrap with Throwable fail-open")


if __name__ == "__main__":
    main()
