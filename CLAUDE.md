# rpi5-case — Raspberry Pi 5 window camera mount

CAD in SolidCraft (MCP server `solidcraft`, app on 127.0.0.1:7878), printing on a **Prusa CORE One 0.4** via PrusaSlicer
(flatpak `com.prusa3d.PrusaSlicer`, 2.9.x). Public repo: design CC BY-SA 4.0, `tools/` MIT.

- User-facing docs are the top-level `README.md` (incl. how to run the MCP servers); keep it in sync when parts,
  hardware or the tooling change. Model notes for Claude are below.
- History lives in git, not in `.solidcraft.before-*` copies (they are gitignored). Log notable model changes in the
  commit message instead of a running changelog.
- The user does not want headless SolidCraft — always drive the app window.
- STLs: re-export only when the user asks.
- Animation: `tools/anim/README.md` (storyboard JSON → WebM): `anim/purpose.json` → `tools/anim/make-gif.sh` →
  `anim/purpose.gif` (README header).
- Print prep: `tools/prusaslicer/README.md`. Use the `prusaslicer` MCP server (`arrange` → loads plate into the user's
  PrusaSlicer window, `slice`, `cli`). `stl/plate.json` holds per-part print orientation; generated plates go to
  `stl/plates/` (gitignored).

## The model (`rpi5_window_cam.solidcraft`)

**Open / save.** `solidcraft --control 7878 rpi5_window_cam.solidcraft` from the repo root (last saved with 0.23.1; works
from inside bwrap too, the window opens on the host display; libEGL/MESA warnings are harmless; the app closes when the
Claude Code session that started it ends). Opened that way it has no file path (`inspect_design` → `path: null`): save
with `save {path: "rpi5_window_cam.solidcraft"}`. The MCP server may only write to the repo, the open design's folder,
`/tmp/solidcraft-mcp-*/` and the recovery folder (an app started elsewhere: use its `/tmp/solidcraft-mcp-*` folder).

**Export.** One body per printed part, same names as in `stl/plate.json`: `Case`, `Cover`, `WindowPad`, `Connector`,
`CamHousing`, `CamLid` → `stl/<file>.stl` (file names: README print table). **Always pass `bodies: [...]`**, otherwise
the embedded Pi and camera models (≈ 634 loose bodies) are exported too. After a geometry change: re-export the affected
STLs, rebuild the plate (`arrange`), re-render the animation if the change shows.

**Coordinates (assembled position).**
- Case back plate outer face = z 0; +Z toward the room; glass at z = `glass_z` (−11.2).
- Pi board landscape, X = 85 mm length, **+Y = up in use** (GPIO edge). −Y edge = USB-C + micro-HDMI (and the CAM/DISP
  connector at x ≈ 6), +X = USB/Ethernet, −X = microSD + power button + LEDs (camera side). Pi PCB z 7.0…8.4.
- Pi standoffs at x −37 / 21, y ±24.5 (board x −40.5…44.5).
- Back-plate honeycomb: pointy-top hexes, pitch 9.6, row 8.3138, origin cell at (0, 0). Connector pegs sit in cells
  (±9.6, 0) and (0, −16.63).
- Camera ball centre at (`ball_x`, `ball_y`, `ball_z`) = (−53.5, −24, 17.2). `CamHousing`/`CamLid` are modelled around a
  local origin and placed by `Move1`…`Move8` + `CamFollowBall`.

**Design rules.** PETG, every overhang ≤ 45°, no supports. `back_t` stays 2 (the connector barbs catch on it).
`fit` (0.2) and `ball_interf` (0.25) are the PETG fits to tune after a test print. `hex_cell`/`hex_rib` are informational
only; `cam_gap`, `joint_x/y/z` are left over and drive nothing. Literal numbers (not parameters): the honeycomb, the
connector peg sketches, `CaseJointThin` (y values), the housing pin/ring sketches. Suppressed, kept for comparison:
`SDSlot`, `SDArch` (replaced by `SDHex`), `FFCNotch`, `LatchWinLeft`/`TabLeft`/`BumpLeft`, old `LidTabR/L`/`LidBumps`.

**Reference models** (© Raspberry Pi Ltd, see `LICENSE`). To re-import a newer revision, download into `ref/` (gitignored).
- Pi 5: `RaspberryPi5_official` (occurrence 118), RP-004882-DD Iss1 (2023). +90° about X, PCB on the standoff tops,
  +2 mm in X. Downloads: <https://pip.raspberrypi.com/categories/892-raspberry-pi-5> (now `RP-010083-CA-1` STEP).
