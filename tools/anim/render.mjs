#!/usr/bin/env node
// Storyboard → video. Renders tools/anim/player.html in headless Chromium frame by frame and pipes PNGs into ffmpeg.
// No npm dependencies: Node ≥ 20 (built-in WebSocket), chromium/google-chrome, ffmpeg.
//
//   node tools/anim/render.mjs <storyboard.json> [options]
//     --out FILE         output (.webm = VP9, .mp4 = H.264, .png with --still); default: video.out or <storyboard>.webm
//     --size WxH         e.g. 1280x720 (default: video.width/height or 1920x1080)
//     --fps N            default: video.fps or 30
//     --crf N            quality, lower = better (default 28 for VP9, 20 for H.264)
//     --ss N             supersampling factor (render N× larger, downscale) — smoother edges, slower (default 1)
//     --from S --to S    render only this time range (seconds)
//     --still S          render one frame at S seconds to a PNG
//     --preview          serve the interactive player (timeline, free camera) instead of rendering
//     --port N           port for --preview (default 7890)
//     --chrome PATH      browser binary (or env CHROME)
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

if (typeof WebSocket === "undefined") {  // Node 20: WebSocket client is behind a flag
  const r = spawnSync(process.execPath, ["--experimental-websocket", "--no-warnings", ...process.argv.slice(1)], { stdio: "inherit" });
  process.exit(r.status ?? 1);
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = {};
const positional = [];
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a.startsWith("--")) {
    const k = a.slice(2);
    if (k === "preview") opt.preview = true;
    else opt[k] = args[++i];
  } else positional.push(a);
}
if (!positional[0] || opt.help) {
  console.error(fs.readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n").slice(1, 17).map(l => l.replace(/^\/\/ ?/, "")).join("\n"));
  process.exit(2);
}
const SB_PATH = path.resolve(positional[0]);
const SB_DIR = path.dirname(SB_PATH);

// JSON with whole-line // comments
function readStoryboard() {
  const txt = fs.readFileSync(SB_PATH, "utf8").split("\n").filter(l => !/^\s*\/\//.test(l)).join("\n");
  try { return JSON.parse(txt); } catch (e) { throw new Error(`${SB_PATH}: ${e.message}`); }
}
let sb = readStoryboard();
const V = sb.video || {};
let [W, H] = opt.size ? opt.size.split("x").map(Number) : [V.width || 1920, V.height || 1080];
const FPS = +(opt.fps || V.fps || 30);
const SS = +(opt.ss || 1);

// ---------- tiny static server: player + three.js + storyboard (re-read each request) + meshes
const STATIC = { "/": "player.html", "/three.min.js": "vendor/three.min.js", "/OrbitControls.js": "vendor/OrbitControls.js" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  const send = (code, body, type = "text/plain") => { res.writeHead(code, { "content-type": type, "cache-control": "no-store" }); res.end(body); };
  try {
    if (STATIC[url.pathname]) {
      const f = STATIC[url.pathname];
      return send(200, fs.readFileSync(path.join(HERE, f)), f.endsWith(".html") ? "text/html; charset=utf-8" : "text/javascript");
    }
    if (url.pathname === "/storyboard.json") {
      sb = readStoryboard();
      // size given on the command line wins (the page sizes the stage from video.width/height)
      return send(200, JSON.stringify({ ...sb, video: { ...(sb.video || {}), width: W, height: H } }), "application/json");
    }
    if (url.pathname.startsWith("/mesh/")) {
      const name = decodeURIComponent(url.pathname.slice(6));
      const part = (sb.parts || {})[name];
      if (!part?.stl) return send(404, `no stl for part "${name}"`);
      const file = path.resolve(SB_DIR, sb.meshDir || ".", part.stl);
      if (!fs.existsSync(file)) return send(404, `missing file ${file}`);
      return send(200, fs.readFileSync(file), "application/octet-stream");
    }
    send(404, "not found");
  } catch (e) { send(500, String(e.message || e)); }
});

// ---------- Chrome DevTools Protocol
function findChrome() {
  // Debian's /usr/bin/chromium is a shell wrapper around /usr/lib/chromium/chromium (the wrapper fails in some sandboxes)
  const cands = [opt.chrome, process.env.CHROME, "/usr/lib/chromium/chromium", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"].filter(Boolean);
  const dirs = (process.env.PATH || "").split(path.delimiter).filter(Boolean);
  for (const c of cands) {
    if (c.includes("/")) { if (fs.existsSync(c)) return c; continue; }
    const hit = dirs.map(d => path.join(d, c)).find(p => fs.existsSync(p));
    if (hit) return hit;
  }
  throw new Error("no chromium/google-chrome found (use --chrome PATH)");
}

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url); this.id = 0; this.pending = new Map();
    this.ws.onmessage = e => {
      const m = JSON.parse(e.data);
      const p = m.id && this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      m.error ? p.reject(new Error(`${p.method}: ${m.error.message}`)) : p.resolve(m.result);
    };
    this.opened = new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = () => rej(new Error("CDP connect failed")); });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject, method }));
  }
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}

