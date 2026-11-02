#!/usr/bin/env python3
"""Install the built package on a Samsung TV in Developer Mode, then start it.

  python install.py --tv 192.168.1.50 [--wgt dist/sm64-wasm.wgt] [--sdb path/to/sdb]

TV side, once: open Apps, press 1 2 3 4 5 on the remote, switch Developer Mode ON, set
Host PC IP to this PC's LAN address, then restart the TV (hold Power until it reboots).
sdb comes with Tizen Studio (tizen-studio/tools/sdb)."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ID, APP_ID = "SM64Tizen0", "SM64Tizen0.SM64"
REMOTE_DIR = "/home/owner/share/tmp/sdk_tools/tmp"   # where Tizen Studio uploads packages too


def find_sdb(given):
    cands = [given] if given else []
    cands.append(shutil.which("sdb"))
    exe = "sdb.exe" if os.name == "nt" else "sdb"
    for root in (Path.home() / "tizen-studio", Path("C:/tizen-studio"), Path("C:/tizen-studio-data")):
        cands.append(str(root / "tools" / exe))
    for c in cands:
        if c and Path(c).is_file():
            return c
    sys.exit("sdb not found: install Tizen Studio or pass --sdb path/to/sdb")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tv", required=True, help="TV IP address")
    ap.add_argument("--wgt", default=str(HERE / "dist" / "sm64-wasm.wgt"))
    ap.add_argument("--sdb")
    a = ap.parse_args()
    wgt = Path(a.wgt)
    if not wgt.is_file():
        sys.exit("%s not found - run build.py with --cert-dir first" % wgt)
    sdb = find_sdb(a.sdb)
    target = a.tv if ":" in a.tv else a.tv + ":26101"

    def sdb_run(*args, timeout=180):
        r = subprocess.run([sdb] + list(args), capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        print(out)
        return out

    print("connecting to", target)
    if "connected" not in sdb_run("connect", target):
        sys.exit("could not connect: is Developer Mode on, with Host PC IP set to this PC?")
    try:
        remote = REMOTE_DIR + "/" + wgt.name
        print("uploading %s (%.1f MB)" % (wgt.name, wgt.stat().st_size / 1048576.0))
        if "pushed" not in sdb_run("-s", target, "push", str(wgt), remote, timeout=600):
            sys.exit("upload failed")
        out = sdb_run("-s", target, "shell", "0", "vd_appinstall", PACKAGE_ID, remote)
        if "install completed" not in out:
            sys.exit("install failed (\"Check certificate error\" = Developer Mode off or TV not restarted)")
        sdb_run("-s", target, "shell", "0", "was_execute", APP_ID)
        print("\ninstalled; the app is in the TV's Apps list")
    finally:
        sdb_run("disconnect", target)


if __name__ == "__main__":
    main()