- Camera Module 3 (standard): `CameraModule3_official` (occurrence 346). 180° about Z, then (−71.9, −11.5, 15.88), before
  the housing moves. `RP-008154-DS-1-camera-module-3-step.zip` → `Camera_module_3_std_model_simple.stp` from
  <https://pip.raspberrypi.com/categories/1207-design-files>.
- `stl/external/pi.stl`, `camera3.stl` are meshes of these for the animation (see `stl/external/README.md`).

**Checks done.** Only the intended ball grip interferes. Pi 5: no contact with Case/Cover, all ports inside their
cut-outs (upper USB stack 0.13 mm to the ±27 mm notch), microSD and power button reachable through `SDHex`. Camera: no
contact with housing/lid. Tilt ±30° about X and Y (copies rotated about the ball centre; set `ball_interf` to −0.3 for
the check, the kernel can't intersect concentric spheres, then back to 0.25): no contact with Case/Cover.

**Open items.** Not fully test-printed (peg click, ball grip, lid tabs). Down 30° + sideways 30° together can swing the
far lid corner into the glass. `SDHex` roof is a 60° overhang over ~10 mm. Camera board is 0.67 mm, lid rings assume
1.0 (0.33 mm gap). Possible later: display cover variant (bigger `case_depth`), portrait mounting (second connector).

**SolidCraft gotchas.**
- Sketch plane `origin` ignores expressions (silently 0): use numeric origins, or build at the origin and `solid.move`
  with an expression translate.
- Joins with exactly touching faces fail ("zero-thickness edge contact"): overlap 0.1–0.2 mm. Exactly tangent
  circles/spheres fail booleans too (+0.01).
- A cut whose cylinder coincides with an existing cylinder fails: make it slightly bigger.
- `solid.extrude` of several profiles as `new` makes several bodies (`X`, `X (2)`): join them before combining.
- A `solid.box` cut once split a lid ring off as a separate body; the same cut as sketch + extrude didn't.
- `timeline.redefine` / `timeline.edit` change features in place; `timeline.delete` removes them.
- Big recomputes can take > 2 min (the MCP call moves to the background); `inspect.interference` with the full camera
  reference model can outlast the MCP timeout.
- `inspect_design` on this design is ~500 kB (640 bodies): pass `sketches: false` and parse the saved file.
- An ASCII STL export of everything is one solid; split by position, not by name.

## bwrap sandbox — check at the start of every session

Claude Code is sometimes started inside the user's bwrap wrapper. Detect it before any PrusaSlicer / render work:

```sh
tr '\0' ' ' </proc/1/cmdline | grep -q '^bwrap' && echo BWRAP   # or: [ -d /var/lib/flatpak ] || echo BWRAP
curl -s -m 2 -o /dev/null http://127.0.0.1:7879/ && echo PORT-7879-ANSWERS
[ -f tools/prusaslicer/.bridge-token ] && echo THIS-REPOS-BRIDGE   # the bridge writes its token next to itself
```

Port answers but no `tools/prusaslicer/.bridge-token` here = a bridge from another checkout (e.g. the old `~/test/solidcraft-mcp`) is
running; it can't be used (wrong token, refuses paths outside its repo). Ask the user to stop it and start this repo's copy.

Inside bwrap: no flatpak (`/var` not mounted, so `flatpak run`/`flatpak list` fail), no D-Bus, no `which`. The
`prusaslicer` MCP server is a child of Claude Code, so it is inside the sandbox too: `arrange` still builds plates, but opening PrusaSlicer, `slice` and `cli` only work through the bridge. If in bwrap and the bridge is
not up, **tell the user up front, before trying, to run in a normal terminal (not via `!`, that is inside bwrap too):**

```sh
python3 ~/projects-micodio/local-traffic/rpi5-case/tools/prusaslicer/bridge.py
```

(it must be this repo's copy — the bridge only accepts paths inside its own repo), or to restart Claude Code without the
wrapper. Do not try workarounds (no downloaded AppImages or other external executables).

Rendering the videos and `make-gif.sh` **do work inside bwrap** (verified 2026-10-30). `/etc/alternatives` is not mounted,
so ffmpeg's BLAS/LAPACK links are broken. Point it at the real libraries:

```sh
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/blas:/usr/lib/x86_64-linux-gnu/lapack
node tools/anim/render.mjs anim/purpose.json && tools/anim/make-gif.sh   # ~1.5 min at 720p
```

`render.mjs` uses `/usr/lib/chromium/chromium` directly (the `/usr/bin/chromium` wrapper fails here). Check frames with
`--still T --out /tmp/x.png` and read the PNG before a full render.

Works inside bwrap: SolidCraft (`/usr/bin/solidcraft`, its window opens on the host display), `solidcraft-cli`, Python tools,
node. numpy is broken either way — keep tools stdlib-only.
