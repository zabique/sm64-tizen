#!/usr/bin/env python3
"""Build Super Mario 64 (WebAssembly) for Samsung Tizen TVs from YOUR OWN ROM.

  python build.py --rom path/to/baserom.us.z64                       # -> dist/web/
  python build.py --rom path/to/baserom.us.z64 --cert-dir path/to/certs   # + dist/sm64-wasm.wgt

What it does:
  1. checks the ROM (Super Mario 64, USA; .z64/.v64/.n64 byte orders are all accepted)
  2. downloads the toolchain into ./toolchain (Emscripten SDK; on Windows also w64devkit for make/gcc)
  3. clones sm64-port at a pinned commit into ./work and applies patches/sm64-port-web.patch
  4. builds the WebAssembly game (the game assets are extracted from your ROM at this step)
  5. writes a self-contained page to dist/web/ and, with --cert-dir, a signed Tizen package

Nothing from the ROM is part of this repository; everything derived from it stays in
./work and ./dist, which are git-ignored. Do not share those folders.
"""
import argparse
import base64
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
TOOLCHAIN = HERE / "toolchain"
DIST = HERE / "dist"
WIN = os.name == "nt"

SM64_PORT_URL = "https://github.com/sm64-port/sm64-port.git"
SM64_PORT_COMMIT = "2b17d081c9798b31b91dc71f37994b0da28cffc9"
EMSDK_URL = "https://github.com/emscripten-core/emsdk.git"
EMSDK_VERSION = "3.1.74"
W64DEVKIT_URL = "https://github.com/skeeto/w64devkit/releases/download/v1.20.0/w64devkit-1.20.0.zip"
W64DEVKIT_SHA256 = "4d3d364f40d16b4634b741d137f2c9ebd66361c7930f5fe5a59dc3730a0faeb4"
ROM_SHA1_US = "9bef1128717f958171a4afac3ed78ee2bb4e86ce"

SM64 = WORK / "sm64-port"
EMSDK = TOOLCHAIN / "emsdk"
W64DEVKIT = TOOLCHAIN / "w64devkit"
GAME_JS = "sm64.us.js"
GAME_WASM = "sm64.us.wasm"


def step(msg):
    print("\n== " + msg, flush=True)


def run(cmd, **kw):
    print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def git(*args, cwd=None):
    # safe.directory: some drives (exFAT, network shares) do not record file ownership
    run(["git", "-c", "safe.directory=*"] + list(args), cwd=cwd)


# ---------------------------------------------------------------------------------------- ROM
def load_rom(path):
    data = Path(path).read_bytes()
    head = data[:4]
    if head == b"\x37\x80\x40\x12":    # .v64: byte-swapped 16-bit words
        b = bytearray(data)
        b[0::2], b[1::2] = data[1::2], data[0::2]
        data = bytes(b)
    elif head == b"\x40\x12\x37\x80":  # .n64: little-endian 32-bit words
        b = bytearray(len(data))
        for i in range(4):
            b[i::4] = data[3 - i::4]
        data = bytes(b)
    elif head != b"\x80\x37\x12\x40":
        sys.exit("%s is not an N64 ROM" % path)
    sha1 = hashlib.sha1(data).hexdigest()
    if sha1 != ROM_SHA1_US:
        sys.exit("ROM SHA-1 is %s, expected %s (Super Mario 64, USA).\n"
                 "Only the USA version is supported." % (sha1, ROM_SHA1_US))
    return data