async function render(pageUrl) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "anim-chrome-"));
  const chrome = spawn(findChrome(), [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
    "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
    `--window-size=${W},${H}`, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  const exited = new Promise(r => chrome.once("exit", r));
  const rmProfile = () => { try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5 }); } catch {} };
  const cleanup = async () => {
    try { chrome.kill("SIGKILL"); } catch {}
    await Promise.race([exited, new Promise(r => setTimeout(r, 3000))]);  // chrome must stop writing into the profile first
    rmProfile();
  };
  process.on("exit", () => { try { chrome.kill("SIGKILL"); } catch {} rmProfile(); });
  try {
    const wsUrl = await new Promise((res, rej) => {
      let buf = "";
      const to = setTimeout(() => rej(new Error("chromium did not start:\n" + buf)), 20000);
      chrome.stderr.on("data", d => {
        buf += d;
        const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
        if (m) { clearTimeout(to); res(m[1]); }
      });
      chrome.on("exit", c => rej(new Error(`chromium exited (${c}):\n${buf}`)));
    });
    const browser = new CDP(wsUrl); await browser.opened;
    const { targetId } = await browser.send("Target.createTarget", { url: "about:blank" });
    const page = new CDP(wsUrl.replace(/\/devtools\/browser\/.*/, `/devtools/page/${targetId}`)); await page.opened;
    await page.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: SS, mobile: false });
    await page.send("Page.enable");
    await page.send("Page.navigate", { url: pageUrl });
    const t0 = Date.now();
    for (;;) {
      const st = await page.eval("({ready: !!window.__ready, err: window.__error || null, dur: window.__duration})").catch(() => ({}));
      if (st.err) throw new Error("page: " + st.err);
      if (st.ready) { var DUR = st.dur; break; }
      if (Date.now() - t0 > 120000) throw new Error("page did not become ready in 120 s");
      await new Promise(r => setTimeout(r, 200));
    }
    const shot = async t => {
      await page.eval(`__setTime(${t})`);
      const { data } = await page.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      return Buffer.from(data, "base64");
    };

    if (opt.still !== undefined) {
      const out = path.resolve(opt.out || path.join(SB_DIR, "still.png"));
      let png = await shot(+opt.still);
      if (SS !== 1) png = spawnSync("ffmpeg", ["-v", "error", "-i", "-", "-vf", `scale=${W}:${H}:flags=lanczos`, "-f", "image2", "-c:v", "png", "-"], { input: png, maxBuffer: 1 << 28 }).stdout;
      fs.writeFileSync(out, png);
      console.log(out);
      return;
    }

    const out = path.resolve(opt.out || (V.out ? path.resolve(SB_DIR, V.out) : SB_PATH.replace(/\.json$/, "") + ".webm"));
    const from = +(opt.from || 0), to = Math.min(DUR, +(opt.to ?? DUR));
    const frames = Math.max(1, Math.round((to - from) * FPS));
    const mp4 = out.endsWith(".mp4");
    const crf = String(opt.crf || (mp4 ? 20 : 28));
    const enc = mp4
      ? ["-c:v", "libx264", "-preset", "slow", "-crf", crf, "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
      : ["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-pix_fmt", "yuv420p", "-row-mt", "1", "-deadline", "good", "-cpu-used", "2"];
    const ff = spawn("ffmpeg", ["-y", "-v", "error", "-f", "image2pipe", "-c:v", "png", "-framerate", String(FPS), "-i", "-",
      ...(SS !== 1 ? ["-vf", `scale=${W}:${H}:flags=lanczos`] : []), ...enc, out], { stdio: ["pipe", "inherit", "inherit"] });
    const ffDone = new Promise((res, rej) => ff.on("exit", c => c === 0 ? res() : rej(new Error(`ffmpeg exited ${c}`))));
    console.error(`${W}x${H} @ ${FPS} fps, ${(to - from).toFixed(2)} s = ${frames} frames → ${out}`);
    const start = Date.now();
    for (let i = 0; i < frames; i++) {
      const png = await shot(from + i / FPS);
      if (!ff.stdin.write(png)) await new Promise(r => ff.stdin.once("drain", r));
      if (i % 15 === 0 || i === frames - 1) {
        const el = (Date.now() - start) / 1000, eta = el / (i + 1) * (frames - i - 1);
        process.stderr.write(`\r  frame ${i + 1}/${frames}  ${el.toFixed(0)} s elapsed, ~${eta.toFixed(0)} s left   `);
      }
    }
    ff.stdin.end();
    await ffDone;
    console.error("");
    console.log(out);
  } finally { await cleanup(); }
}

// ---------- main (at the end: classes above must be initialised first)
if (opt.preview) {
  const port = +(opt.port || 7890);
  server.listen(port, "127.0.0.1", () => {
    console.log(`Preview: http://127.0.0.1:${port}/   (Space play · ←/→ frame · F free camera · R reload storyboard · Ctrl+C quit)`);
  });
} else {
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  try { await render(`http://127.0.0.1:${server.address().port}/?render=1`); }
  catch (e) { console.error("error:", e.message || e); process.exitCode = 1; }
  finally { server.close(); }
}
