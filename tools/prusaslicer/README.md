# PrusaSlicer tools (Prusa CORE One)

Pure Python 3 standard library — no numpy/trimesh needed. MIT licensed (see [`LICENSE`](../../LICENSE)).

Without Claude Code you only need `plate.py`: `python3 tools/prusaslicer/plate.py stl/plate.json` writes a 3MF you open in PrusaSlicer.

| file | what |
|---|---|
| `plate.py` | orient STLs for printing + pack them on the bed → PrusaSlicer `.3mf` + `.png` top view |
| `mcp.py` | MCP server `prusaslicer` (registered in `../../.mcp.json`): `arrange`, `open`, `slice`, `cli` |
| `bridge.py` | only when Claude runs inside bwrap: run **on the host** — executes PrusaSlicer for the MCP server (127.0.0.1:7879) |
| `printers/coreone.json` | bed 250×220, max height 270, margin/gap, system preset names (taken from a PrusaSlicer 2.9.5 project) |

## How Claude talks to PrusaSlicer (verified 2026-10-22, PrusaSlicer 2.9 flatpak)

PrusaSlicer has **no remote-control API** (unlike SolidCraft's port 7878). Interaction is one-way, file based:

```
Claude ─MCP stdio─▶ tools/prusaslicer/mcp.py ─▶ flatpak run com.prusa3d.PrusaSlicer --single-instance <file.3mf>
                                              └▶ flatpak run com.prusa3d.PrusaSlicer --export-gcode … (CLI, headless)
```

- **Show in the GUI** (`arrange`, `open`): `plate.py` writes the 3MF, then `--single-instance <file>` is launched.
  If a PrusaSlicer window is running, the new process hands the file to it over D-Bus and exits
  (GUI log: `Instance check: Another instance found. This instance will terminate.` = success);
  otherwise it opens a new window. Log: `tools/prusaslicer/.prusaslicer-gui.log`.
- **Replacing the plate:** Claude cannot clear the plate. Loading a project 3MF replaces it; PrusaSlicer may first ask
  "save changes?" (→ Don't save) or "open as project / import geometry" (→ Open as project). The user answers in the GUI.
- **No read-back:** Claude can't see what is moved by hand or which presets are selected. `arrange` returns a PNG
  top view so Claude sees its own layout.
- **Presets:** the generated 3MF carries no print config, so the GUI keeps the user's selected presets
  (CORE One / PETG). Per-object overrides (e.g. supports for one part) go in the plan's `settings`.
- **Slicing** (`slice`): CLI with `--printer-profile/--print-profile/--material-profile` from `printers/coreone.json`
  and `--no-binary-gcode` (CORE One profiles default to `.bgcode`; text keeps time/filament stats readable).
- **Code changes** to `plate.py` / `mcp.py` take effect only after the MCP server restarts (restart Claude Code).

### Typical session

1. User opens PrusaSlicer (`flatpak run com.prusa3d.PrusaSlicer`) with the CORE One + PETG presets.
2. `arrange {plan: "stl/plate.json", only: "Case,Cover", output: "stl/plates/x.3mf"}`
   → plate appears in the window; user fine-tunes by hand.
3. Optional `slice {path: …3mf}` → G-code + print time / filament.
4. After a CAD change: re-export the STL from SolidCraft into `<project>/stl/`, then `arrange` again.

## Sandbox (bwrap) and the bridge

Claude Code is **sometimes** started inside the user's `bwrap` wrapper (check `cat /proc/1/cmdline`). Inside it only
`/usr`, `~/.claude`, this repo… are visible — no `/var/lib/flatpak`, no D-Bus — so `flatpak run` fails for Bash,
`!` commands and MCP servers alike. Started normally (PID 1 = `/sbin/init`), flatpak works directly and no bridge is needed.

Inside bwrap the network is still shared, so run `python3 tools/prusaslicer/bridge.py` (from the repo root) in a normal
terminal; the MCP server prefers the bridge when `tools/prusaslicer/.bridge-token` exists and 127.0.0.1:7879 answers, else runs flatpak
directly. Bridge guards: PrusaSlicer only (no shell), token file, paths must be inside the repo, `--post-process` and any
`.3mf`/`.ini` setting `post_process` refused.

## Plan files

One per project, e.g. `../../stl/plate.json`. Paths in it are relative to the plan file. Per part:
- `down`: side of the STL (as exported) that goes onto the bed: `-z` (keep), `+z` (flip), `±x`, `±y`, `[x,y,z]`, or `auto`
  (largest flat bed contact; within 15 % of it, least >45° overhang, then lowest height).
- `spin`: degrees about Z, default `auto` (minimum-area footprint, long side along X; packer may add 90°).
- `settings`: per-object PrusaSlicer overrides written into the 3MF, e.g.
  `{"support_material": 1, "support_material_auto": 1, "support_material_buildplate_only": 1}`.
- `copies`, `name`, `note`.

CLI: `python3 tools/prusaslicer/plate.py stl/plate.json [--only Case,Cover] [--gap 8] [--explain]`.
Parts that don't fit are listed as `DID NOT FIT` — place them manually / on a second plate (`--only`).

Packing uses bounding rectangles (skyline bottom-left), so it is safe but not maximally dense.
Orientation is checked per part: `--explain` lists candidates with bed contact and overhang area.
