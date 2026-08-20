#!/usr/bin/env python3
import os
import shutil
import sys
import zipfile

if len(sys.argv) != 4:
    raise SystemExit("usage: inject_classes_dex.py INPUT.apk classes.dex OUTPUT.apk")
source, dex_path, output = sys.argv[1:]
if not os.path.isfile(source) or not os.path.isfile(dex_path):
    raise SystemExit("input APK or classes.dex does not exist")
if os.path.abspath(source) == os.path.abspath(output):
    raise SystemExit("output must differ from input")

with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(output, "w") as zout:
    for info in zin.infolist():
        if info.filename == "classes8.dex":
            continue
        data = zin.read(info.filename)
        zout.writestr(info, data)
    with open(dex_path, "rb") as handle:
        zout.writestr("classes8.dex", handle.read(), compress_type=zipfile.ZIP_STORED)
print(output)
