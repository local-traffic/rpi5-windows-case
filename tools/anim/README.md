# Animations → WebM / GIF

`render.mjs` turns a **storyboard** (JSON) plus STL meshes into a video. Headless Chromium draws each frame with three.js
(`player.html`) and the PNGs are piped into ffmpeg. There are no npm dependencies: you need Node ≥ 20, chromium or google-chrome,
and ffmpeg.

```sh
# render (size/fps/out default to the storyboard's "video" block)
node tools/anim/render.mjs anim/purpose.json
node tools/anim/render.mjs anim/purpose.json --size 1280x720 --fps 25 --out /tmp/x.webm
node tools/anim/render.mjs ... --still 12.5 --out frame.png   # one frame, for checking
node tools/anim/render.mjs ... --from 10 --to 15               # only part of the timeline
node tools/anim/render.mjs ... --ss 2                          # 2× supersampling (smoother, ~3× slower)
node tools/anim/render.mjs ... --out video.mp4                 # H.264 instead of VP9
# interactive preview while writing the storyboard
node tools/anim/render.mjs anim/purpose.json --preview   # → http://127.0.0.1:7890/
```

Preview keys: Space = play/pause, ←/→ = step one frame, **F = free camera**, R = reload the storyboard after an edit.
With the free camera on, you orbit with the mouse and the page shows the matching `"camera": {"from": …, "to": …}` line,
ready to copy into a scene.

Speed: about 3 frames/s at 1080p on the CPU renderer (SwiftShader), so a 30 s clip takes about 5 min. At 720p it is
roughly twice as fast. `--crf` sets quality (VP9 default 28; lower = better and bigger).

## Storyboard format

The file is JSON, plus whole-line `//` comments. Units are mm and degrees, in the CAD model's coordinates
(the meshes are exported **in assembled position**).

```jsonc
{
  "video":  { "width": 1920, "height": 1080, "fps": 30, "out": "purpose.webm", "background": "#dfe3e7",
              "accent": "#d9631e", "numberSteps": true },
  "meshDir": "meshes",                         // STL paths are relative to this (relative to the storyboard)

  "groups": {                                  // empty nodes used to move several parts together
    "unit": {},
    "cam":  { "parent": "unit", "pivot": [-53.5, -24, 17.2] }   // rotations happen about the pivot
  },
  "parts": {
    "case":  { "stl": "case.stl", "color": "#d9631e", "parent": "unit" },
    "cover": { "stl": "cover.stl", "color": "#8d99a6", "opacity": 0.72 },   // base opacity
    "pi":    { "stl": "pi.stl", "color": "#2f7d4f", "edges": false },       // no outline edges
    "glass": { "plane": [320, 240], "at": [-30, 10, -11.2], "color": "#9fc4d8", "opacity": 0.18 },
    "tape1": { "box": [16, 6, 0.8], "at": [-11, 3, -11.6], "color": "#f4f1ea", "parent": "pad" },  // box: size + centre
    "ffc":   { "ribbon": [{ "at": [5, -17, 13.5], "node": "pi" }, { "at": [-56, -23, 23], "node": "cam" }],
               "width": 12, "side": [0, 1, 0], "color": "#e9dfc0" }   // flat cable, see below
  },                                           // a part's pivot defaults to its bounding-box centre

  "initial": { "cover": { "move": [0, 0, 90] } },   // the state at t = 0 (exploded view); omitted = assembled
  "camera":  { "from": [-270, 165, 350], "to": [-15, 25, 30], "fov": 32 },

  "scenes": [ … ]                              // played one after another
}
```

### Scenes

```jsonc
{ "title": "Snap the cover on",              // lower-left caption, auto-numbered "Step n / N"
  "text":  "Latches at top-left, top-right and bottom.",
  "step":  "Assembled",                      // optional: replaces the "Step n / N" label (and isn't counted)
  "card":  "Pi 5 window camera", "cardText": "subtitle",   // optional: full-screen title card instead
  "duration": 2.6,                            // seconds
  "camera":  { "from": [...], "to": [...], "fov": 32, "orbit": 40, "start": 0, "end": 1.2, "ease": "inOut" },
  "actions": [
    { "node": "cover", "move": [0, 0, 0], "start": 0.2, "end": 1.8, "ease": "back" },
    { "node": "cover", "flash": 0.8, "start": 1.7, "end": 2.2 },
    { "label": "click", "node": "cover", "at": [0, 32, 30], "start": 1.8, "end": 2.6 }
  ] }
```

- Each **action** animates a node (part or group) from its current state **to** the given values, between `start` and
  `end` (seconds from the start of the scene; the default is the whole scene). `node` can also be a list of names.
  - `move: [x,y,z]`: offset from the assembled position (`[0,0,0]` = in place).
  - `rotate: [rx,ry,rz]`: degrees about the node's pivot.
  - `opacity`: 0 to 1, multiplied down to children. Use it to fade the other parts and show one part on its own.
  - `glow`: 0 to 1, a lasting highlight in the accent colour. `flash: 0.8` is a one-off pulse (`"times": 2` for a double pulse).
  - `label` + `at`: a callout pinned to a model point. With `node` set, the point follows that node.
- A **ribbon** part is a flat strip (a flat cable) through its points. Each point is given in assembled coordinates
  and follows its `node` (part or group), and the strip is rebuilt every frame as a smooth curve. So it bends instead
  of tearing when the nodes at its two ends move apart. `side` is the width direction (default +Y). A ribbon takes no
  `parent`. To unplug a cable, attach its end points to a group inside the part and move that group back.
- **camera**: the same "to the new values" rule. `orbit` = degrees around the vertical axis through `to` (absolute,
  so `0 → 50` is one 50° turn). `camera` and `actions` entries may be a single object or a list.
- **ease**: `inOut` (default), `in`, `out`, `linear`, `back` (overshoot and settle, good for snap-in clicks), `snap` (jump at the end).
- Captions fade in and out per scene. When neighbouring scenes share a title, one caption stays on screen across them.

## Meshes

Each moving part is an STL in assembled position (`export {path, bodies: ["Case"]}` in SolidCraft). The window-cam
storyboard uses the printable STLs in `stl/` directly (`"meshDir": "../stl"`), so one export updates printing and the
video; the Pi board, which is not printed, is `stl/external/pi.stl`. The Camera Module 3 board is not in the scene.

## README preview GIF

The README shows a short looping clip, `anim/purpose.json` (camera aiming, unit lifting off the pad, quick exploded view with a Hailo-8 AI HAT+). Render it, then
`tools/anim/make-gif.sh` turns `anim/purpose.webm` into `anim/purpose.gif` (640 px, 12 fps; arguments:
`[video] [out.gif] [width] [fps]`). Run it after every re-render.
