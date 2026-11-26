// Endora Highlights - DaVinci Resolve Workflow Integration plugin (Resolve Studio 19 / 20).
// Resolve starts this file with its own Electron. The panel (index.html) talks to:
//   - Resolve, through WorkflowIntegration.node (here, in the main process, via IPC)
//   - the AutoWed engine at http://127.0.0.1:7788 (analysis on the GPU, offline)
const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");

const PLUGIN_ID = "lk.endora.highlights";
let WI = null;
let resolve = null;
let win = null;

function loadWI() {
  if (WI) return WI;
  const here = path.join(__dirname, "WorkflowIntegration.node");
  const cands = [here,
    "C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Workflow Integrations\\Examples\\SamplePlugin\\WorkflowIntegration.node",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"];
  for (const p of cands) {
    try { if (fs.existsSync(p)) { WI = require(p); return WI; } } catch (e) { /* try next */ }
  }
  throw new Error("WorkflowIntegration.node හම්බුණේ නැහැ (Resolve Studio 19/20 ඕන).");
}

async function getResolve() {
  if (resolve) return resolve;
  const wi = loadWI();
  const ok = await wi.Initialize(PLUGIN_ID);
  if (!ok) throw new Error("Resolve එකට connect වෙන්න බැරි වුණා.");
  resolve = await wi.GetResolve();
  if (!resolve) throw new Error("Resolve object එක ගන්න බැරි වුණා.");
  return resolve;
}

const num = (v, d = 0) => { const n = parseFloat(v); return isFinite(n) ? n : d; };

async function project() {
  const r = await getResolve();
  const pm = await r.GetProjectManager();
  const p = await pm.GetCurrentProject();
  if (!p) throw new Error("Project එකක් open කරලා නැහැ.");
  return p;
}

// every video clip in the media pool (all bins), with its file path and bin name
async function poolClips(mp) {
  const out = [];
  const walk = async (folder, binPath) => {
    const name = await folder.GetName();
    const here = binPath ? binPath + "/" + name : "";
    const clips = (await folder.GetClipList()) || [];
    for (const c of clips) {
      try {
        const type = String((await c.GetClipProperty("Type")) || "");
        const file = String((await c.GetClipProperty("File Path")) || "");
        if (!file || !/video/i.test(type)) continue;
        out.push({ id: String(await c.GetUniqueId()), path: file, name: String(await c.GetName()), bin: here || name,
                   fps: num(await c.GetClipProperty("FPS")), _item: c });
      } catch (e) { /* skip odd items (compound clips, timelines) */ }
    }
    for (const sub of (await folder.GetSubFolderList()) || []) await walk(sub, here || "");
  };
  await walk(await mp.GetRootFolder(), "");
  return out;
}

let CLIPS = new Map();       // unique id -> MediaPoolItem (for markers)

async function snapshot() {
  const p = await project();
  const mp = await p.GetMediaPool();
  const tl = await p.GetCurrentTimeline();
  const clips = await poolClips(mp);
  CLIPS = new Map(clips.map((c) => [c.id, c._item]));
  const snap = { project: String(await p.GetName()), clips: clips.map(({ _item, ...c }) => c), music: [], timeline: null };
  if (tl) {
    const fps = num(await tl.GetSetting("timelineFrameRate"), 25);
    const start = num(await tl.GetStartFrame());
    snap.timeline = { name: String(await tl.GetName()), fps, start };
    const n = num(await tl.GetTrackCount("audio"));
    for (let i = 1; i <= n; i++) {
      for (const it of (await tl.GetItemListInTrack("audio", i)) || []) {
        const mpi = await it.GetMediaPoolItem();
        if (!mpi) continue;
        const file = String((await mpi.GetClipProperty("File Path")) || "");
        if (!file) continue;
        // songs only: the sound of camera clips on the timeline is not music
        if (/video/i.test(String((await mpi.GetClipProperty("Type")) || ""))) continue;
        const a = num(await it.GetStart()), b = num(await it.GetEnd());
        const srcFps = num(await mpi.GetClipProperty("FPS"), fps) || fps;
        snap.music.push({ path: file, name: String(await it.GetName()), track: i,
                          start: (a - start) / fps, dur: (b - a) / fps, in: num(await it.GetLeftOffset()) / srcFps });
      }
    }
  }
  snap.fps = snap.timeline ? snap.timeline.fps : 25;
  return snap;
}

const COLORS = { smile: "Yellow", hug: "Pink", couple: "Rose", slowmo: "Purple", drone: "Sky", detail: "Mint",
                 applause: "Lemon", chant: "Sand", speech: "Cyan" };

// markers on the source clips: one per moment (name = score + tags), so the best bits show up in the
// source viewer and in the media pool; done in small steps so Resolve stays responsive
async function markMoments(moments) {
  let n = 0, bad = 0;
  for (const m of moments) {
    const item = CLIPS.get(m.clip);
    if (!item) { bad++; continue; }
    try {
      const fps = num(await item.GetClipProperty("FPS"), 25) || 25;
      const f = Math.max(0, Math.round(m.start * fps));
      const d = Math.max(1, Math.round((m.end - m.start) * fps));
      const color = COLORS[(m.tags || []).find((t) => COLORS[t])] || "Green";
      const ok = await item.AddMarker(f, color, `★ ${m.score.toFixed(2)} ${(m.tags || []).join(" ")}`.slice(0, 60),
                                      `Endora Highlights ${m.id}`, d, `endora:${m.id}`);
      if (ok) n++; else bad++;
    } catch (e) { bad++; }
    if (n % 20 === 0) await new Promise((r) => setTimeout(r, 30));
  }
  return { added: n, failed: bad };
}

async function clearMarkers() {
  let n = 0;
  for (const item of CLIPS.values()) {
    try { if (await item.DeleteMarkersByCustomData("endora:")) n++; } catch (e) {}
    try {
      const mk = (await item.GetMarkers()) || {};
      for (const [f, v] of Object.entries(mk)) {
        if (String(v.customData || "").startsWith("endora:")) { await item.DeleteMarkerAtFrame(parseInt(f, 10)); n++; }
      }
    } catch (e) {}
  }
  return { cleared: n };
}

// song sections + hits as markers on the highlight timeline
async function markSong(song) {
  const p = await project();
  const tl = await p.GetCurrentTimeline();
  if (!tl) throw new Error("Highlight timeline එකක් open කරලා නැහැ.");
  const fps = num(await tl.GetSetting("timelineFrameRate"), 25);
  const col = { low: "Blue", mid: "Cyan", high: "Orange", peak: "Red" };
  let n = 0;
  for (const s of song.sections || []) {
    const ok = await tl.AddMarker(Math.round(s.start * fps), col[s.energy] || "Cyan", `♪ ${s.energy}`,
                                  `Endora Highlights: ${s.energy} energy, ${s.bars} bars`, Math.max(1, Math.round((s.end - s.start) * fps)), "endora:song");
    if (ok) n++;
  }
  for (const h of song.hits || []) {
    const ok = await tl.AddMarker(Math.round(h * fps) + 1, "Red", "♪ hit", "Endora Highlights: chorus / drop starts", 1, "endora:hit");
    if (ok) n++;
  }
  return { added: n };
}

// ---------------------------------------------------------------- phase 2-3: build the highlight timeline
// plan (from the engine): shots [{clip, src_in, src_out, tl_in, tl_out, slow, why}], music_wav, speech_wav
async function build(plan, opts = {}) {
  const p = await project();
  const mp = await p.GetMediaPool();
  const root = await mp.GetRootFolder();
  const stamp = new Date().toISOString().slice(0, 16).replace("T", " ").replace(":", ".");
  const name = (opts.name || "Endora Highlight") + " " + stamp;
  const bin = await mp.AddSubFolder(root, name);
  if (bin) await mp.SetCurrentFolder(bin);
  const log = [];
  // audio: ducked music + levelled speech lines, rendered by the engine
  const files = [plan.music_wav, plan.speech_wav].filter(Boolean);
  const audio = (await mp.ImportMedia(files)) || [];
  if (audio.length < files.length) log.push("audio files import වුණේ නැහැ: " + files.join(", "));
  // timeline
  const tl = await mp.CreateEmptyTimeline(name);
  if (!tl) throw new Error("අලුත් timeline එකක් හදන්න බැරි වුණා.");
  await p.SetCurrentTimeline(tl);
  const fps = num(await tl.GetSetting("timelineFrameRate"), plan.fps || 25);
  const start = num(await tl.GetStartFrame());
  for (let k = 0; k < 4 && num(await tl.GetTrackCount("audio")) < 2; k++) await tl.AddTrack("audio", "stereo");
  // slow motion: a second copy of the clip in this bin, played at the timeline's frame rate
  const slow = new Map();
  if (opts.slowmo !== false) {
    for (const id of plan.slowmo_clips || []) {
      const src = CLIPS.get(id);
      if (!src) continue;
      try {
        const path0 = String(await src.GetClipProperty("File Path"));
        const [copy] = (await mp.ImportMedia([path0])) || [];
        if (copy && await copy.SetClipProperty("FPS", String(fps))) slow.set(id, { item: copy, native: num(await src.GetClipProperty("FPS"), fps) });
        else log.push("slow-mo set කරන්න බැරි වුණා: " + path0);
      } catch (e) { log.push("slow-mo: " + e); }
    }
  }
  // video: V1, picture only (sound comes from the music + speech tracks)
  const infos = [];
  for (const s of plan.shots) {
    // frame-exact boundaries (rounded once) so the shots join without 1-frame gaps
    const f0 = Math.round(s.tl_in * fps), f1 = Math.round(s.tl_out * fps);
    const n = Math.max(1, f1 - f0);
    const rec = start + f0;
    const sl = s.slow > 1 ? slow.get(s.clip) : null;
    if (sl) {
      const a = Math.round(s.src_in * sl.native);
      infos.push({ mediaPoolItem: sl.item, startFrame: a, endFrame: a + n - 1, trackIndex: 1, recordFrame: rec, mediaType: 1, _s: s });
    } else {
      const item = CLIPS.get(s.clip);
      if (!item) { log.push("clip නැහැ: " + s.clip); continue; }
      const cf = num(await item.GetClipProperty("FPS"), fps) || fps;
      const a = Math.round(s.src_in * cf);
      const ns = Math.max(1, Math.round(n * cf / fps));
      infos.push({ mediaPoolItem: item, startFrame: a, endFrame: a + ns - 1, trackIndex: 1, recordFrame: rec, mediaType: 1, _s: s });
    }
  }
  let placed = 0;
  for (let i = 0; i < infos.length; i += 40) {
    const part = infos.slice(i, i + 40);
    const res = (await mp.AppendToTimeline(part.map(({ _s, ...x }) => x))) || [];
    placed += res.filter(Boolean).length;
  }
  // audio tracks: A1 music (ducked), A2 speech lines
  const lenF = Math.round(plan.length * fps);
  if (audio[0]) await mp.AppendToTimeline([{ mediaPoolItem: audio[0], startFrame: 0, endFrame: lenF - 1, trackIndex: 1, recordFrame: start, mediaType: 2 }]);
  if (audio[1]) await mp.AppendToTimeline([{ mediaPoolItem: audio[1], startFrame: 0, endFrame: lenF - 1, trackIndex: 2, recordFrame: start, mediaType: 2 }]);
  // markers: why each shot is there, and the speech lines
  for (const x of infos) {
    const s = x._s;
    try { await tl.AddMarker(Math.round(s.tl_in * fps), s.chapter === "speech" ? "Cyan" : (s.slow > 1 ? "Purple" : "Blue"), s.chapter, s.why || "", 1, "endora:shot"); } catch (e) {}
  }
  for (const b of plan.bites || []) {
    try { await tl.AddMarker(Math.round(b.tl * fps), "Cyan", "🎤 " + b.name, `${b.name} ${b.start}s - ${b.end}s`, Math.max(1, Math.round(b.dur * fps)), "endora:speech"); } catch (e) {}
  }
  return { timeline: name, shots: infos.length, placed, slowmo: slow.size, log };
}

// ---------------------------------------------------------------- phase 4: learn from a finished highlight
async function learnSnapshot() {
  const p = await project();
  const tl = await p.GetCurrentTimeline();
  if (!tl) throw new Error("ඉවර කරපු highlight timeline එක open කරන්න.");
  const fps = num(await tl.GetSetting("timelineFrameRate"), 25);
  const start = num(await tl.GetStartFrame());
  const items = [];
  const nv = num(await tl.GetTrackCount("video"));
  for (let i = 1; i <= nv; i++) {
    for (const it of (await tl.GetItemListInTrack("video", i)) || []) {
      const mpi = await it.GetMediaPoolItem();
      if (!mpi) continue;
      const a = num(await it.GetStart()), b = num(await it.GetEnd());
      items.push({ tl_in: (a - start) / fps, tl_out: (b - start) / fps, tl_fps: fps,
                   clip_fps: num(await mpi.GetClipProperty("FPS"), fps), track: i });
    }
  }
  const snap = await snapshot();
  return { items, music: snap.music, name: String(await tl.GetName()) };
}

function createWindow() {
  win = new BrowserWindow({
    width: 560, height: 820, minWidth: 420, minHeight: 500, title: "Endora Highlights", backgroundColor: "#16171c",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false },
  });
  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, "index.html"));
  win.on("closed", () => { win = null; resolve = null; try { WI && WI.CleanUp && WI.CleanUp(); } catch (e) {} });
}

const wrap = (fn) => async (_ev, ...a) => { try { return { ok: true, data: await fn(...a) }; } catch (e) { return { ok: false, error: String(e && e.message || e) }; } };
ipcMain.handle("resolve:snapshot", wrap(snapshot));
ipcMain.handle("resolve:markMoments", wrap(markMoments));
ipcMain.handle("resolve:clearMarkers", wrap(clearMarkers));
ipcMain.handle("resolve:markSong", wrap(markSong));
ipcMain.handle("resolve:build", wrap(build));
ipcMain.handle("resolve:learnSnapshot", wrap(learnSnapshot));

app.whenReady().then(createWindow);
app.on("window-all-closed", () => app.quit());