# ---------------------------------------------------------------------------------- toolchain
def download(url, dest):
    print("   downloading " + url, flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.replace(dest)


def setup_w64devkit():
    if (W64DEVKIT / "bin" / "make.exe").exists():
        return
    step("w64devkit (make + gcc for the host tools)")
    TOOLCHAIN.mkdir(exist_ok=True)
    z = TOOLCHAIN / "w64devkit.zip"
    if not z.exists():
        download(W64DEVKIT_URL, z)
    digest = hashlib.sha256(z.read_bytes()).hexdigest()
    if digest != W64DEVKIT_SHA256:
        z.unlink()
        sys.exit("w64devkit.zip checksum mismatch (%s); deleted, run again" % digest)
    with zipfile.ZipFile(z) as f:
        f.extractall(TOOLCHAIN)
    z.unlink()


def setup_emsdk():
    if (EMSDK / "upstream" / "emscripten").is_dir() and (EMSDK / ".emscripten").exists():
        return
    step("Emscripten SDK " + EMSDK_VERSION)
    TOOLCHAIN.mkdir(exist_ok=True)
    if not EMSDK.is_dir():
        git("clone", "--depth", "1", EMSDK_URL, EMSDK)
    tool = EMSDK / ("emsdk.bat" if WIN else "emsdk")
    run([tool, "install", EMSDK_VERSION], cwd=EMSDK)
    run([tool, "activate", EMSDK_VERSION], cwd=EMSDK)


PYSHIM = TOOLCHAIN / "python"


def setup_python3():
    """sm64-port runs 'python3' by name (Makefile and extract_assets.py). Windows Python has no
    python3.exe, so make one: a small venv whose launcher is copied as python3.exe."""
    if not WIN:
        return
    exe = PYSHIM / "Scripts" / "python3.exe"
    if exe.exists():
        return
    step("python3 for the build scripts")
    run([sys.executable, "-m", "venv", "--without-pip", PYSHIM])
    shutil.copy2(PYSHIM / "Scripts" / "python.exe", exe)


def python_for_make():
    if WIN:
        return (PYSHIM / "Scripts" / "python3.exe").as_posix()
    return shutil.which("python3") or sys.executable


def build_env():
    env = os.environ.copy()
    paths = [str(EMSDK / "upstream" / "emscripten")]
    if WIN:
        paths[:0] = [str(PYSHIM / "Scripts"), str(W64DEVKIT / "bin")]
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    env["EMSDK"] = str(EMSDK)
    env["EM_CONFIG"] = str(EMSDK / ".emscripten")
    return env


# ----------------------------------------------------------------------------------- the game
def setup_source(rom):
    if not (SM64 / ".git").is_dir():
        step("sm64-port @ " + SM64_PORT_COMMIT[:7])
        WORK.mkdir(exist_ok=True)
        git("clone", "--no-checkout", SM64_PORT_URL, SM64)
        # the patch has LF line endings; keep the checkout LF on Windows too
        git("config", "core.autocrlf", "false", cwd=SM64)
        git("config", "core.eol", "lf", cwd=SM64)
        git("checkout", "-q", SM64_PORT_COMMIT, cwd=SM64)
    # the patch must be LF like the checkout (a ZIP download or core.autocrlf can make it CRLF)
    patch = WORK / "sm64-port-web.patch"
    patch.write_bytes((HERE / "patches" / "sm64-port-web.patch").read_bytes().replace(bytes((13, 10)), bytes((10,))))
    applied = subprocess.run(["git", "-c", "safe.directory=*", "apply", "--reverse", "--check", str(patch)],
                             cwd=SM64, capture_output=True).returncode == 0
    if not applied:
        step("applying " + patch.name)
        git("apply", patch, cwd=SM64)
    target = SM64 / "baserom.us.z64"
    if not target.exists() or target.read_bytes() != rom:
        target.write_bytes(rom)


def make(jobs):
    step("building (asset extraction from the ROM takes a few minutes the first time)")
    cmd = [W64DEVKIT / "bin" / "make.exe" if WIN else "make", "-j%d" % jobs,
           "VERSION=us", "TARGET_WEB=1", "PYTHON=" + python_for_make()]
    run(cmd, cwd=SM64, env=build_env())
    out = SM64 / "build" / "us_web"
    for f in (GAME_JS, GAME_WASM):
        if not (out / f).exists():
            sys.exit("build finished but %s is missing" % (out / f))
    return out


def assemble_page(out):
    """One page that needs no network: the wasm is inlined (file:// pages cannot fetch() it)."""
    step("assembling dist/web")
    web = HERE / "web"
    html = (web / "shell.html").read_text(encoding="utf-8")
    # these patch browser APIs (AudioContext, requestAnimationFrame, getGamepads) before the
    # game uses them, so they go first
    fixes = "".join("<script>\n%s</script>\n" % (web / f).read_text(encoding="utf-8")
                    for f in ("audio_fix.js", "fps_cap.js", "pad_fix.js"))
    wasm = base64.b64encode((out / GAME_WASM).read_bytes()).decode("ascii")
    loader = ('<script>window.__SM64_WASM_B64="%s";</script>\n'
              "<script>\n"
              "(function () {\n"
              "  var bin = atob(window.__SM64_WASM_B64), bytes = new Uint8Array(bin.length);\n"
              "  window.__SM64_WASM_B64 = null;\n"
              "  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);\n"
              "  Module.wasmBinary = bytes.buffer;\n"
              "  var s = document.createElement('script');\n"
              "  s.src = '%s';\n"
              "  document.body.appendChild(s);\n"
              "})();\n"
              "</script>\n") % (wasm, GAME_JS)
    html = html.replace("<!--FIXES-->", fixes, 1).replace("<!--LOADER-->", loader, 1)
    dest = DIST / "web"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(html, encoding="utf-8", newline="\n")
    shutil.copy2(out / GAME_JS, dest / GAME_JS)
    print("   %s (%.1f MB)" % (dest / "index.html", (dest / "index.html").stat().st_size / 1048576.0))
    return dest


def package(page_dir, cert_dir, name, query):
    step("packaging + signing the Tizen app")
    sys.path.insert(0, str(HERE / "tools"))
    from tizen_signer import sign_widget
    tz = HERE / "tizen"
    files = {
        "config.xml": (tz / "config.xml").read_text(encoding="utf-8").replace("@NAME@", name).encode(),
        "index.html": (tz / "index.html").read_text(encoding="utf-8")
                      .replace("@QUERY@", ("?" + query) if query else "").encode(),
        "icon.png": (tz / "icon.png").read_bytes(),
        "wasm/index.html": (page_dir / "index.html").read_bytes(),
        "wasm/" + GAME_JS: (page_dir / GAME_JS).read_bytes(),
    }
    files.update(sign_widget(files, str(cert_dir)))
    wgt = DIST / "sm64-wasm.wgt"
    with zipfile.ZipFile(wgt, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n, data in files.items():
            z.writestr(n, data)
    print("   %s (%.1f MB)" % (wgt, wgt.stat().st_size / 1048576.0))
    return wgt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", required=True, help="your Super Mario 64 (USA) ROM")
    ap.add_argument("--cert-dir", help="folder with author.p12, distributor.p12, password.txt (see README)")
    ap.add_argument("--name", default="SM64", help="app name on the TV (default: SM64)")
    ap.add_argument("--query", default="", help="page options baked into the TV app, e.g. 'meter=1'")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()

    if a.cert_dir:
        for f in ("author.p12", "distributor.p12", "password.txt"):
            if not (Path(a.cert_dir) / f).exists():
                sys.exit("--cert-dir: %s is missing" % (Path(a.cert_dir) / f))
        try:
            import cryptography  # noqa: F401
        except ImportError:
            sys.exit("signing needs the 'cryptography' package: python -m pip install cryptography")
    for tool in ("git",) + (() if WIN else ("make", "gcc", "g++")):
        if not shutil.which(tool):
            sys.exit("'%s' was not found on PATH" % tool)

    if WIN and len(str(HERE)) > 100:
        # gcc reaches its own headers through bin/../lib/gcc/.../../../../include paths that
        # break the Windows 260-character path limit when the folder is deep
        sys.exit("The folder path is too long for the Windows toolchain (%d characters, max 100).\n"
                 "Move the repository to a shorter path such as C:\\sm64-tizen" % len(str(HERE)))
    if " " in str(HERE):
        sys.exit("The folder path contains a space, which make cannot handle (%s).\n"
                 "Move the repository to a path without spaces such as C:\\sm64-tizen" % HERE)
    rom = load_rom(a.rom)
    print("ROM ok (Super Mario 64, USA)")
    if WIN:
        setup_w64devkit()
        setup_python3()
    setup_emsdk()
    setup_source(rom)
    out = make(a.jobs)
    page = assemble_page(out)
    if a.cert_dir:
        wgt = package(page, a.cert_dir, a.name, a.query)
        print("\nDone. Install on the TV:  python install.py --tv <TV_IP> --wgt %s" % wgt.relative_to(HERE))
    else:
        print("\nDone. Open dist/web/index.html in a browser to try it.\n"
              "For the TV package, run again with --cert-dir (see README).")


if __name__ == "__main__":
    main()
