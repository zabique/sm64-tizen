# SM64 for Samsung Tizen TVs (WebAssembly)

Build Super Mario 64 as a WebAssembly app for a Samsung Tizen TV, from **your own** ROM.
The game runs in the TV's own web engine as a normal app in the Apps list: no root, no
Android box, no streaming, no network needed once it is installed. An Xbox (or other) gamepad
works through the browser Gamepad API, and saves are kept on the TV.

This repository contains **no game code, assets or ROM data**, only build scripts, a small
patch, page code and an original icon. `build.py` downloads
[sm64-port](https://github.com/sm64-port/sm64-port) and builds it with the assets extracted
from the ROM you supply. See [Legal](#legal).

## What you need

| | |
| :--- | :--- |
| ROM | your own dump of **Super Mario 64 (USA)**, SHA-1 `9bef1128717f958171a4afac3ed78ee2bb4e86ce` (`.z64`, `.v64` and `.n64` all work) |
| PC | Windows 10/11 or Linux, Python 3.8+, git, about 3 GB free disk |
| Linux only | `make`, `gcc`, `g++` (e.g. `sudo apt install build-essential`) |
| TV | Samsung Tizen TV with Developer Mode (tested: QN85A, Tizen 6.0) |
| Signing | Tizen test certificates (see [Certificates](#certificates)) and `pip install cryptography` |
| Installing | `sdb` from [Tizen Studio](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/installing-tv-sdk.html) |

On Windows nothing else needs installing: `build.py` downloads the Emscripten SDK and
[w64devkit](https://github.com/skeeto/w64devkit) (make + gcc) into `toolchain/`.

## Build

```
cd C:\
git clone https://github.com/zabique/sm64-tizen
cd sm64-tizen
python build.py --rom path/to/your/sm64.z64 --cert-dir path/to/certs
```

Clone into a short folder path without spaces (such as `C:\sm64-tizen`): make cannot handle
spaces, and on Windows a deep folder breaks the 260-character path limit inside the compiler.

The first run downloads the toolchain (about 1 GB) and extracts the assets from the ROM; it
takes 10-20 minutes. Later runs only rebuild what changed. The output is:

- `dist/web/index.html`: the game as one self-contained page (try it in a desktop browser)
- `dist/sm64-wasm.wgt`: the signed TV app (only with `--cert-dir`)

`--name "My name"` sets the app name in the TV's Apps list (default `SM64`).

## Install on the TV

1. On the TV, open **Apps**, press **1 2 3 4 5** on the remote, switch **Developer Mode** on and
   set **Host PC IP** to your PC's LAN address.
2. Restart the TV (hold Power until it reboots).
3. Find the TV's IP in Settings → General → Network → Network Status → IP Settings.
4. Run:

   ```
   python install.py --tv <TV_IP>
   ```

   It uploads and installs the package and starts the game. Pass `--sdb path/to/sdb` if
   Tizen Studio is not in its default location.

## Certificates

Tizen TVs only install signed packages. In Developer Mode a TV accepts packages signed with
the **Tizen test certificates** that come with Tizen Studio. `--cert-dir` is a folder with:

- `author.p12`: an author certificate, created in Tizen Studio's Certificate Manager
  (choose the **Tizen** profile, not Samsung)
- `distributor.p12`: Tizen Studio's public distributor certificate,
  `tizen-studio/tools/certificate-generator/certificates/distributor/tizen-distributor-signer.p12`
- `password.txt`: two lines, the author certificate's password, then the distributor's
  (the public one's is `tizenpkcs12passfordsigner`); one line if both use the same password

A Samsung certificate made for your TV in Certificate Manager (Samsung profile) works too.
Unsigned packages fail with `Check certificate error [-12]`. Certificates are git-ignored; never
commit them.

## Playing

- **Controller:** plug in or pair a gamepad (an Xbox Wireless Controller is tested). In a
  desktop browser the keyboard works too: WASD moves, arrow keys are the C buttons, L = A,
  `,` = B, K = Z, Right Shift = R, Space = Start.
- **Saves:** the normal in-game save slots, kept in the app's local storage on the TV. They
  survive restarts and app updates and are lost if the app is uninstalled.
- **Leaving:** press the remote's Home or Exit key.

### Page options

Add these to the page URL (`dist/web/index.html?meter=1`) or bake them into the TV app with
`python build.py ... --query "meter=1&fps=30"`:

| Option | Effect |
| :--- | :--- |
| `meter=1` | show an FPS meter |
| `fps=30` | cap the frame rate (lower CPU use; on a QN85A it felt worse than uncapped) |
| `aw=0` | don't use an AudioWorklet for sound (falls back to a large ScriptProcessor) |
| `ab=<frames>` | ScriptProcessor size of that fallback (default 4096; `ab=0` = SDL's own audio) |
| `lead=<frames>` | how far the audio runs ahead of the audio clock (default 4096) |
| `padfix=0` | turn off the duplicate-gamepad fix |
| `log=<url>` | send audio/input stats to `<url>/log?...` once a second (debugging) |

## How it works

- **Game:** upstream sm64-port's `TARGET_WEB` build with Emscripten 3.1.74.
  `patches/sm64-port-web.patch` brings that old target up to date:
  - build: flags for current Emscripten (memory growth, SDL2 port, `em++`, no `-march=native`),
    a response file for the link (Windows command-line limit), MinGW fixes for the host tools;
  - `emscripten_request_animation_frame` in place of the removed `dynCall`;
  - GLSL ES (`#version 100`) shaders, which WebGL requires;
  - no timer frame pacing on the web: it busy-waited in `SDL_Delay` on the browser's main thread
    every frame (frames are already paced by `requestAnimationFrame`);
  - an out-of-bounds loop over the keyboard scancode tables that newer compilers turn into an
    endless loop at startup.
- **One page, no network:** a TV app runs from `file://`, where the page cannot `fetch()` the
  `.wasm`. `build.py` inlines it as base64 and hands it to Emscripten through
  `Module.wasmBinary`.
- **Audio stutter fix (`web/audio_fix.js`):** SDL2's web audio refills a 512-frame
  ScriptProcessor on the main thread, about 86 times a second at 44.1 kHz. On a TV a game frame
  blocks the main thread for 25 ms or more, so refills arrive late and the sound stutters. The
  fix gives SDL a fake node, pulls its audio in step with the audio clock, and plays it from an
  AudioWorklet on the audio thread, or from a large ScriptProcessor as a fallback.
- **Gamepad fix (`web/pad_fix.js`):** after a controller reconnects, the TV can list a dead copy
  of it as pad 0, and the game only reads pad 0. The fix puts the pad that last changed first.
- **Optional FPS cap (`web/fps_cap.js`):** see `fps=30` above.

## Troubleshooting

- **`ROM SHA-1 is ..., expected ...`:** only the USA ROM is supported, unmodified.
- **`Check certificate error`:** Developer Mode is off, the TV was not restarted after turning
  it on, or the package is unsigned.
- **`sdb connect` fails:** Host PC IP on the TV must be this PC's address; a VPN can also block
  LAN traffic. The TV accepts one developer connection at a time (close Tizen Studio).
- **Desktop browser:** opening `dist/web/index.html` directly works; desktop Chrome then uses
  the ScriptProcessor audio fallback. `python -m http.server -d dist/web` and
  `http://localhost:8000/` gives it the AudioWorklet path.
- **Rebuild from scratch:** delete `work/` (and `toolchain/` to download the tools again).

## Legal

This project is unofficial and not affiliated with or endorsed by Nintendo or Samsung.
"Super Mario 64" is a trademark of Nintendo. You need your own legally obtained copy of the
game. The ROM and everything built from it (`work/`, `dist/`, the `.wgt`) contain Nintendo's
copyrighted assets: keep them for your own use and do not share or upload them. The code in
this repository is MIT-licensed (see `LICENSE` and `NOTICE`).

## Credits

[sm64-port](https://github.com/sm64-port/sm64-port) and the
[SM64 decompilation](https://github.com/n64decomp/sm64) teams,
[Emscripten](https://emscripten.org), [w64devkit](https://github.com/skeeto/w64devkit).
