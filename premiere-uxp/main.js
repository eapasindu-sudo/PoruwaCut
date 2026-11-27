/* AutoWed (Powered by Endora) – Premiere Pro UXP panel
 * Reads the synced sequence, sends it to the local PoruwaCut engine and applies the edit LIVE:
 *  - Multicamera edit : nest the cameras, razor the nest as cuts arrive, then open a project copy in
 *                       which every piece is a real multicam clip with its angle set.
 *  - Manual cutting   : clone the sequence, razor every camera track and disable the unused pieces.
 */
const ppro = require("premierepro");
const ENGINES = ["http://127.0.0.1:7788", "http://localhost:7788"];
const PANEL_VERSION = "__PORUWACUT_VERSION__"; // stamped by build/make_ccx.py
const TICKS = 254016000000;
const $ = (id) => document.getElementById(id);
window.addEventListener("error", (ev) => { try { log("JS error: " + (ev.message || ev.error), "bad"); } catch (e) {} });
window.addEventListener("unhandledrejection", (ev) => { try { log("Error: " + (ev.reason && ev.reason.message || ev.reason), "bad"); } catch (e) {} });

let seqData = null;
let busy = false;

// ---------- engine transport: HTTP first, file-based IPC fallback ----------
// Premiere's UXP sandbox can refuse http:// fetches. Fallback: the panel writes req_<id>.json
// into its own UXP plugin-data folder (always writable, no extra permission) and the engine,
// which scans %APPDATA%\\Adobe\\UXP\\PluginsStorage for PoruwaCut folders, answers with res_<id>.json.
let transport = null;        // "http" | "file"
let engineBase = ENGINES[0];
let lastNetErr = "";
let dataFolder = null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ipcFolder() {
  if (!dataFolder) {
    const lfs = require("uxp").storage.localFileSystem;
    dataFolder = await lfs.getDataFolder();
    try {
      const f = await dataFolder.createFile("poruwacut_panel.json", { overwrite: true });
      await f.write(JSON.stringify({ panel: PANEL_VERSION, path: dataFolder.nativePath }));
    } catch (e) {}
  }
  return dataFolder;
}

async function fileCall(method, path, body, timeoutMs) {
  const dir = await ipcFolder();
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const tmp = await dir.createFile("req_" + id + ".part", { overwrite: true });
  await tmp.write(JSON.stringify({ method, path, body: body || null }));
  try { await tmp.moveTo(dir, { newName: "req_" + id + ".json", overwrite: true }); }
  catch (e) { const f = await dir.createFile("req_" + id + ".json", { overwrite: true }); await f.write(JSON.stringify({ method, path, body: body || null })); try { await tmp.delete(); } catch (e2) {} }
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(250);
    let entry = null;
    try { entry = await dir.getEntry("res_" + id + ".json"); } catch (e) { continue; }
    const txt = await entry.read();
    try { await entry.delete(); } catch (e) {}
    return JSON.parse(txt).body;
  }
  try { const r = await dir.getEntry("req_" + id + ".json"); await r.delete(); } catch (e) {}
  throw new Error("engine did not answer via " + dir.nativePath);
}

async function httpCall(base, method, path, body) {
  const opt = method === "POST"
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }
    : undefined;
  return (await fetch(base + path, opt)).json();
}

async function call(method, path, body, timeoutMs = 8000) {
  if (transport !== "file") {
    for (const base of transport === "http" ? [engineBase] : ENGINES) {
      let j;
      try { j = await httpCall(base, method, path, body); transport = "http"; engineBase = base; }
      catch (e) { lastNetErr = String(e); continue; }
      return licGuard(j);
    }
  }
  const j = await fileCall(method, path, body, timeoutMs);
  transport = "file";
  return licGuard(j);
}
// an engine answer that says "no licence" opens the licence screen
function licGuard(j) {
  if (j && j.license && j.error && typeof showLicense === "function") { showLicense(j.license); throw new Error(j.error); }
  return j;
}

const LOGBUF = [];
let LAST_DONE = "", LAST_BAD = "";
function log(msg, cls) {
  if (typeof msg === "string" && msg.startsWith("✔")) LAST_DONE = msg;
  if (cls === "bad") LAST_BAD = String(msg);
  LOGBUF.push((cls ? `[${cls}] ` : "") + msg);
  if (LOGBUF.length > 800) LOGBUF.splice(0, LOGBUF.length - 800);
  const el = $("log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  const m = /~(\d+) BPM/.exec(msg);
  if (m && $("t_bpm")) $("t_bpm").textContent = m[1];
  const ml = $("minilog");
  if (ml) {
    const d = document.createElement("div");
    if (cls) d.className = cls;
    d.textContent = msg;
    ml.appendChild(d);
    while (ml.children.length > 5) ml.removeChild(ml.children[0]);
  }
}


const fmt = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0");
};
const numOrNull = (id) => { const v = parseFloat(String($(id).value || "").replace(",", ".")); return isFinite(v) && v > 0 ? v : null; };
const tt = (sec) => ppro.TickTime.createWithSeconds(sec);

// ---------- engine status, events, updates ----------
let lastPingErr = "";
async function ping() {
  try {
    const p = await call("GET", "/api/ping", null, transport === "file" ? 4000 : 9000);
    $("eng").textContent = `GPU ${p.nvdec ? "ON" : "OFF"} · ${transport}`;
    $("eng").className = "ok";
    if ($("rdot")) { $("rdot").className = "ok"; $("rdot").title = "engine v" + p.version; }
    if (!$("event").options.length) loadEvents();
    if (!LIC_CHECKED) { LIC_CHECKED = true; checkLicense(); }
    return true;
  } catch (e) {
    $("eng").textContent = "engine offline";
    $("eng").className = "bad";
    if ($("rdot")) $("rdot").className = "";
    lastPingErr = `${lastNetErr} | ${e}`;
    return false;
  }
}

const FALLBACK_EVENTS = [["auto", "Auto (AI)"], ["poruwa", "පෝරුව"], ["dance_floor", "Dance Floor"]];
async function loadEvents() {
  let list = FALLBACK_EVENTS.map(([id, si]) => ({ id, si }));
  try { const r = await call("GET", "/api/presets"); if (r.events && r.events.length) list = r.events; } catch (e) {}
  const sel = $("event");
  sel.innerHTML = "";
  for (const ev of list) {
    const o = document.createElement("option");
    o.value = ev.id;
    o.textContent = ev.si + (ev.en && ev.en !== ev.si ? "  ·  " + ev.en : "");
    sel.appendChild(o);
  }
  let last = "";
  try { last = localStorage.getItem("autowed.event") || ""; } catch (e) {}
  sel.value = list.some((e) => e.id === last) ? last : list[0].id;
  if (sel.selectedIndex < 0) sel.selectedIndex = 0;
  renderPills();
}

async function checkUpdate(manual) {
  try {
    if (manual) log("Update තියෙනවද බලනවා…", "mute");
    // GitHub can be slow on a weak connection: give it time (the default 8 s timed out quietly)
    const u = await call("GET", manual ? "/api/update?force=1" : "/api/update", null, 45000);
    if (u.available) {
      $("upd").style.display = "block";
      if (manual) log(`අලුත් version එකක් තියෙනවා: v${u.latest}. උඩ තියෙන "Update කරන්න" ඔබන්න.`, "ok");
      try { $("upd").scrollIntoView && $("upd").scrollIntoView(); } catch (e) {}
      $("updtxt").textContent = `අලුත් version එකක්: v${u.latest} (දැන් v${u.current})`;
      $("updnotes").textContent = (u.notes || "").split("\n").slice(0, 4).join("\n");
    } else if (manual) {
      log(u.error ? "Update check කරන්න බැරි වුණා: " + u.error : `ඔබ ළඟ තියෙන්නේ අලුත්ම version එක (v${u.current}) ✔`, u.error ? "bad" : "ok");
    }
  } catch (e) { if (manual) log("Engine එකට සම්බන්ධ වෙන්න බැහැ: " + e, "bad"); }
}

async function installUpdate() {
  // Premiere stays open: closing it from outside did not always work (then nothing got installed and
  // the engine was left stopped). The installer updates the engine (it starts again by itself) and the
  // panel files; the new panel loads the next time Premiere opens.
  $("updbtn").disabled = true;
  try {
    const project = await ppro.Project.getActiveProject();
    if (project) { const ok = await project.save(); log(ok ? "Project එක save කළා ✔" : "Project save වුණේ නැද්ද කියලා සැකයි", ok ? "ok" : "warn"); }
  } catch (e) { log("Save: " + e, "warn"); }
  try {
    const r = await call("POST", "/api/update/install", { close_premiere: false }, 45000);
    if (r.status === "error") { log(r.message, "bad"); $("updbtn").disabled = false; return; }
  } catch (e) { log("Update පටන් ගන්න බැරි වුණා: " + e, "bad"); $("updbtn").disabled = false; return; }
  let gone = 0;
  const poll = async () => {
    let s;
    try { s = await call("GET", "/api/update/status", null, 3000); }
    catch (e) {
      // engine is being replaced; wait for the new one
      gone++;
      $("updtxt").textContent = "Update install වෙනවා... (Premiere close කරන්න එපා)";
      $("updnotes").textContent = ""; $("updbtn").style.display = "none";
      if (gone < 120) setTimeout(poll, 2000);
      else log("Engine එක ආයෙ start වුණේ නැහැ. Start menu > AutoWed > AutoWed Engine open කරන්න.", "bad");
      return;
    }
    if (gone > 0) {
      let v = ""; try { v = (await call("GET", "/api/ping", null, 3000)).version || ""; } catch (e) {}
      $("updtxt").textContent = `✔ Engine v${v} දැන් වැඩ. අලුත් panel එක ගන්න Premiere close කරලා ආයෙ open කරන්න (වැඩේ ඉවර වුණාම).`;
      log(`✔ Update install වුණා (engine v${v}). Premiere restart කළාම panel එකත් අලුත් වෙනවා.`, "ok");
      return;
    }
    if (s.status === "downloading") $("updtxt").textContent = `Download වෙනවා... ${Math.round(s.progress * 100)}%`;
    else if (s.status === "installing") $("updtxt").textContent = s.message;
    if (s.status === "error") { log(s.message, "bad"); $("updbtn").disabled = false; return; }
    setTimeout(poll, 1000);
  };
  poll();
}

// ---------- progress display ----------
const STAGES = ["prepare", "audio", "analysis", "finish"];
const STAGE_TXT = {
  prepare: "Sequence සූදානම් කරමින්…", queued: "Engine එකට යවනවා…", read: "Timeline clips කියවමින්…",
  faces: "මනාලයා / මනාලිය අඳුරගන්නවා…", qc: "Audio checks…", sync: "Audio sync…",
  audio: "Audio අහමින් (beats / වචන)…", analysis: "කැමරා analyze කරමින්, cuts timeline එකට…",
  cuts: "අවසාන cuts…", xml: "අවසාන cuts…", finish: "ඉවර කරමින්…", done: "ඉවරයි ✔",
};
function showProgress(stage, frac, eta) {
  $("prog").style.display = "block";
  const pct = Math.max(0, Math.min(100, Math.floor(frac * 100)));
  $("pct").textContent = pct + "%";
  $("bar").firstElementChild.style.width = pct + "%";
  $("stage").textContent = STAGE_TXT[stage] || stage;
  $("eta").textContent = eta != null && stage === "analysis" ? "ඉතිරි ~" + fmt(eta) : "";
  const map = { queued: "prepare", sync: "audio", cuts: "finish", xml: "finish" };
  const idx = STAGES.indexOf(map[stage] || stage);
  document.querySelectorAll("#steps span").forEach((el) => {
    const i = STAGES.indexOf(el.getAttribute("data-s"));
    el.className = stage === "done" ? "done" : i < idx ? "done" : i === idx ? "on" : "";
  });
}

// ---------- reading the sequence ----------
async function readItem(item) {
  const start = (await item.getStartTime()).seconds;
  const end = (await item.getEndTime()).seconds;
  const srcIn = (await item.getInPoint()).seconds;
  let speed = 1;
  try { speed = await item.getSpeed(); } catch (e) {}
  const pi = await item.getProjectItem();
  const clip = ppro.ClipProjectItem.cast(pi);
  let path = "", why = "";
  try {
    if (await clip.isMulticamClip()) why = "multicam clip";
    else if (await clip.isSequence()) why = "nested sequence";
    else path = await clip.getMediaFilePath();
  } catch (e) { why = String(e); }
  // titles, graphics, mattes and synthetic clips have no real file (Premiere gives an id or "")
  if (path && !/[\\/]/.test(path)) { why = "file එකක් නෙවෙයි (title / graphic)"; path = ""; }
  // a proxy (if the editor made one) is analysed instead of the 4K original: much faster to decode
  let proxy = "";
  try { if (path && (await clip.hasProxy())) proxy = await clip.getProxyPath(); } catch (e) {}
  if (proxy && !/[\\/]/.test(proxy)) proxy = "";
  return { start, end, srcIn, speed, path, proxy, why, name: await item.getName() };
}

async function readSequence(rangeOverride) {
  $("tracks").innerHTML = "";
  const project = await ppro.Project.getActiveProject();
  const seq = project && (await project.getActiveSequence());
  if (!seq) { log("Active sequence එකක් නැහැ.", "bad"); return null; }
  let fps = 25;
  try { const tb = parseFloat(await seq.getTimebase()); if (tb > 0) fps = TICKS / tb; } catch (e) {}
  const endT = (await seq.getEndTime()).seconds;
  let inT = 0, outT = endT;
  try { inT = (await seq.getInPoint()).seconds; outT = (await seq.getOutPoint()).seconds; } catch (e) {}
  if (rangeOverride) { inT = rangeOverride[0]; outT = rangeOverride[1]; }   // a batch job's own range
  // In/Out must be set by the editor: AutoWed only ever touches that part of the timeline
  const hasInOut = rangeOverride ? outT > inT + 0.5 : (outT > inT + 0.5 && !(inT < 0.01 && Math.abs(outT - endT) < 0.05));
  const cams = [], warnings = [], nests = [];
  const vCount = await seq.getVideoTrackCount();
  for (let t = 0; t < vCount; t++) {
    const track = await seq.getVideoTrack(t);
    const items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
    const clips = [];
    for (const it of items) {
      const c = await readItem(it);
      if (c.why === "nested sequence") { nests.push({ track: t, it, start: c.start, end: c.end, srcIn: c.srcIn, name: c.name }); continue; }
      if (!c.path) { warnings.push(`V${t + 1} "${c.name}": ${c.why || "media path නැත"} – මගහරිනවා`); continue; }
      if (Math.abs(c.speed - 1) > 0.001) { warnings.push(`V${t + 1} "${c.name}": speed ${c.speed} – මගහරිනවා`); continue; }
      clips.push({ path: c.path, proxy: c.proxy || undefined, offset: c.start - c.srcIn, src_in: c.srcIn, tl_start: c.start, tl_end: c.end });
    }
    if (clips.length) cams.push({ name: track.name || `V${t + 1}`, track: t, clips });
  }
  const audio = [];
  // "Enabled audio only": disabled audio clips (Shift+E) and muted tracks are not listened to
  const onlyEnabled = !$("x_audon") || $("x_audon").value !== "all";
  let skippedAudio = 0;
  const aCount = await seq.getAudioTrackCount();
  for (let t = 0; t < aCount; t++) {
    const track = await seq.getAudioTrack(t);
    try { if (await track.isMuted()) continue; } catch (e) {}
    const items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
    const clips = [];
    for (const it of items) {
      if (onlyEnabled) {
        let off = false;
        try { off = await it.isDisabled(); } catch (e) {}
        if (off) { skippedAudio++; continue; }
      }
      const c = await readItem(it);
      if (!c.path || Math.abs(c.speed - 1) > 0.001) continue;
      clips.push({ path: c.path, proxy: c.proxy || undefined, offset: c.start - c.srcIn, src_in: c.srcIn, tl_start: c.start, tl_end: c.end });
    }
    if (clips.length) audio.push(clips);
  }
  warnings.forEach((w) => log(w, "warn"));
  if (skippedAudio) log(`Disable කරපු audio clips ${skippedAudio}ක් අහන්නේ නැහැ (enable audio විතරයි).`, "mute");
  $("seqinfo").textContent = `${seq.name} · ${fps.toFixed(2)} fps · ` +
    (hasInOut ? `In ${fmt(inT)} → Out ${fmt(outT)} (${fmt(outT - inT)})` : "In/Out දාලා නැහැ") + ` · audio tracks ${audio.length}`;
  cams.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "trk";
    row.innerHTML = `<span class="nm">Cam ${i + 1} · ${c.name}</span><span class="c">${c.clips.length} clips</span>`;
    $("tracks").appendChild(row);
  });
  const nestInRange = nests.find((n) => n.start < outT - 0.1 && n.end > inT + 0.1) || null;
  if (cams.length < 2 && !nestInRange) { log("කැමරා tracks 2ක් වත් හමු නොවුණි (එක් කැමරාවකට එක video track එකක් ඕන).", "bad"); return null; }
  seqData = { seq, name: seq.name, guid: seq.guid.toString(), fps, inT, outT, endT, hasInOut, cams, audio, vCount, nestInRange };
  if (nestInRange) log(`In/Out එකේ nest clip එකක් තියෙනවා: "${nestInRange.name}" (V${nestInRange.track + 1}) ✔`, "ok");
  if (cams.length) log(`කැමරා ${cams.length}ක් කියෙව්වා ✔`, "ok");
  if (!hasInOut) log("Timeline එකේ edit කරන්න ඕන කොටසට In (I) සහ Out (O) දාන්න.", "warn");
  return seqData;
}

// ---------- Premiere edit helpers ----------
// Premiere crashes when edits arrive back to back while it is still redrawing the timeline:
// every live edit is followed by a short pause (see breathe()).
let LIVE_GAP_MS = 60;
// "calm" mode (all cuts after the analysis): every 20 edits a 1 s rest, with a counter in the panel
let CALM = false, CALM_N = 0, CALM_TOTAL = 0;
let LIVE_N = 0;
async function breathe() {
  await sleep(LIVE_GAP_MS);
  if (!CALM) {
    // live cuts too: a longer rest every 15 edits, Premiere redraws the timeline in between
    if (++LIVE_N % 15 === 0) await sleep(800);
    return;
  }
  CALM_N++;
  if ($("stage")) $("stage").textContent = `Timeline එකේ cuts දානවා ${CALM_N}${CALM_TOTAL ? "/" + CALM_TOTAL : ""}…`;
  if (CALM_TOTAL && $("pct")) $("pct").textContent = Math.min(99, Math.round(CALM_N / CALM_TOTAL * 100)) + "%";
  if (CALM_N % 20 === 0) await sleep(1000);
}
function transact(project, label, makeActions) {
  let ok = false;
  project.lockedAccess(() => {
    ok = project.executeTransaction((ca) => { for (const a of makeActions()) ca.addAction(a); }, label);
  });
  return ok;
}

async function findNewSequence(project, before, timeoutMs = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const seqs = await project.getSequences();
    const s = seqs.find((x) => !before.has(x.guid.toString()));
    if (s) return s;
    await sleep(200);
  }
  return null;
}

// Sequence objects can go stale after edits too: look a sequence up again by its guid.
async function freshSeq(project, seq, guid) {
  try {
    const g = guid || seq.guid.toString();
    return (await project.getSequences()).find((x) => x.guid.toString() === g) || seq;
  } catch (e) { return seq; }
}

async function renameSequence(project, seq, name) {
  try {
    const pi = await seq.getProjectItem();
    transact(project, "AutoWed rename", () => [pi.createSetNameAction(name)]);
  } catch (e) { log("Rename: " + e, "warn"); }
}

async function cloneSequence(project, seq, name) {
  const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
  transact(project, "AutoWed clone", () => [seq.createCloneAction()]);
  const s = await findNewSequence(project, before);
  if (!s) throw new Error("sequence clone failed");
  await renameSequence(project, s, name);
  return s;
}

async function uniqueName(project, base) {
  const names = new Set((await project.getSequences()).map((s) => s.name));
  if (!names.has(base)) return base;
  for (let i = 2; i < 100; i++) if (!names.has(`${base} ${i}`)) return `${base} ${i}`;
  return base + " " + Date.now();
}

const EPS = 0.02;
// Premiere can invalidate script objects a moment AFTER an edit, not right away. An operation that
// hits a dead object is simply done again with everything looked up fresh.
const isStale = (e) => /no longer valid/i.test(String((e && e.message) || e));
async function retryStale(fn, tries = 4) {
  for (let i = 0; ; i++) {
    try { return await fn(); }
    catch (e) { if (!isStale(e) || i >= tries - 1) throw e; await sleep(150 * (i + 1)); }
  }
}
async function itemsOf(track) {
  if (typeof track === "function") { const get = track; return retryStale(async () => itemsOfTrack(await get())); }
  return itemsOfTrack(track);
}
async function itemsOfTrack(track) {
  const out = [];
  for (const it of track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
    out.push({ it, a: (await it.getStartTime()).seconds, b: (await it.getEndTime()).seconds });
  }
  return out.sort((x, y) => x.a - y.a);
}
async function readTI(it) {
  const A = await it.getStartTime(), B = await it.getEndTime(), I = await it.getInPoint(), O = await it.getOutPoint();
  return { a: A.seconds, b: B.seconds, inp: I.seconds, out: O.seconds,
           // exact integer ticks (seconds are floats: at 59.94 a sum of them can land a hair under a frame
           // and Premiere then snaps it one frame early = video 1 frame out of sync with the audio)
           ka: tk(A), kb: tk(B), ki: tk(I), ko: tk(O) };
}
function tk(t) { try { const n = Number(t.ticks); if (isFinite(n)) return n; } catch (e) {} return Math.round(t.seconds * TICKS); }
const TPF = {};
async function ticksPerFrame(seq) {
  const g = seq.guid.toString();
  if (!TPF[g]) {
    let n = 0;
    try { n = Number(await seq.getTimebase()); } catch (e) {}
    TPF[g] = n > 0 ? n : Math.round(TICKS / 25);
  }
  return TPF[g];
}
const snapTk = (ticks, tpf) => Math.round(ticks / tpf) * tpf;
const ttk = (ticks) => ppro.TickTime.createWithTicks(String(Math.round(ticks)));

// ---------- cutting a track at time t, keeping sync ----------
// Premiere 27.1+ has a razor action. 26.x does not: there we trim the clip, clone it into the gap
// the trim left, and trim the clone so it fills the gap. Every value is read back and corrected,
// and source time follows the clip's real speed (slow-motion clips run at 0.5x etc.).
function hasNativeRazor(seq) {
  try { return typeof ppro.SequenceEditor.getEditor(seq).createRazorTrackAction === "function"; }
  catch (e) { return false; }
}

// Premiere 26.5 invalidates every track-item object after an edit ("The script object is no longer
// valid"), so an item is never kept across a transaction: `getIt()` finds it again on the track each
// time, as the clip that overlaps the window it is being fitted into.
async function locate(track, win, avoid) {
  // the clip overlapping `win` the most; ties / zero overlap -> the clip closest to the window.
  // `avoid` = the other half of a cut, which must never be picked by mistake.
  let best = null, score = -Infinity;
  for (const x of await itemsOf(track)) {
    if (avoid && Math.abs(x.a - avoid[0]) < EPS && Math.abs(x.b - avoid[1]) < EPS) continue;
    const ov = Math.min(x.b, win[1]) - Math.max(x.a, win[0]);       // negative = gap size
    const sc = ov > 0.001 ? ov : -Math.abs((x.a + x.b) / 2 - (win[0] + win[1]) / 2) - 1e6;
    if (sc > score) { score = sc; best = x; }
  }
  return best ? best.it : null;
}
const locator = (track, win, avoid) => async () => {
  const it = await locate(track, win, avoid);
  if (!it) throw new Error(`clip not found at ${fmt(win[0])}`);
  return it;
};

async function setInAdaptive(project, getIt, want) {
  // set the source in-point; if Premiere interprets the value on another time base, learn it
  const setTo = (x) => retryStale(async () => { const it = await getIt(); transact(project, "AutoWed cut", () => [it.createSetInPointAction(tt(x))]); });
  const readIn = () => retryStale(async () => (await (await getIt()).getInPoint()).seconds);
  let x = want;
  await setTo(x);
  let r = await readIn();
  if (Math.abs(r - want) < 0.002) return true;
  let x0 = x, r0 = r;
  x = Math.abs(r) > 1e-6 && Math.abs(want) > 1e-6 ? want * (want / r) : want + (want - r);
  for (let i = 0; i < 4; i++) {
    await setTo(x);
    r = await readIn();
    if (Math.abs(r - want) < 0.002) return true;
    const slope = (r - r0) / ((x - x0) || 1e-9);
    x0 = x; r0 = r;
    x = Math.abs(slope) > 1e-6 ? x + (want - r) / slope : x + (want - r);
  }
  return false;
}

// Premiere versions differ in what a trim action does to the other values (26.5: setting the
// in-point trims the head and moves the start; setting the start moves only the start). So we try
// the orders, read every value back, and remember the order that worked on this machine.
const FIX_ORDERS = [["in", "start", "end"], ["start", "end", "in"], ["end", "in", "start"]];
let fixOrder = null;

async function applyField(project, getIt, f, want) {
  if (f === "in") return setInAdaptive(project, getIt, want.inp);
  return retryStale(async () => {
    const it = await getIt();
    if (f === "start") transact(project, "AutoWed cut", () => [it.createSetStartAction(tt(want.a))]);
    if (f === "end") transact(project, "AutoWed cut", () => [it.createSetEndAction(tt(want.b))]);
  });
}
const fieldOk = (c, want, f) => f === "in" ? Math.abs(c.inp - want.inp) <= 0.004
  : f === "start" ? Math.abs(c.a - want.a) <= 0.002 : Math.abs(c.b - want.b) <= 0.002;
const allOk = (c, want) => fieldOk(c, want, "in") && fieldOk(c, want, "start") && fieldOk(c, want, "end");

async function fixItem(project, getIt, want) {
  const read = () => retryStale(async () => readTI(await getIt()));
  let c = await read();
  if (allOk(c, want)) return c;
  let orders = fixOrder ? [fixOrder, ...FIX_ORDERS.filter((o) => o !== fixOrder)] : FIX_ORDERS.slice();
  // a clip that has to get longer at the tail: extend the end first, so it never collapses to 0 length
  if (want.b > c.b + 0.002) orders = [["end", "in", "start"], ["end", "start", "in"], ...orders];
  for (const order of orders.slice(0, 2)) {
    for (const f of order) {
      c = await read();
      if (!fieldOk(c, want, f)) { try { await applyField(project, getIt, f, want); } catch (e) {} }
    }
    c = await read();
    if (allOk(c, want)) { fixOrder = order; return c; }
  }
  // no blocking sync check: the footage is already synced by the editor. Note it and carry on.
  c = await read();
  syncWarns.push(fmt(want.a));
  return c;
}
let syncWarns = [];
function reportSyncWarns() {
  if (!syncWarns.length) return;
  log(`සටහන: cuts ${syncWarns.length}ක clip එකේ in-point Premiere එකෙන් කියවපු අගය වෙනස් (${syncWarns.slice(0, 6).join(", ")}${syncWarns.length > 6 ? "…" : ""}). Edit එක නතර කළේ නෑ — ඒ තැන් ටික ඇහෙන් බලන්න.`, "warn");
  syncWarns = [];
}

// A TrackItemSelection is only valid inside createEmptySelection's callback (Premiere 26.5: outside it
// the remove action fails with "script object is no longer valid"), so the items are added and the
// remove transaction runs inside the callback, on a sequence fetched just before.
async function removeWhere(project, getSeq, pickItems, mediaType, label = "AutoWed", ripple = false) {
  return retryStale(async () => {
    const s = await getSeq();
    const items = await pickItems(s);
    if (!items.length) return 0;
    let err = null, ok = false;
    await new Promise((resolve) => {
      const started = ppro.TrackItemSelection.createEmptySelection((sel) => {
        try {
          for (const it of items) sel.addItem(it, true);
          ok = transact(project, label, () => [ppro.SequenceEditor.getEditor(s).createRemoveItemsAction(sel, ripple, mediaType)]);
        } catch (e) { err = e; }
        resolve();
      });
      if (started === false) { err = new Error("selection"); resolve(); }
      setTimeout(resolve, 3000);
    });
    if (err) throw err;
    return ok ? items.length : 0;
  });
}
async function removeItems(project, seq, items, mediaType) {
  // items -> their windows, found again on the fresh sequence inside the selection callback
  const wins = [];
  for (const it of items) wins.push([(await it.getStartTime()).seconds, (await it.getEndTime()).seconds]);
  const g = seq.guid.toString();
  const audio = mediaType === ppro.Constants.MediaType.AUDIO;
  return removeWhere(project, () => freshSeq(project, seq, g), async (s) => {
    const out = [];
    const n = audio ? await s.getAudioTrackCount() : await s.getVideoTrackCount();
    for (let i = 0; i < n; i++) {
      const tr = audio ? await s.getAudioTrack(i) : await s.getVideoTrack(i);
      for (const x of tr.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
        const a = (await x.getStartTime()).seconds, b = (await x.getEndTime()).seconds;
        if (wins.some((w) => Math.abs(w[0] - a) < EPS && Math.abs(w[1] - b) < EPS)) out.push(x);
      }
    }
    return out;
  }, mediaType);
}

// Premiere 26.5 has no razor. A cut at t on a video track = overwrite the clip's own media, from the
// source frame of t to the clip's out point, at t on the same track (the project item's In/Out are set
// for that and cleared after). Premiere does the edit itself, so the track's clip list stays in order.
// (Earlier ways - clone far right and move back - left the list out of order and Premiere 26 then
// stopped drawing the track's clips.) The overwrite also brings the media's audio: it goes onto the
// camera's own audio track when that clip sits there in sync (same content, so nothing changes), and
// otherwise onto a spare muted track, disabled.
let SPARE_AUDIO = {};
let SPARE_VIDEO = {};
const IO_OFFSET = {};   // per file: project-item In/Out vs timeline in-point (seconds)
// camera colors without closing the project: the overwrite takes the project item's label, so the
// camera's label is put on its project item for the edit and the old one put back right after
const CAM_LABEL_IDX = [7, 2, 6, 5, 0, 15, 4, 11];     // Mango, Caribbean, Rose, Forest, Violet, Yellow, ...
let LABEL_OF_TRACK = null;                              // { videoTrackIndex: labelIndex } while colors are on
async function withLabel(project, pi, vIndex, fn) {
  const want = LABEL_OF_TRACK && LABEL_OF_TRACK[vIndex];
  if (want == null) return fn();
  let old = null;
  try { old = await pi.getColorLabelIndex(); } catch (e) {}
  try { if (old !== want) transact(project, "AutoWed color", () => [pi.createSetColorLabelAction(want)]); } catch (e) {}
  try { return await fn(); }
  finally { if (old != null && old !== want) { try { transact(project, "AutoWed color", () => [pi.createSetColorLabelAction(old)]); } catch (e) {} } }
}
// the camera's own audio clip at t, when it plays in sync with the video clip (same file, same offset):
// the overwrite then cuts it at the same frame and the new video/audio pieces stay linked
async function ownAudioAt(project, seq, g, name, vStart, vIn, t) {
  const s = await freshSeq(project, seq, g);
  const n = await s.getAudioTrackCount();
  for (let i = 0; i < n; i++) {
    for (const x of (await s.getAudioTrack(i)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
      const a = (await x.getStartTime()).seconds, b = (await x.getEndTime()).seconds;
      if (!(a < t + EPS && t + EPS < b)) continue;
      let nm = ""; try { nm = await x.getName(); } catch (e) {}
      if (nm !== name) continue;
      const inp = (await x.getInPoint()).seconds;
      if (Math.abs((inp - a) - (vIn - vStart)) > 0.02) continue;      // slipped for sync: leave it alone
      let off = false; try { off = await x.isDisabled(); } catch (e) {}
      return { index: i, disabled: off };
    }
  }
  return null;
}
async function spareAudioIndex(project, seq, g) {
  if (SPARE_AUDIO[g] == null) SPARE_AUDIO[g] = await (await freshSeq(project, seq, g)).getAudioTrackCount();
  return SPARE_AUDIO[g];
}
async function emulatedRazor(project, seq, vIndex, t, whole) {
  const g = seq.guid.toString();
  const tpf = await ticksPerFrame(seq);
  const tT = snapTk(Math.round(t * TICKS), tpf);      // the cut sits exactly on a frame
  t = tT / TICKS;
  const seqNow = async () => (seq = await freshSeq(project, seq, g));
  const track = async () => (await seqNow()).getVideoTrack(vIndex);
  const first = await retryStale(async () => {
    const h = (await itemsOf(track)).find((x) => whole ? Math.abs(x.a - t) < EPS * 4 : (x.a < t - EPS && t + EPS < x.b));
    if (!h) return null;
    let name = ""; try { name = await h.it.getName(); } catch (e) {}
    let off = false; try { off = await h.it.isDisabled(); } catch (e) {}
    return { a: h.a, b: h.b, o: await readTI(h.it), raw: await h.it.getProjectItem(), name, off };
  });
  if (!first) return false;
  const { a, b, o, raw } = first;
  const rate = o.out > o.inp && b > a ? (o.out - o.inp) / (b - a) : 1;
  if (Math.abs(rate - 1) > 0.01) {   // speed-changed clips: an overwrite would play at 100%
    log(`V${vIndex + 1} ${fmt(t)}: speed වෙනස් කරපු clip එකක් cut කළේ නැහැ.`, "warn");
    return false;
  }
  const pi = ppro.ClipProjectItem.cast(raw);
  const wantInT = o.ki + (tT - o.ka);                 // exact: the source frame that plays at t now
  const wantIn = wantInT / TICKS;
  // camera audio stays uncut unless the editor chose linked cuts (Settings > Camera audio)
  const linked = $("x_alink") && $("x_alink").value === "link";
  const own = linked ? await ownAudioAt(project, seq, g, first.name, a, o.inp, t) : null;
  const at = own ? { index: own.index, own: true, disabled: own.disabled } : { index: await spareAudioIndex(project, seq, g) };
  // Some files (e.g. phones / action cams) place a project item's In/Out a few frames away from the
  // timeline clip's in-point (seen: 16 frames). The overwrite is checked against the clip, and redone
  // with the measured offset, which is remembered per file for the next cuts.
  const overwriteFrom = async (inPt, outPt) => withLabel(project, pi, vIndex, async () => {
    let setIO = false;
    try {
      transact(project, "AutoWed cut", () => [pi.createSetInOutPointsAction(ttk(inPt), ttk(outPt))]);
      setIO = true;
      await retryStale(async () => {
        const s = await seqNow();
        transact(project, "AutoWed cut", () => [ppro.SequenceEditor.getEditor(s).createOverwriteItemAction(raw, ttk(tT), vIndex, at.index)]);
      });
    } finally {
      if (setIO) { try { transact(project, "AutoWed cut", () => [pi.createClearInOutPointsAction()]); } catch (e) {} }
    }
  });
  const readNew = () => retryStale(async () => { const it = await findItem(track, t, null); return it ? readTI(it) : null; });
  // the per-file In/Out offset (whole frames, in ticks) is learned only from a real difference of at
  // least half a frame - never from float rounding, which used to shift later cuts of the file by 1 frame
  let off = IO_OFFSET[first.name] || 0;
  await overwriteFrom(wantInT - off, o.ko - off);
  let done = await readNew();
  for (let k = 0; k < 2 && done && Math.abs(done.ki - wantInT) >= tpf / 2; k++) {
    off += done.ki - wantInT;
    IO_OFFSET[first.name] = off;
    await overwriteFrom(wantInT - off, o.ko - off);
    done = await readNew();
  }
  if (!done || Math.abs(done.ki - wantInT) >= tpf / 2) syncWarns.push(fmt(t));
  if (whole && first.off) {   // a recolored piece keeps its enable state
    try { await retryStale(async () => { const it = await findItem(track, t, null); if (it) transact(project, "AutoWed cut", () => [it.createSetDisabledAction(true)]); }); } catch (e) {}
  }
  if (at.own) {
    // linked audio piece: keep it off if the camera audio was off
    if (at.disabled) {
      try {
        await retryStale(async () => {
          const s = await seqNow();
          for (const x of (await s.getAudioTrack(at.index)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
            if (Math.abs((await x.getStartTime()).seconds - t) < EPS * 4 && !(await x.isDisabled())) transact(project, "AutoWed cut", () => [x.createSetDisabledAction(true)]);
          }
        });
      } catch (e) {}
    }
  } else {
    // no in-sync camera audio here: the audio that came with the overwrite is removed again
    try {
      await removeWhere(project, seqNow, async (s) => (await s.getAudioTrack(at.index)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false), ppro.Constants.MediaType.AUDIO, "AutoWed cut");
    } catch (e) {
      try { const s = await seqNow(); await (await s.getAudioTrack(at.index)).setMute(true); } catch (e2) {}
    }
  }
  await breathe();
  return true;
}
async function findItem(track, a, b) {
  for (const x of await itemsOf(track)) {
    if (Math.abs(x.a - a) < EPS * 4 && (b == null || Math.abs(x.b - b) < EPS * 4)) return x.it;
  }
  return null;
}

async function razorTracks(project, seq, tracks, t, native) {
  if (native) {
    const editor = ppro.SequenceEditor.getEditor(seq);
    const V = ppro.Constants.MediaType.VIDEO;
    const at = snapTk(Math.round(t * TICKS), await ticksPerFrame(seq));
    transact(project, "AutoWed cut", () => tracks.map((ti) => editor.createRazorTrackAction(V, ti, ttk(at), false, V)));
    return;
  }
  const g = seq.guid.toString();
  for (const ti of tracks) await emulatedRazor(project, await freshSeq(project, seq, g), ti, t);
}

// Live applier: cuts the given tracks at every committed cut; for "stacked" also enables only the
// chosen camera. Works on the editor's own timeline and only between In and Out.
function makeApplier(project, target, kind, cutTracks, trackOfCam, rangeT0, rangeT1) {
  const native = hasNativeRazor(target);
  log(native ? "Cuts: Premiere razor" : "Cuts: clone + trim (Premiere 26 / 27.0)", "mute");
  const done = new Set();
  let enabledUntil = rangeT0;
  const targetGuid = target.guid.toString();
  const tgt = async () => (target = await freshSeq(project, target, targetGuid));
  let followed = false, lastFollow = 0;
  async function cutAt(times) {
    for (const t of times) {
      if (STOPPING) return;            // Stop: finish the cut in hand, start no new one
      const key = t.toFixed(3);
      if (done.has(key) || t <= 0.001) continue;
      try { await razorTracks(project, await tgt(), cutTracks, t, native); await breathe(); }
      catch (e) {
        // one more try with everything fetched again; if it still fails, skip only this cut
        await sleep(150);
        try { await razorTracks(project, await tgt(), cutTracks, t, native); }
        catch (e2) { log(`Cut ${fmt(t)} මඟ හැරියා: ${e2 && e2.message || e2}`, "warn"); }
      }
      done.add(key);
    }
  }
  async function enablePieces(segments, until) {
    if (kind !== "stacked") return;
    await tgt();
    const camAt = (t) => { for (const s of segments) if (t >= s.start - 1e-3 && t < s.end + 1e-3) return s.cam; return null; };
    const todo = [];
    let processed = enabledUntil;
    for (let c = 0; c < trackOfCam.length; c++) {
      for (const { it, a, b } of await itemsOf(async () => (await tgt()).getVideoTrack(trackOfCam[c]))) {
        if (b <= enabledUntil + 1e-3 || a < rangeT0 - 1e-3 || b > until + 1e-3) continue;
        const chosen = camAt((a + b) / 2);
        if (chosen === null) continue;
        if (c === 0) processed = Math.max(processed, b);
        const want = chosen !== c;
        let cur = false;
        try { cur = await it.isDisabled(); } catch (e) {}
        if (cur !== want) todo.push({ c, a, b, want });
      }
    }
    // Premiere 26.5 invalidates every clip object after each edit, so several clips in ONE
    // transaction fail after the first. One clip per transaction, each looked up fresh.
    for (const x of todo) {
      await retryStale(async () => {
        const hit = (await itemsOf(async () => (await tgt()).getVideoTrack(trackOfCam[x.c])))
          .find((y) => Math.abs(y.a - x.a) < 0.01 && Math.abs(y.b - x.b) < 0.01);
        if (!hit) return;
        const it = hit.it;
        let cur = false;
        try { cur = await it.isDisabled(); } catch (e) {}
        if (cur !== x.want) transact(project, "AutoWed angles", () => [it.createSetDisabledAction(x.want)]);
      });
      await breathe();
    }
    enabledUntil = processed;
  }
  return {
    // the first piece of every clip in the range is still the original clip (no color): overwrite it whole
    async recolor() {
      if (!LABEL_OF_TRACK) return 0;
      let n = 0;
      for (const v of cutTracks) {
        const starts = await retryStale(async () => (await itemsOf(async () => (await tgt()).getVideoTrack(v)))
          .filter((x) => x.b > rangeT0 + EPS && x.a < rangeT1 - EPS && !done.has(x.a.toFixed(3))).map((x) => x.a));
        for (const a of starts) {
          if (a < rangeT0 - EPS) continue;           // a clip that starts before In keeps its own color
          try { if (await emulatedRazor(project, await tgt(), v, a, true)) n++; } catch (e) {}
        }
      }
      return n;
    },
    async apply(segments, until, final) {
      const cuts = [rangeT0];
      for (let i = 1; i < segments.length; i++) cuts.push(segments[i].start);
      if (final) cuts.push(rangeT1);
      await cutAt(cuts);
      if (STOPPING) return;
      await retryStale(() => enablePieces(segments, until));
      // keep the timeline view on the newest cut, so the edit can be watched as it happens
      try {
        const seq = await tgt();
        if (!followed) { followed = true; try { await project.openSequence(seq); } catch (e) {} }
        const last = cuts.length ? Math.min(Math.max(...cuts), rangeT1) : until;
        // moving the playhead makes Premiere redraw; only now and then, and always at the end
        if (typeof seq.setPlayerPosition === "function" && (final || Date.now() - lastFollow > 8000)) {
          lastFollow = Date.now();
          await seq.setPlayerPosition(tt(last));
        }
      } catch (e) {}
    },
  };
}

// ---------- Single camera (one camera, shot continuously) ----------
// The engine analyses the one camera and its audio. Silent stretches: markers, cut + disabled, or cut
// and removed (ripple) - Settings > Silence. Jerk / blur / exposure / walk-by / clipping / moments:
// markers only. With "new timeline" output it all happens in a subsequence of In/Out.
async function cutAudioAt(project, seq, aIndex, t) {
  // like emulatedRazor, for an audio clip: overwrite its own media from t on the same audio track;
  // a camera file's video that comes along goes to a spare video track and is removed
  const g = seq.guid.toString();
  const tpf = await ticksPerFrame(seq);
  const tT = snapTk(Math.round(t * TICKS), tpf);
  t = tT / TICKS;
  const seqNow = async () => (seq = await freshSeq(project, seq, g));
  const track = async () => (await seqNow()).getAudioTrack(aIndex);
  const first = await retryStale(async () => {
    const h = (await itemsOf(track)).find((x) => x.a < t - EPS && t + EPS < x.b);
    if (!h) return null;
    let off = false; try { off = await h.it.isDisabled(); } catch (e) {}
    let name = ""; try { name = await h.it.getName(); } catch (e) {}
    return { a: h.a, b: h.b, o: await readTI(h.it), raw: await h.it.getProjectItem(), off, name };
  });
  if (!first) return false;
  const { a, o, raw } = first;
  if (o.out > o.inp && Math.abs((o.out - o.inp) / (first.b - a) - 1) > 0.01) return false;
  const pi = ppro.ClipProjectItem.cast(raw);
  const wantInT = o.ki + (tT - o.ka);
  if (SPARE_VIDEO[g] == null) SPARE_VIDEO[g] = await (await seqNow()).getVideoTrackCount();
  const spareV = SPARE_VIDEO[g];
  const over = async (inPt, outPt) => {
    let setIO = false;
    try {
      transact(project, "AutoWed cut", () => [pi.createSetInOutPointsAction(ttk(inPt), ttk(outPt))]);
      setIO = true;
      await retryStale(async () => {
        const s = await seqNow();
        transact(project, "AutoWed cut", () => [ppro.SequenceEditor.getEditor(s).createOverwriteItemAction(raw, ttk(tT), spareV, aIndex)]);
      });
    } finally {
      if (setIO) { try { transact(project, "AutoWed cut", () => [pi.createClearInOutPointsAction()]); } catch (e) {} }
    }
  };
  const readNew = () => retryStale(async () => { const it = await findItem(track, t, null); return it ? readTI(it) : null; });
  let off = IO_OFFSET["a|" + first.name] || 0;
  await over(wantInT - off, o.ko - off);
  let done = await readNew();
  for (let k = 0; k < 2 && done && Math.abs(done.ki - wantInT) >= tpf / 2; k++) {
    off += done.ki - wantInT; IO_OFFSET["a|" + first.name] = off;
    await over(wantInT - off, o.ko - off);
    done = await readNew();
  }
  if (!done || Math.abs(done.ki - wantInT) >= tpf / 2) syncWarns.push(fmt(t));
  if (first.off) {
    try { await retryStale(async () => { const it = await findItem(track, t, null); if (it) transact(project, "AutoWed cut", () => [it.createSetDisabledAction(true)]); }); } catch (e) {}
  }
  try {
    await removeWhere(project, seqNow, async (s) => {
      if (spareV >= await s.getVideoTrackCount()) return [];
      return (await s.getVideoTrack(spareV)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
    }, ppro.Constants.MediaType.VIDEO, "AutoWed cut");
  } catch (e) {}
  await breathe();
  return true;
}

// cut every track (video and audio) that has a clip running through t
async function cutAllAt(project, seq, t) {
  const g = seq.guid.toString();
  const s = await freshSeq(project, seq, g);
  const nv = await s.getVideoTrackCount();
  for (let v = 0; v < nv; v++) { try { await emulatedRazor(project, await freshSeq(project, seq, g), v, t); } catch (e) {} }
  const na = await (await freshSeq(project, seq, g)).getAudioTrackCount();
  for (let i = 0; i < na; i++) { try { await cutAudioAt(project, await freshSeq(project, seq, g), i, t); } catch (e) {} }
}

// every clip (video + audio) lying inside [a, b]
async function itemsInside(s, a, b) {
  const out = [];
  const nv = await s.getVideoTrackCount(), na = await s.getAudioTrackCount();
  for (let v = 0; v < nv; v++) for (const x of (await s.getVideoTrack(v)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
    const xa = (await x.getStartTime()).seconds, xb = (await x.getEndTime()).seconds;
    if (xa > a - EPS * 4 && xb < b + EPS * 4) out.push({ it: x, kind: "v" });
  }
  for (let i = 0; i < na; i++) for (const x of (await s.getAudioTrack(i)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
    const xa = (await x.getStartTime()).seconds, xb = (await x.getEndTime()).seconds;
    if (xa > a - EPS * 4 && xb < b + EPS * 4) out.push({ it: x, kind: "a" });
  }
  return out;
}

async function runSingle(project, sd, range, event, evName) {
  if (!sd.cams.length) { log("In/Out කොටසේ video clip එකක් හමු නොවුණි.", "bad"); return; }
  const cam = sd.cams[0];
  if (sd.cams.length > 1) log(`Single camera: "${cam.name}" track එක විතරක් analyze කරනවා.`, "mute");
  const act = ($("x_sact") && $("x_sact").value) || "remove";
  const smin = Math.max(0.5, parseFloat(($("x_smin") && $("x_smin").value) || "2") || 2);
  const outMode = ($("x_live") && $("x_live").value) || "new";
  renderLegend([cam.name]);
  renderStrip([], range[0], range[1], range[0]);
  const ex = Object.assign(gatherExtras(), { silence: true, silence_min: smin, qc: true, trim: false });
  const body = {
    project: { name: sd.name, fps: sd.fps, master_cam: 0, range, cameras: [{ name: cam.name, clips: cam.clips }], audio_tracks: sd.audio },
    event, options: { layout: "stacked", kind: "single", extras: ex, xml_name: evName },
  };
  log(`Single camera: ${evName} · ${fmt(range[0])} → ${fmt(range[1])} · silence (${smin}s+): ${act === "remove" ? "අයින් කරනවා" : act === "disable" ? "disable" : "markers"}`);
  const job = (await call("POST", "/api/edit", body)).job;
  CUR_JOB = job;
  let shown = 0, j = null;
  while (true) {
    await sleep(1000);
    if (await stopRequested(job)) return false;
    try { j = await call("GET", "/api/job/" + job); } catch (e) { continue; }
    if (j.status === "cancelled") { log("■ නැවැත්තුවා.", "warn"); showProgress("done", j.progress || 0, null); return false; }
    showProgress(j.stage || "queued", j.progress || 0, j.eta);
    for (; shown < j.log.length; shown++) log(j.log[shown], j.log[shown].startsWith("ERROR") ? "bad" : "");
    if (j.status === "error") return false;
    if (j.status === "done") break;
  }
  let result = j.result;
  showProgress("finish", 0.99, null);
  // where: a new timeline of In/Out (default) or this timeline
  let target = sd;
  if (outMode === "new") {
    try {
      const name = `${(result.event && eventName(result.event)) || evName} (single)`;
      const guid = await openSubFor(project, sd, result.range, name);
      result = shiftResult(result, -result.range[0]);
      const sd2 = await readSequence(result.range);
      if (!sd2 || sd2.guid !== guid) throw new Error("අලුත් timeline එක active කරගන්න බැරි වුණා");
      target = sd2;
      log(`අලුත් timeline එක හැදුවා: "${name}". ඔබේ "${sd.name}" timeline එකට අත තියන්නේ නැහැ.`, "ok");
    } catch (e) { log("අලුත් timeline එක: " + (e && e.message || e), "bad"); showProgress("done", 1, null); return; }
  }
  const project2 = await ppro.Project.getActiveProject();
  const seqT = target.seq;
  // markers: problems and good moments (markers only, nothing is cut for these)
  const mk = [];
  for (const b of result.bad || []) mk.push({ name: `⚠ ${b.why || "weak"}`, start: b.start, dur: b.end - b.start });
  for (const c of result.clipping || []) mk.push({ name: "⚠ Audio clipping", start: c.start, dur: c.end - c.start });
  for (const gp of result.mic_gaps || []) mk.push({ name: "⚠ Mic drop-out", start: gp.start, dur: gp.end - gp.start });
  for (const m of result.moments || []) mk.push({ name: `★ ${m.kind || "moment"}`, start: m.time, dur: 1 });
  for (const r of result.rituals || []) mk.push({ name: `◆ ${r.label}`, start: r.start, dur: r.end - r.start });
  const sil = (result.silences || []).filter((x) => x.end - x.start >= smin - 0.01);
  if (act === "markers") for (const x of sil) mk.push({ name: "Silence", start: x.start, dur: x.end - x.start });
  const nm = await addMarkers(project2, seqT, mk);
  log(`Markers ${nm}: issues ${(result.bad || []).length}, audio ${(result.clipping || []).length + (result.mic_gaps || []).length}, moments ${(result.moments || []).length}${act === "markers" ? `, silence ${sil.length}` : ""}`, "ok");
  try { renderMoments(result); } catch (e) {}
  // silences: cut every track at both ends, then disable or remove (last one first, so times hold)
  if (act !== "markers" && sil.length) {
    try { await project2.save(); } catch (e) {}
    CALM = true; CALM_N = 0; CALM_TOTAL = 0;
    const g = seqT.guid.toString();
    let done = 0;
    for (const x of sil.slice().reverse()) {
      try {
        await cutAllAt(project2, await freshSeq(project2, seqT, g), x.end);
        await cutAllAt(project2, await freshSeq(project2, seqT, g), x.start);
        if (act === "disable") {
          const its = await retryStale(async () => itemsInside(await freshSeq(project2, seqT, g), x.start, x.end));
          for (const o of its) { try { if (!(await o.it.isDisabled())) transact(project2, "AutoWed silence", () => [o.it.createSetDisabledAction(true)]); } catch (e) {} }
        } else {
          await removeWhere(project2, () => freshSeq(project2, seqT, g), async (s) => (await itemsInside(s, x.start, x.end)).map((o) => o.it),
            ppro.Constants.MediaType.ANY != null ? ppro.Constants.MediaType.ANY : ppro.Constants.MediaType.VIDEO, "AutoWed silence", true);
        }
        done++;
        if ($("stage")) $("stage").textContent = `Silence ${done}/${sil.length}…`;
      } catch (e) { log(`Silence ${fmt(x.start)}: ` + (e && e.message || e), "warn"); }
    }
    CALM = false;
    if (act === "remove" && done) {
      // every join where silence was taken out is a jump cut: marked for a cutaway / transition
      let gone = 0;
      const joins = [];
      for (const x of sil) { joins.push({ name: "⚠ Jump cut (silence අයින් කළා)", start: x.start - gone, dur: 0.1 }); gone += x.end - x.start; }
      try { const nj = await addMarkers(project2, await freshSeq(project2, seqT, g), joins); log(`Jump cut markers ${nj}.`, "mute"); } catch (e) {}
    }
    const secs = Math.round(sil.reduce((a2, x) => a2 + x.end - x.start, 0));
    log(`Silence ${done}ක් (${secs}s) ${act === "remove" ? "අයින් කරලා ගැප්පුවා" : "cut කරලා disable කළා"}.`, "ok");
    await repaintTimeline(project2, g);
  }
  reportSyncWarns();
  showProgress("done", 1, null);
  log(`✔ ඉවරයි · single camera · ${evName}`, "ok");
  return true;
}

// ---------- live strip in the panel ----------
// same order as the clip labels AutoWed writes: Mango, Caribbean, Rose, Forest, Violet, Yellow, Cerulean, Magenta
const CAM_COLORS = ["#e8962e", "#1fc4a8", "#f2709f", "#62b356", "#a990dd", "#e3d23f", "#2d9fd6", "#d63fae"];
function renderStrip(segments, t0, t1, until) {
  const strip = $("strip");
  strip.innerHTML = "";
  const total = Math.max(0.001, t1 - t0);
  for (const s of segments) {
    const d = document.createElement("div");
    d.style.width = (Math.max(0, s.end - s.start) / total * 100).toFixed(3) + "%";
    d.style.height = "16px";
    d.style.backgroundColor = s.cam < 0 ? "#333" : CAM_COLORS[s.cam % CAM_COLORS.length];
    d.style.borderRight = "1px solid #1b1b1b";
    strip.appendChild(d);
  }
  const rest = document.createElement("div");
  rest.style.flex = "1"; rest.style.height = "16px"; rest.style.backgroundColor = "#1b1b1b";
  strip.appendChild(rest);
  $("striplbl").textContent = `Cuts ${Math.max(0, segments.length - 1)} · ${fmt((until || t0) - t0)} / ${fmt(total)}`;
  renderLanes(segments, t0, t1, until);
}

// live timeline: one lane per camera, the chosen pieces drawn where they fall in the In/Out range
let CAMN = [];
function renderLanes(segments, t0, t1, until) {
  const box = $("lanes");
  if (!box) return;
  const total = Math.max(0.001, t1 - t0);
  box.innerHTML = "";
  const n = Math.max(CAMN.length, ...segments.map((x) => x.cam + 1), 0);
  const mk = (label, color) => {
    const row = document.createElement("div"); row.className = "lane";
    const ln = document.createElement("span"); ln.className = "ln"; ln.textContent = label; ln.style.color = color;
    const lt = document.createElement("div"); lt.className = "lt";
    row.appendChild(ln); row.appendChild(lt); box.appendChild(row);
    return lt;
  };
  const head = until != null ? Math.max(0, Math.min(1, ((until || t0) - t0) / total)) : 0;
  for (let c = 0; c < n; c++) {
    const lt = mk("CAM " + (c + 1), CAM_COLORS[c % CAM_COLORS.length]);
    for (const sg of segments) {
      if (sg.cam !== c) continue;
      const d = document.createElement("div"); d.className = "sg";
      d.style.left = ((sg.start - t0) / total * 100).toFixed(3) + "%";
      d.style.width = (Math.max(0.15, (sg.end - sg.start) / total * 100)).toFixed(3) + "%";
      d.style.backgroundColor = CAM_COLORS[c % CAM_COLORS.length];
      lt.appendChild(d);
    }
    const ph = document.createElement("div"); ph.className = "ph"; ph.style.left = (head * 100).toFixed(2) + "%"; lt.appendChild(ph);
  }
  $("tk0").textContent = fmt(t0); $("tk1").textContent = fmt((t0 + t1) / 2); $("tk2").textContent = fmt(t1);
  const real = segments.filter((x) => x.cam >= 0);
  $("t_cuts").textContent = String(Math.max(0, real.length - 1));
  $("t_avg").textContent = real.length ? (real.reduce((a, x) => a + x.end - x.start, 0) / real.length).toFixed(1) + "s" : "–";
}
function renderLegend(names) {
  CAMN = names.slice();
  $("legend").innerHTML = "";
  names.forEach((n, i) => {
    const sw = document.createElement("span");
    sw.textContent = "■ " + n;
    sw.style.color = CAM_COLORS[i % CAM_COLORS.length];
    sw.style.marginRight = "10px";
    $("legend").appendChild(sw);
  });
}

// ---------- multicam: nest the In/Out part of the timeline in place ----------
async function camerasInsideNest(project, sd, nest) {
  return retryStale(async () => {
    // after a failed try the nest clip object may be dead: find it again on its track
    const it = await locate(async () => (await freshSeq(project, sd.seq, sd.guid)).getVideoTrack(nest.track), [nest.start, nest.end]) || nest.it;
    return camerasInsideNestItem({ ...nest, it });
  });
}
async function camerasInsideNestItem(nest) {
  // cameras = video tracks of the nested sequence, mapped to the outer timeline's time
  const pi = ppro.ClipProjectItem.cast(await nest.it.getProjectItem());
  const nseq = await pi.getSequence();
  const d = nest.start - nest.srcIn;                       // outer time = nest time + d
  const cams = [];
  const vCount = await nseq.getVideoTrackCount();
  for (let t = 0; t < vCount; t++) {
    const track = await nseq.getVideoTrack(t);
    const clips = [];
    for (const it of track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
      const c = await readItem(it);
      if (!c.path || Math.abs(c.speed - 1) > 0.001) continue;
      const s = Math.max(c.start + d, nest.start), e = Math.min(c.end + d, nest.end);
      if (e - s < 0.5) continue;
      clips.push({ path: c.path, proxy: c.proxy || undefined, offset: c.start - c.srcIn + d, src_in: c.srcIn, tl_start: s, tl_end: e });
    }
    if (clips.length) cams.push({ name: track.name || `V${t + 1}`, track: t, clips });
  }
  return { nseq, nseqName: nseq.name, nseqGuid: nseq.guid.toString(), d, cams };
}

async function selectionOf(items) {
  let sel = null;
  ppro.TrackItemSelection.createEmptySelection((s) => { sel = s; });
  for (let i = 0; i < 20 && !sel; i++) await sleep(50);
  for (const it of items) sel.addItem(it, true);
  return sel;
}

async function nestInPlace(project, sd, range) {
  // 1) the inside of the nest: a subsequence of In..Out (all tracks)
  const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
  let nseq = await sd.seq.createSubsequence(true);
  if (!nseq) nseq = await findNewSequence(project, before);
  if (!nseq) throw new Error("nest (subsequence) හදන්න බැරි වුණා");
  const nGuid = nseq.guid.toString();
  const nName = "AutoWed Nest";
  await renameSequence(project, nseq, nName);
  const seqG = sd.seq.guid.toString();
  const outer = async () => (await project.getSequences()).find((x) => x.guid.toString() === seqG) || sd.seq;
  const native = hasNativeRazor(await outer());
  const camTracks = sd.cams.map((c) => c.track);
  // 2) clear the camera video between In and Out (audio is not touched)
  for (const t of range) await razorTracks(project, await outer(), camTracks, t, native);
  const victims = [];
  for (const ti of camTracks) {
    for (const x of await itemsOf(async () => (await outer()).getVideoTrack(ti))) {
      if (x.a >= range[0] - EPS && x.b <= range[1] + EPS) victims.push(x.it);
    }
  }
  if (victims.length) await removeItems(project, await outer(), victims, ppro.Constants.MediaType.VIDEO);
  // 3) drop the nest on the first camera track at In; its audio goes to a new track and is removed
  const vIdx = Math.min(...camTracks);
  const o2 = await outer();
  const aCount = await o2.getAudioTrackCount();
  const nFresh = (await project.getSequences()).find((x) => x.guid.toString() === nGuid) || nseq;
  const nestItem = await nFresh.getProjectItem();
  transact(project, "AutoWed nest", () => [ppro.SequenceEditor.getEditor(o2).createOverwriteItemAction(nestItem, tt(range[0]), vIdx, aCount)]);
  await removeWhere(project, outer, async (s) => {
    const out = [];
    const aNow = await s.getAudioTrackCount();
    for (let i = aCount; i < aNow; i++) for (const x of (await s.getAudioTrack(i)).getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) out.push(x);
    return out;
  }, ppro.Constants.MediaType.AUDIO, "AutoWed nest");
  sd.seq = await outer();
  const placed = (await itemsOf(async () => (await outer()).getVideoTrack(vIdx))).find((x) => Math.abs(x.a - range[0]) < 0.05);
  if (!placed) throw new Error("nest clip timeline එකට දාන්න බැරි වුණා");
  log(`In/Out කොටස nest කළා: "${nName}" (V${vIdx + 1})`, "ok");
  const srcIn = await retryStale(async () => { const it = await locate(async () => (await outer()).getVideoTrack(vIdx), [placed.a, placed.b]); return (await it.getInPoint()).seconds; });
  return { track: vIdx, it: placed.it, start: placed.a, end: placed.b, srcIn, name: nName, guid: nGuid };
}

function eventName(id) {
  for (const o of $("event").options || []) if (o.value === id) return o.textContent.split("  ·  ")[0];
  return id;
}

// ---------- the main flow ----------
async function autoEdit() {
  if (busy) return;
  busy = true; $("go").disabled = true; showStop(true);
  LAST_DONE = ""; LAST_BAD = "";
  const ev = eventName(($("event") && $("event").value) || "auto");
  let ok = false;
  try { ok = await runAutoEdit(); }
  catch (e) { log("Error: " + (e && e.message || e), "bad"); }
  finally { busy = false; $("go").disabled = false; showStop(false); }
  if (ok) notifyPhone(`✔ ${ev} edit එක ඉවරයි`, LAST_DONE || "Auto edit ඉවරයි.", "white_check_mark");
  else if (!STOPPING) notifyPhone(`✖ ${ev} edit එක ඉවර වුණේ නැහැ`, LAST_BAD || "Panel එකේ Log එක බලන්න.", "warning", "high");
}

// ---------- phone notifications (ntfy) ----------
// The engine posts to ntfy.sh; on the phone: install the free "ntfy" app and subscribe to the topic.
function ntfyTopic() {
  let t = "";
  try { t = localStorage.getItem("autowed.ntfy") || ""; } catch (e) {}
  if (!t) {
    t = "autowed-" + Math.random().toString(36).slice(2, 10);
    try { localStorage.setItem("autowed.ntfy", t); } catch (e) {}
  }
  return t;
}
function notifyOn() { return !$("x_ntfy_on") || $("x_ntfy_on").value !== "off"; }
async function notifyPhone(title, message, tags, priority) {
  if (!notifyOn()) return false;
  const topic = ($("x_ntfy") && $("x_ntfy").value.trim()) || ntfyTopic();
  try {
    const r = await call("POST", "/api/notify", { topic, title, message, tags: tags || "clapper", priority: priority || "default" });
    return !!(r && r.ok);
  } catch (e) { return false; }
}

// ---------- Stop / Reset ----------
let CUR_JOB = null, STOPPING = false;
function showStop(on) {
  if ($("stop")) $("stop").style.display = on ? "flex" : "none";
  if ($("go")) $("go").className = "go" + (on ? " dis" : "");
  if (!on) { STOPPING = false; CUR_JOB = null; }
}
async function stopRequested(job) {
  if (!STOPPING) return false;
  try { await call("POST", "/api/job/" + job + "/cancel", {}, 5000); } catch (e) {}
  log("■ නැවැත්තුවා. Timeline එකේ දැනටමත් වැටුණ cuts එහෙමම තියෙනවා (ඕන නම් Ctrl+Z).", "warn");
  showProgress("done", 0, null);
  return true;
}
function stopNow() {
  if (!busy) return;
  STOPPING = true;
  log("■ Stop ඔබුවා: engine එක නවත්තනවා…", "warn");
  if (CUR_JOB) call("POST", "/api/job/" + CUR_JOB + "/cancel", {}, 5000).catch(() => {});
}
async function resetPanel() {
  STOPPING = true;
  try { await call("POST", "/api/reset", {}, 5000); } catch (e) {}
  await sleep(1200);                    // running loops see STOPPING and end
  busy = false; STOPPING = false; CUR_JOB = null;
  for (const id of ["go", "sync", "qrun", "evfind", "rtest", "read"]) if ($(id)) $(id).disabled = false;
  showStop(false);
  for (const id of ["strip", "legend", "lanes", "minilog"]) if ($(id)) $(id).innerHTML = "";
  if ($("pct")) $("pct").textContent = "0%";
  if ($("stage")) $("stage").textContent = "Ready";
  if ($("eta")) $("eta").textContent = "";
  for (const [id, v] of [["t_cuts", "0"], ["t_avg", "–"], ["t_bpm", "–"], ["t_mom", "0"]]) if ($(id)) $(id).textContent = v;
  for (const id of ["striplbl", "syncinfo"]) if ($(id)) $(id).textContent = "";
  if ($("log")) $("log").innerHTML = "";
  for (const q of QUEUE) if (q.status === "running") q.status = "";
  try { qSave(); qRender(); } catch (e) {}
  log("↺ Reset: panel එක මුල් තත්වයට ආවා, engine එකේ run වෙන jobs නැවැත්තුවා.", "ok");
  try { await ping(); } catch (e) {}
}

async function runAutoEdit(jobOpts) {
  // jobOpts (batch): { range, event, kind } instead of the panel's In/Out and selects
  jobOpts = jobOpts || {};
  const sd = await readSequence(jobOpts.range);
  if (!sd) return false;
  if (!sd.hasInOut) { log("මුලින්ම timeline එකේ edit කරන්න ඕන කොටසට In (I) සහ Out (O) දාන්න. AutoWed edit කරන්නේ ඒ කොටස විතරයි.", "bad"); return; }
  if (!(await ping())) {
    log("Engine එකට සම්බන්ධ වෙන්න බැහැ. Start menu › AutoWed › AutoWed Engine run කරලා ආයෙ උත්සාහ කරන්න.", "bad");
    log("විස්තර: " + lastPingErr, "mute");
    return;
  }
  const project = await ppro.Project.getActiveProject();
  sd.guid = sd.seq.guid.toString();
  const kind = "stacked";   // only manual edit (multicam and single camera were removed)
  const range = [sd.inT, sd.outT];
  const event = jobOpts.event || $("event").value || "auto";
  const evName = eventName(event);
  showProgress("prepare", 0, null);

  let cams, applier = null, nest = null, nestInfo = null;
  // output: "new" = your timeline is not touched, the finished edit opens as a new timeline (default);
  // "live" / "end" = cuts in your own timeline (live, or all at the end)
  const outMode = ($("x_live") && $("x_live").value) || "new";
  if (outMode === "new") {
    if (sd.nestInRange) {
      nest = sd.nestInRange;
      nestInfo = await camerasInsideNest(project, sd, nest);
      cams = nestInfo.cams;
      nest = null;                       // only read: nothing in your timeline changes
    } else cams = sd.cams;
    if (cams.length < 2) { log("කැමරා tracks 2ක් වත් හමු නොවුණි (එක් කැමරාවකට එක video track එකක් ඕන).", "bad"); return; }
    log(`ඔබේ timeline එකට අත තියන්නේ නැහැ. Cuts මෙතන live පේනවා; ඉවර වුණාම timeline එකේ copy එකක් හදලා එකේ cuts දානවා (${fmt(range[0])} → ${fmt(range[1])}).`, "ok");
  } else {
    const st = await setupInPlace(project, sd, kind, range);
    if (!st) return;
    ({ applier, nest, nestInfo, cams } = st);
  }
  renderLegend(cams.map((c, i) => `Cam ${i + 1}`));
  renderStrip([], range[0], range[1], range[0]);
  // a run that crashed while its cuts were going in: put in the rest, no new analysis
  if (outMode === "new") {
    let pend = null;
    try { pend = JSON.parse(localStorage.getItem("autowed.pending") || "null"); } catch (e) {}
    if (pend && pend.origGuid === sd.guid && pend.kind === kind && Math.abs(pend.range[0] - range[0]) < 0.05 && Math.abs(pend.range[1] - range[1]) < 0.05 && pend.result) {
      log("කලින් run එකේ අලුත් timeline එකට cuts දාලා ඉවර වුණේ නැහැ: analysis නැතුව ඉතුරු ටික දානවා…", "ok");
      await finishInCopy(project, sd, kind, pend.result, evName, pend.guid);
      return true;
    }
  }
  if (outMode === "end" && applier) {
    let pend = null;
    try { pend = JSON.parse(localStorage.getItem("autowed.pending") || "null"); } catch (e) {}
    if (pend && pend.guid === sd.guid && pend.kind === kind && Math.abs(pend.range[0] - range[0]) < 0.05 && Math.abs(pend.range[1] - range[1]) < 0.05 && pend.result) {
      log("කලින් run එකේ cuts දාලා ඉවර වුණේ නැහැ: analysis නැතුව ඉතුරු cuts ටික දානවා…", "ok");
      await finish(project, kind, sd, nest, nestInfo, applier, cams, pend.result, true);
      return true;
    }
  }

  const body = {
    project: { name: sd.name, fps: sd.fps, master_cam: 0, range,
               cameras: cams.map((c) => ({ name: c.name, clips: c.clips })), audio_tracks: sd.audio },
    event,
    options: { min_shot: numOrNull("min_shot"), max_shot: numOrNull("max_shot"), layout: "stacked", kind,
               extras: gatherExtras(),
               xml_name: evName },
  };
  log(`Auto edit: ${evName} · ${kind === "multicam" ? "Multicamera" : "Manual cutting"} · ${fmt(range[0])} → ${fmt(range[1])}`);
  const job = (await call("POST", "/api/edit", body)).job;
  CUR_JOB = job;
  let shown = 0, lastSegCount = -1, applyFails = 0, lastApply = 0, appliedCount = 0;
  const live = outMode === "live";
  if (outMode === "end") log("Live cuts OFF: analysis ඉවර වුණාම cuts ඔක්කොම එකපාර දානවා (Premiere එකට ආරක්ෂිතයි).", "mute");
  while (true) {
    await sleep(1000);
    let j;
    if (await stopRequested(job)) return false;
    try { j = await call("GET", "/api/job/" + job); } catch (e) { continue; }
    if (j.status === "cancelled") { log("■ නැවැත්තුවා. Timeline එකේ දැනටමත් වැටුණ cuts එහෙමම තියෙනවා (ඕන නම් Ctrl+Z).", "warn"); showProgress("done", j.progress || 0, null); return false; }
    showProgress(j.stage || "queued", j.progress || 0, j.eta);
    for (; shown < j.log.length; shown++) log(j.log[shown], j.log[shown].startsWith("ERROR") ? "bad" : "");
    if (j.segments && j.segments.length !== lastSegCount) {
      lastSegCount = j.segments.length;
      renderStrip(j.segments, range[0], range[1], j.committed_until || range[0]);
    }
    // live cuts in calm batches: at least 6 s apart and a few new pieces, never one per poll
    const newPieces = (j.segments || []).length - appliedCount;
    if (live && applier && j.status !== "done" && newPieces > 0 && (Date.now() - lastApply > 6000) && (newPieces >= 3 || Date.now() - lastApply > 15000)) {
      appliedCount = j.segments.length; lastApply = Date.now();
      {
        try { await applier.apply(j.segments, j.committed_until || range[0], false); applyFails = 0; }
        catch (e) {
          // one bad round does not end the live edit: the next round picks up from where it is
          applyFails++;
          log("Timeline cut: " + (e && e.message || e) + (applyFails < 4 ? " — ඊළඟ වටයේ ආයෙ උත්සාහ කරනවා" : ""), "warn");
          if (e && e.stack) log("  " + String(e.stack).split("\n").slice(0, 4).join(" | "), "mute");
          if (applyFails >= 4) {
            log("Live cuts නවත්තලා analysis ඉවර වුණාම cuts ඔක්කොම එකපාර දානවා.", "warn");
            lastApply = Infinity;
          }
        }
      }
    }
    if (j.status === "error") return false;
    if (j.status === "done") {
      if (outMode === "new") { await finishInCopy(project, sd, kind, j.result, evName, null); return true; }
      await finish(project, kind, sd, nest, nestInfo, applier, cams, j.result, outMode === "end"); return true;
    }
  }
}

// one marker per transaction (Premiere 26.5 invalidates objects after every edit)
async function addMarkers(project, seq, list) {
  const g = seq.guid.toString();
  let n = 0;
  for (const m of list) {
    try {
      await retryStale(async () => {
        const markers = await ppro.Markers.getMarkers(await freshSeq(project, seq, g));
        transact(project, "AutoWed markers", () => [markers.createAddMarkerAction(m.name, m.type || "Comment",
          tt(m.start), tt(Math.max(0.1, m.dur || 0.1)), m.comment || "AutoWed")]);
      });
      n++;
    } catch (e) { log("Marker: " + (e && e.message || e), "warn"); }
  }
  return n;
}

async function addDeadMarkers(project, seq, result) {
  if (!result.dead || !result.dead.length) return;
  await addMarkers(project, seq, result.dead.map((d) => ({ name: "CHECK – කැමරා සියල්ල දුර්වලයි", start: d.start, dur: d.end - d.start })));
}

function stamp() {
  const d = new Date(), p2 = (n) => String(n).padStart(2, "0");
  return `${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}`;
}

// a copy of the finished edit as its own sequence (from the engine's XML), next to the in-place edit
async function importCopy(proj, result) {
  // Premiere 26 fails ("project appears to be damaged") on an FCP XML with more than ~25 clips and its
  // importFiles still answers true, so the copy is no longer imported automatically: only its path is shown
  if (!result || !result.xml) return;
  log("Silence cut / white flash / mic / loudness තියෙන XML copy එක file එකක් විදිහට save කළා (Premiere 26 එකට clips 25කට වැඩි XML import කරන්න බැහැ): " + result.xml, "mute");
}


// Cutting in a timeline (the user's own, or AutoWed's copy of it): stacked = cut every camera track and
// enable the chosen one; multicam = one nest clip in the range, cut and its angles set by prproj patch
async function setupInPlace(project, sd, kind, range) {
  let applier, nest = null, nestInfo = null, cams;
  if (kind === "stacked") {
    if (sd.cams.length < 2) { log("Manual cutting එකට කැමරා tracks 2ක් වත් ඕන (nest එකක් නෙවෙයි).", "bad"); return null; }
    cams = sd.cams;
    applier = makeApplier(project, sd.seq, "stacked", cams.map((c) => c.track), cams.map((c) => c.track), range[0], range[1]);
    LABEL_OF_TRACK = tlColors ? Object.fromEntries(cams.map((c, i) => [c.track, CAM_LABEL_IDX[i % CAM_LABEL_IDX.length]])) : null;
    log(`"${sd.name}" timeline එකේ, ${fmt(range[0])} → ${fmt(range[1])} අතර cuts…`, "ok");
  } else {
    nest = sd.nestInRange;
    if (nest) log(`Nest එක පාවිච්චි කරනවා: "${nest.name}"`, "ok");
    else {
      if (sd.cams.length < 2) { log("කැමරා tracks 2ක් වත් හමු නොවුණි.", "bad"); return null; }
      nest = await nestInPlace(project, sd, range);
    }
    nestInfo = await camerasInsideNest(project, sd, nest);
    cams = nestInfo.cams;
    if (cams.length < 2) { log("Nest එක ඇතුළේ කැමරා tracks 2ක් වත් හමු නොවුණි.", "bad"); return null; }
    applier = makeApplier(project, sd.seq, "multicam", [nest.track], [], Math.max(range[0], nest.start), Math.min(range[1], nest.end));
    log(`Multicam: nest clip එක "${sd.name}" timeline එකේ cut වෙනවා…`, "ok");
  }
  return { applier, nest, nestInfo, cams };
}

// Default output. Premiere 26 cannot import an FCP XML with more than ~25 clips, so the new timeline is
// a copy of the editor's sequence made by Premiere itself; the cuts go into that copy in calm batches
// after the analysis (project saved first). The editor's own timeline is never touched.

// Premiere 26 does not repaint the timeline after many scripted edits (clips show only under the mouse):
// switching to another sequence and back forces a full redraw.
async function repaintTimeline(project, guid) {
  try {
    const all = await project.getSequences();
    const me = all.find((x) => x.guid.toString() === guid);
    const other = all.find((x) => x.guid.toString() !== guid);
    if (!me) return;
    if (other) { await project.openSequence(other); await sleep(500); }
    const again = (await project.getSequences()).find((x) => x.guid.toString() === guid) || me;
    await project.openSequence(again);
  } catch (e) {}
}

let PEND_ORIG = null;
// the new timeline = a Premiere subsequence of In..Out (only the clips in the range, all tracks, starting
// at 00:00). The editor's own timeline is not edited (its In/Out are put back if a batch range needed others).
async function openSubFor(project, sd, range, name) {
  let seq = await freshSeq(project, sd.seq, sd.guid);
  let io = null;
  try { io = [(await seq.getInPoint()).seconds, (await seq.getOutPoint()).seconds]; } catch (e) {}
  const needIO = !io || Math.abs(io[0] - range[0]) > 0.01 || Math.abs(io[1] - range[1]) > 0.01;
  if (needIO) {
    transact(project, "AutoWed range", () => [seq.createSetInPointAction(tt(range[0]))]);
    seq = await freshSeq(project, sd.seq, sd.guid);
    transact(project, "AutoWed range", () => [seq.createSetOutPointAction(tt(range[1]))]);
    seq = await freshSeq(project, sd.seq, sd.guid);
  }
  const before = new Set((await project.getSequences()).map((x) => x.guid.toString()));
  let ns = null;
  try { await seq.createSubsequence(true); } catch (e) {}
  ns = await findNewSequence(project, before, 15000);
  if (needIO && io) {
    try {
      seq = await freshSeq(project, sd.seq, sd.guid);
      transact(project, "AutoWed range", () => [seq.createSetInPointAction(tt(io[0]))]);
      seq = await freshSeq(project, sd.seq, sd.guid);
      transact(project, "AutoWed range", () => [seq.createSetOutPointAction(tt(io[1]))]);
    } catch (e) {}
  }
  if (!ns) throw new Error("In/Out කොටසේ අලුත් timeline එක (subsequence) හැදුණේ නැහැ");
  const ng = ns.guid.toString();
  ns = (await project.getSequences()).find((x) => x.guid.toString() === ng) || ns;
  await renameSequence(project, ns, name);
  ns = (await project.getSequences()).find((x) => x.guid.toString() === ns.guid.toString()) || ns;
  await project.openSequence(ns);
  await sleep(800);
  return ns.guid.toString();
}

// every time in an engine result moved by d seconds (the subsequence starts at 00:00, not at In)
function shiftResult(r, d) {
  const x = JSON.parse(JSON.stringify(r));
  const sh = (o, keys) => { for (const k of keys) if (typeof o[k] === "number") o[k] += d; };
  x.range = [r.range[0] + d, r.range[1] + d];
  if (x.trim) x.trim = [x.trim[0] + d, x.trim[1] + d];
  for (const k of ["segments", "dead", "bad", "clipping", "mic_gaps", "silences", "rituals"]) for (const o of x[k] || []) sh(o, ["start", "end"]);
  for (const k of ["moments", "markers", "jumps"]) for (const o of x[k] || []) sh(o, ["time"]);
  for (const o of x.mic_choice || []) sh(o, ["start", "end"]);
  if (x.color && x.color.pieces) for (const o of x.color.pieces) sh(o, ["start", "end"]);
  x.shifted = true;
  return x;
}

async function finishInCopy(project, sd, kind, result, evName, resumeGuid) {
  let guid = resumeGuid;
  const origRange = result.shifted ? null : result.range.slice();
  if (!result.shifted) result = shiftResult(result, -result.range[0]);
  const range = result.range;
  try {
    if (!guid) {
      const name = (result.event && eventName(result.event)) || evName;   // simple: the event name only, no numbers
      guid = await openSubFor(project, sd, origRange, name);
      log(`අලුත් timeline එක හැදුවා: "${name}" (In/Out කොටසේ clips විතරයි). ඔබේ "${sd.name}" timeline එකට අත තියන්නේ නැහැ.`, "ok");
    } else {
      const ns = (await project.getSequences()).find((x) => x.guid.toString() === guid);
      if (!ns) throw new Error("කලින් හදපු timeline copy එක හමු නොවුණි");
      await project.openSequence(ns); await sleep(800);
    }
  } catch (e) {
    log("අලුත් timeline එක හදන්න බැරි වුණා: " + (e && e.message || e), "bad");
    showProgress("done", 1, null);
    return;
  }
  const sd2 = await readSequence(range);
  if (!sd2 || sd2.guid !== guid) { log("අලුත් timeline එක active කරගන්න බැරි වුණා.", "bad"); showProgress("done", 1, null); return; }
  const st = await setupInPlace(project, sd2, kind, range);
  if (!st) return;
  PEND_ORIG = sd.guid;
  try { await finish(project, kind, sd2, st.nest, st.nestInfo, st.applier, st.cams, result, true); }
  finally { PEND_ORIG = null; }
}

// (old) XML output: the finished edit (trims, silence cuts, white flash, mic, loudness, camera colors,
// markers) comes in as a new timeline and opens; your own timeline is never edited, so nothing to crash
async function finishNew(project, sd, cams, result, kind) {
  showProgress("finish", 0.99, null);
  renderStrip(result.segments, result.range[0], result.range[1], result.range[1]);
  reportSyncWarns();
  await qcReportOnly(result);
  saveLastRun(sd, cams, result, kind);
  if (!result.xml) { log("Engine එකෙන් timeline file එක ආවේ නැහැ.", "bad"); showProgress("done", 1, null); return; }
  const before = new Set((await project.getSequences()).map((x) => x.guid.toString()));
  await importCopy(project, result);
  try {
    const ns = await findNewSequence(project, before, 15000);
    if (ns) { await project.openSequence(ns); log(`අලුත් timeline එක open කළා: "${ns.name}"`, "ok"); }
  } catch (e) {}
  const s = result.stats;
  showProgress("done", 1, null);
  log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s · ඔබේ මුල් timeline එක වෙනස් වුණේ නැහැ.`, "ok");
}

async function finish(project, kind, sd, nest, nestInfo, applier, cams, result, calm) {
  showProgress("finish", 0.99, null);
  renderStrip(result.segments, result.range[0], result.range[1], result.range[1]);
  const s = result.stats;
  if (applier && calm) {
    // all cuts after the analysis: save first (a crash loses nothing), remember the result so the
    // next Auto Edit on the same In/Out only puts in what is missing, then cut in calm batches
    try { await project.save(); log("Project එක save කළා. දැන් timeline එකේ cuts දානවා (කොටස් 20 බැගින්)…", "ok"); } catch (e) {}
    try { localStorage.setItem("autowed.pending", JSON.stringify({ guid: sd.guid, origGuid: PEND_ORIG, kind, range: result.range, result })); } catch (e) {}
    CALM = true; CALM_N = 0;
    const nCuts = result.segments.length + 1;
    CALM_TOTAL = nCuts * (kind === "stacked" ? cams.length : 2) + (kind === "stacked" ? result.segments.length * (cams.length - 1) : 0);
  }
  if (applier) {
    try { await applier.apply(result.segments, result.range[1], true); }
    catch (e) {
      await sleep(400);
      try { await applier.apply(result.segments, result.range[1], true); }
      catch (e2) { log("Timeline cut: " + (e2 && e2.message || e2), "bad"); applier = null; }
    }
  }
  if (applier) await repaintTimeline(project, sd.guid);
  if (calm) {
    CALM = false; CALM_TOTAL = 0;
    if (applier) try { localStorage.removeItem("autowed.pending"); } catch (e) {}
  }
  reportSyncWarns();
  sd.seq = await freshSeq(project, sd.seq, sd.guid);
  await addDeadMarkers(project, sd.seq, result);
  await qcReport(project, sd, result, cams, kind);
  saveLastRun(sd, cams, result, kind);
  if (kind === "stacked" && applier) await eventTransitions(project, sd, cams, result);
  if (kind === "stacked") {
    if (!applier) {
      log("Timeline එකේ cut කරන්න බැරි වුණ නිසා ඔබේ timeline එකට තවත් වෙනස්කම් කළේ නැහැ. Edit එක copy sequence එකේ තියෙනවා.", "warn");
      await importCopy(project, result);
      log("Razor test එක ඔබලා ඒකේ screenshot එකක් එවන්න.", "mute");
      showProgress("done", 1, null);
      return;
    }
    // camera colors were put on while cutting (project item label, see withLabel): no close/reopen
    await stabilizePieces(project, sd, cams, result, "stacked");
    try { await colorMatch(project, sd, cams, result); } catch (e) { log("Color match: " + (e && e.message || e), "warn"); }
    let proj = project;
    if (LABEL_OF_TRACK) {
      try { const n = await applier.recolor(); log(`Camera colors දැම්මා (Premiere close නොකර)${n ? ` · clip මුල් ${n}` : ""}.`, "ok"); } catch (e) {}
      LABEL_OF_TRACK = null;
    }
    await importCopy(proj, result);
    showProgress("done", 1, null);
    log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s · angle මාරු කරන්න කෑල්ල select කරලා Shift+E.`, "ok");
    return;
  }
  // multicam: angles can only be written into the project file -> save, close, write, reopen
  log("Multicam angles ලියනවා: project එක save කරලා තත්පර කිහිපයකට close කරනවා…");
  const path = project.path;
  const seqName = sd.name;
  if (!(await project.save())) log("Project save වුණේ නැද්ද කියලා සැකයි – දිගටම යනවා", "warn");
  // trimmed head / tail (-1) can't be an angle: keep the neighbouring camera there (marked above)
  const segsFixed = result.segments.map((x, i, a) => x.cam >= 0 ? x : { ...x, cam: (a[i + 1] && a[i + 1].cam >= 0 ? a[i + 1].cam : (a[i - 1] ? a[i - 1].cam : 0)) });
  const segsNest = segsFixed.map((x) => ({ ...x, start: x.start - nestInfo.d, end: x.end - nestInfo.d }));
  const opts = ppro.CloseProjectOptions();
  try { opts.setPromptIfDirty(false); opts.setShowCancelButton(false); } catch (e) {}
  await project.close(opts);
  let r;
  try {
    r = await call("POST", "/api/prproj/multicam", {
      project_path: path, in_place: true, edit_seq: seqName, nest_seq: nestInfo.nseqName,
      segments: segsNest, cam_tracks: cams.map((c) => c.track),
    }, 120000);
  } catch (e) { r = { error: String(e) }; }
  const opened = await ppro.Project.open(path);
  try {
    const seqs = await opened.getSequences();
    const e = seqs.find((x) => x.name === seqName);
    if (e) await opened.openSequence(e);
  } catch (e) {}
  await importCopy(opened, result);
  try {   // multicam pieces can't take Warp Stabilizer directly: shaky ones get markers
    const e = (await opened.getSequences()).find((x) => x.name === seqName);
    if (e) await stabilizePieces(opened, { seq: e, guid: e.guid.toString() }, cams, result, "multicam");
  } catch (e) {}
  if (r.error) { log("Multicam angles ලියන්න බැරි වුණා: " + r.error + " (project එක වෙනස් වුණේ නැහැ)", "bad"); return; }
  showProgress("done", 1, null);
  log(`✔ ඉවරයි · multicam pieces ${r.report.multicam} · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s`, "ok");
  log(`Backup: ${r.backup}`, "mute");
  log("Angles මාරු කරන්න: Program Monitor › Multi-Camera (Shift+0).", "ok");
}

// ---------- razor test ----------
async function razorTest() {
  if (busy) return;
  busy = true;
  try {
    const project = await ppro.Project.getActiveProject();
    const seq = project && (await project.getActiveSequence());
    if (!seq) { log("Test එකට sequence එකක් open කරන්න.", "bad"); return; }
    let test = await cloneSequence(project, seq, await uniqueName(project, `${seq.name} - AutoWed Razor Test`));
    const testG = test.guid.toString();
    const T_ = async () => (test = await freshSeq(project, test, testG));
    await T_();
    await project.openSequence(test);
    const native = hasNativeRazor(test);
    // the longest clip on any video track
    let best = null, bestTrack = 0;
    const vc = await test.getVideoTrackCount();
    for (let t = 0; t < vc; t++) for (const x of await itemsOf(await test.getVideoTrack(t))) {
      if (!best || x.b - x.a > best.b - best.a) { best = x; bestTrack = t; }
    }
    if (!best) { log("Video clip එකක් නැහැ.", "bad"); return; }
    const aCount = async () => { await T_(); let n = 0; const c = await test.getAudioTrackCount(); for (let i = 0; i < c; i++) n += (await itemsOf(await test.getAudioTrack(i))).length; return n; };
    const audioBefore = await aCount();
    const o = await readTI(best.it);
    const rate = o.out > o.inp ? (o.out - o.inp) / (o.b - o.a) : 1;
    let fps = 25;
    try { const tb = parseFloat(await test.getTimebase()); if (tb > 0) fps = TICKS / tb; } catch (e) {}
    const pts = [0.25, 0.5, 0.8].map((f) => Math.round((o.a + (o.b - o.a) * f) * fps) / fps);
    log(`Razor test (${native ? "Premiere razor" : "clone + trim"}) · V${bestTrack + 1} clip ${o.a.toFixed(2)}–${o.b.toFixed(2)}s · source ${o.inp.toFixed(2)}–${o.out.toFixed(2)}s · speed ${rate.toFixed(3)}`);
    const before = (await itemsOf(await test.getVideoTrack(bestTrack))).length;
    for (const t of pts) await razorTracks(project, test, [bestTrack], t, native);
    await T_();
    const after = (await itemsOf(await test.getVideoTrack(bestTrack))).filter((x) => x.a >= o.a - EPS && x.b <= o.b + EPS);
    let ok = (await itemsOf(await test.getVideoTrack(bestTrack))).length === before + 3 && after.length === 4;
    for (const p of after) {
      const r = await readTI(p.it);
      const expect = o.inp + (p.a - o.a) * rate;
      const good = Math.abs(r.inp - expect) < 0.01;
      ok = ok && good;
      log(`  ${p.a.toFixed(3)}–${p.b.toFixed(3)}s · source in ${r.inp.toFixed(3)} (හරි අගය ${expect.toFixed(3)}) ${good ? "✔" : "✘"}`, good ? "ok" : "bad");
    }
    const audioAfter = await aCount();
    if (audioAfter !== audioBefore) { ok = false; log(`  Audio clips ${audioBefore} → ${audioAfter}: audio වලටත් බලපෑවා ✘`, "bad"); }
    else log("  Audio clips වෙනස් වුණේ නැහැ ✔", "ok");
    log(ok ? "✔ Razor test සාර්ථකයි. Timeline එකේ live cuts වැඩ කරයි." : "✘ Razor test අසාර්ථකයි. මේ log එකේ screenshot එකක් එවන්න.", ok ? "ok" : "bad");
    log(`Test sequence: "${test.name}" (බලලා delete කරන්න).`, "mute");
  } catch (e) { log("Razor test error: " + (e && e.message || e), "bad"); }
  finally { busy = false; }
}

// ---------- wiring ----------
const HINTS = {
  multicam: "In/Out කොටසේ කැමරා nest කරලා (නැත්නම් ඔබේ nest එක) ඒ timeline එකේම multicam cuts. ඉවර වුණාම angles ලියන්න project එක තත්පර කිහිපයකට close වෙලා ආයෙ open වෙනවා.",
  stacked: "In/Out කොටසේ කැමරා tracks ඒ timeline එකේම live cut කරලා, තෝරපු කැමරාව විතරක් enable. Angle මාරු කරන්න Shift+E.",
  single: "එක කැමරාවක්: jerk / blur / walk-by / audio ප්‍රශ්න markers විදිහට; silence කොටස් Settings › Silence අනුව (අයින් / disable / markers).",
};
$("etype").addEventListener("change", () => { $("etypehint").textContent = HINTS[$("etype").value]; });
$("etypehint").textContent = HINTS.multicam;
$("updbtn").addEventListener("click", () => installUpdate());
$("chk").addEventListener("click", () => checkUpdate(true));
$("read").addEventListener("click", () => readSequence().catch((e) => log("Error: " + e, "bad")));
$("rtest").addEventListener("click", () => razorTest());
// the footer button: on the Batch page it runs the list, everywhere else one Auto Edit
$("go").addEventListener("click", () => {
  if (CUR_TAB === "batch") { showTab("edit"); return qRun(); }
  showTab("edit");
  return autoEdit();
});
$("ver").textContent = "AutoWed v" + PANEL_VERSION;
ping().then((ok) => { if (ok) checkUpdate(false); });
setInterval(ping, 15000);


// ---------- Claude debug bridge (MCP) ----------
// When "Claude: ON", the panel asks the engine for commands that Claude sent through the AutoWed MCP
// server (read the timeline, the log, run a test) and sends the answers back. Off by default.
let tlColors = true;    // camera colors on the cut pieces (no project close any more)
try { tlColors = localStorage.getItem("autowed.tlcolors2") !== "0"; } catch (e) {}
function showTlColors() { const b = $("tlcol"); if (!b) return; b.textContent = tlColors ? "Timeline colors: ON" : "Timeline colors: OFF"; b.className = tlColors ? "on" : ""; }
let bridgeOn = false;
try { bridgeOn = localStorage.getItem("autowed.bridge") === "1"; } catch (e) {}
function showBridge() { const b = $("dbg"); b.textContent = bridgeOn ? "Claude: ON" : "Claude: OFF"; b.className = bridgeOn ? "on" : ""; }

function safeJson(v, depth = 0) {
  if (v === null || v === undefined) return v === undefined ? null : v;
  if (typeof v === "number" || typeof v === "string" || typeof v === "boolean") return v;
  if (depth > 5) return String(v);
  if (Array.isArray(v)) return v.slice(0, 2000).map((x) => safeJson(x, depth + 1));
  if (typeof v === "object") {
    if (typeof v.seconds === "number" && Object.keys(v).length <= 3) return v.seconds;
    const o = {};
    for (const k of Object.keys(v).slice(0, 200)) { try { o[k] = safeJson(v[k], depth + 1); } catch (e) { o[k] = String(e); } }
    return o;
  }
  return String(v);
}

async function dumpSequence(args) {
  const project = await ppro.Project.getActiveProject();
  const seq = project && (await project.getActiveSequence());
  if (!seq) return { error: "no active sequence" };
  let fps = 25;
  try { const tb = parseFloat(await seq.getTimebase()); if (tb > 0) fps = TICKS / tb; } catch (e) {}
  const inT = (await seq.getInPoint()).seconds, outT = (await seq.getOutPoint()).seconds;
  const lo = args.range ? inT : -1, hi = args.range ? outT : 1e12;
  const maxClips = args.max_clips || 400;
  const dumpTrack = async (track, kind, i) => {
    const clips = [];
    for (const it of track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
      const a = (await it.getStartTime()).seconds, b = (await it.getEndTime()).seconds;
      if (b < lo || a > hi) continue;
      if (clips.length >= maxClips) { clips.push({ truncated: true }); break; }
      const c = { start: +a.toFixed(3), end: +b.toFixed(3), in: +(await it.getInPoint()).seconds.toFixed(3),
                  out: +(await it.getOutPoint()).seconds.toFixed(3) };
      try { c.name = await it.getName(); } catch (e) {}
      try { c.disabled = await it.isDisabled(); } catch (e) {}
      if (kind === "video") {
        try { c.speed = await it.getSpeed(); } catch (e) {}
        try { const r = await readItem(it); c.path = r.path; if (r.why) c.why = r.why; } catch (e) { c.read_error = String(e); }
      }
      clips.push(c);
    }
    let name = ""; try { name = track.name; } catch (e) {}
    return { index: i, name, clips };
  };
  const video = [], audio = [];
  const vc = await seq.getVideoTrackCount();
  for (let i = 0; i < vc; i++) video.push(await dumpTrack(await seq.getVideoTrack(i), "video", i));
  if (args.audio) {
    const ac = await seq.getAudioTrackCount();
    for (let i = 0; i < ac; i++) audio.push(await dumpTrack(await seq.getAudioTrack(i), "audio", i));
  }
  return { project: project.name, path: project.path, sequence: seq.name, guid: seq.guid.toString(), fps,
           in: inT, out: outT, end: (await seq.getEndTime()).seconds, video, audio };
}

async function bridgeRun(cmd, args) {
  args = args || {};
  if (cmd === "status") return { panel: PANEL_VERSION, busy, progress: ($("pct") || {}).textContent, stage: ($("stage") || {}).textContent };
  if (cmd === "log") return { lines: LOGBUF.slice(-(args.lines || 120)) };
  if (cmd === "sequence") return dumpSequence(args);
  if (cmd === "read_sequence") { const sd = await readSequence(); return sd ? { name: sd.name, inT: sd.inT, outT: sd.outT, hasInOut: sd.hasInOut, cams: sd.cams.map((c) => ({ name: c.name, track: c.track, clips: c.clips.length })), nest: sd.nestInRange ? sd.nestInRange.name : null } : { error: "no sequence" }; }
  if (cmd === "razor_test") { const n = LOGBUF.length; await razorTest(); return { lines: LOGBUF.slice(n) }; }
  if (cmd === "auto_edit") {
    if (busy) return { error: "AutoWed is already running" };
    if (args.event) $("event").value = args.event;
    if (args.edit_type) $("etype").value = args.edit_type;
    autoEdit();
    return { started: true, event: $("event").value, edit_type: $("etype").value };
  }
  if (cmd === "eval") {
    const AsyncFn = Object.getPrototypeOf(async function () {}).constructor;
    const fn = new AsyncFn("ppro", "project", "h", args.code || "");
    const project = await ppro.Project.getActiveProject();
    const h = { itemsOf, readTI, readItem, freshSeq, locate, log, fmt, sleep };
    return { result: safeJson(await fn(ppro, project, h)) };
  }
  return { error: "unknown command " + cmd };
}

async function bridgeLoop() {
  while (true) {
    if (!bridgeOn) { await sleep(1000); continue; }
    let job = null;
    try { job = await call("GET", "/api/bridge/next?panel=" + encodeURIComponent(PANEL_VERSION), null, 5000); } catch (e) { await sleep(2000); continue; }
    if (!job || !job.id) { await sleep(600); continue; }
    let res;
    try { res = { ok: true, result: safeJson(await bridgeRun(job.cmd, job.args)) }; }
    catch (e) { res = { ok: false, error: String((e && e.stack) || e) }; }
    try { await call("POST", "/api/bridge/result", { id: job.id, ...res }, 8000); } catch (e) {}
  }
}
$("dbg").onclick = () => {
  bridgeOn = !bridgeOn;
  try { localStorage.setItem("autowed.bridge", bridgeOn ? "1" : "0"); } catch (e) {}
  showBridge();
  log(bridgeOn ? "Claude debug bridge ON: Claude ට MCP එකෙන් timeline එක කියවන්න, test run කරන්න පුළුවන්." : "Claude debug bridge OFF.", "mute");
};
showBridge();
bridgeLoop();
if ($("tlcol")) $("tlcol").onclick = () => {
  tlColors = !tlColors;
  try { localStorage.setItem("autowed.tlcolors2", tlColors ? "1" : "0"); } catch (e) {}
  showTlColors();
  log(tlColors ? "Timeline colors ON: cut කරන කෑලි වලට කැමරාවේ color එක (Premiere close වෙන්නේ නැහැ)." : "Timeline colors OFF.", "mute");
};
showTlColors();

// =====================================================================================
// 0 · Sync: line up selected camera / recorder / mic files by audio -> new Synced Sequence
// =====================================================================================
async function collectClips(items, out = [], depth = 0) {
  for (const it of items) {
    let isBin = false;
    try { isBin = it.type === ppro.Constants.ProjectItemType.BIN; } catch (e) {}
    if (isBin && depth < 6) {
      try { await collectClips(await ppro.FolderItem.cast(it).getItems(), out, depth + 1); } catch (e) {}
      continue;
    }
    try {
      const ci = ppro.ClipProjectItem.cast(it);
      if (await ci.isSequence()) continue;
      const path = await ci.getMediaFilePath();
      if (!path || !/[\\/]/.test(path)) continue;          // titles, mattes, graphics
      if (!out.some((x) => x.path === path)) out.push({ path, name: it.name, item: ci });
    } catch (e) {}
  }
  return out;
}

async function runSync() {
  if (busy) return;
  busy = true; $("sync").disabled = true; $("go").disabled = true; showStop(true);
  try {
    const project = await ppro.Project.getActiveProject();
    if (!project) { log("Project එකක් open කරලා නැහැ.", "bad"); return; }
    if (!ppro.ProjectUtils || typeof ppro.ProjectUtils.getSelection !== "function") {
      log("මේ Premiere version එකේ Project panel selection කියවන්න බැහැ (ProjectUtils.getSelection නැහැ).", "bad"); return;
    }
    const sel = await ppro.ProjectUtils.getSelection(project);
    const files = await collectClips(sel ? await sel.getItems() : []);
    if (files.length < 2) { log("Project panel එකේ sync කරන්න ඕන files (හෝ ඒවා තියෙන bins) select කරලා ආයෙ ඔබන්න. files 2ක් වත් ඕන.", "bad"); return; }
    if (!(await ping())) { log("Engine එකට සම්බන්ධ වෙන්න බැහැ.", "bad"); return; }
    log(`Sync: files ${files.length}ක් audio එකෙන් ගලපනවා…`);
    const job = (await call("POST", "/api/sync", { files: files.map((f) => ({ path: f.path, name: f.name })) })).job;
  CUR_JOB = job;
    let res = null;
    while (true) {
      await sleep(1000);
      let j;
      if (await stopRequested(job)) return false;
    try { j = await call("GET", "/api/job/" + job); } catch (e) { continue; }
    if (j.status === "cancelled") { log("■ නැවැත්තුවා. Timeline එකේ දැනටමත් වැටුණ cuts එහෙමම තියෙනවා (ඕන නම් Ctrl+Z).", "warn"); showProgress("done", j.progress || 0, null); return false; }
      $("syncinfo").textContent = `${Math.round((j.progress || 0) * 100)}% · ${j.stage_text || j.stage || ""}`;
      if (j.status === "error") { for (const l of j.log) log(l, l.startsWith("ERROR") ? "bad" : "mute"); return; }
      if (j.status === "done") { res = j.result; break; }
    }
    await buildSyncedSequence(project, files, res);
  } catch (e) {
    log("Sync: " + (e && e.message || e), "bad");
    if (e && e.stack) log("  " + String(e.stack).split("\n").slice(0, 3).join(" | "), "mute");
  } finally { busy = false; $("sync").disabled = false; $("go").disabled = false; showStop(false); }
}

async function buildSyncedSequence(project, files, res) {
  const byPath = new Map(files.map((f) => [f.path, f]));
  const pl = res.placements.filter((p) => byPath.has(p.path));
  const firstVid = pl.filter((p) => p.has_video && !p.unsynced).sort((a, b) => a.start - b.start)[0];
  if (!firstVid) { log("Sync: video file එකක් නැති නිසා sequence එකක් හදන්න බැහැ.", "bad"); return; }
  const name = "Synced Sequence";
  // sequence settings from a real camera clip; that clip is removed again right away
  let seq = await project.createSequenceFromMedia(name, [byPath.get(firstVid.path).item]);
  if (!seq) throw new Error("sequence එක හදන්න බැරි වුණා");
  const g = seq.guid.toString();
  const S = async () => (seq = await freshSeq(project, seq, g));
  for (const kind of ["v", "a"]) {
    const cnt = kind === "v" ? await (await S()).getVideoTrackCount() : await (await S()).getAudioTrackCount();
    for (let i = 0; i < cnt; i++) {
      const items = (await itemsOf(async () => kind === "v" ? (await S()).getVideoTrack(i) : (await S()).getAudioTrack(i))).map((x) => x.it);
      if (items.length) await retryStale(async () => removeItems(project, await S(), items, kind === "v" ? ppro.Constants.MediaType.VIDEO : ppro.Constants.MediaType.AUDIO));
    }
  }
  // audio-only files first (recorder/mic lanes), then cameras; each lane keeps the audio track it got first
  const audioLane = new Map();
  let nextAudio = 0;
  const order = [...pl.filter((p) => !p.has_video).sort((a, b) => a.audio_track - b.audio_track || a.start - b.start),
                 ...pl.filter((p) => p.has_video).sort((a, b) => a.video_track - b.video_track || a.start - b.start)];
  let placed = 0, failed = 0;
  for (const p of order) {
    const laneKey = (p.has_video ? "v" : "a") + p.audio_track;
    if (!audioLane.has(laneKey)) { audioLane.set(laneKey, nextAudio); nextAudio += 1; }
    const aIdx = audioLane.get(laneKey);
    const before = await (await S()).getAudioTrackCount().catch(() => 0);
    try {
      await retryStale(async () => {
        const ed = ppro.SequenceEditor.getEditor(await S());
        transact(project, "AutoWed sync", () => [ed.createOverwriteItemAction(byPath.get(p.path).item, tt(p.start), p.has_video ? p.video_track : 0, aIdx)]);
      });
      placed++;
      // if this clip opened more than one new audio track (multi-channel camera), move later lanes up
      const after = await (await S()).getAudioTrackCount().catch(() => before);
      if (after - before > 1 && aIdx + 1 >= nextAudio) nextAudio = aIdx + (after - before);
    } catch (e) {
      failed++;
      log(`Sync: "${p.name}" timeline එකට දාන්න බැරි වුණා: ${e && e.message || e}`, "warn");
    }
    $("syncinfo").textContent = `Timeline එකට දානවා ${placed + failed}/${order.length}`;
  }
  try { await project.openSequence(await S()); } catch (e) {}
  const un = pl.filter((p) => p.unsynced);
  log(`✔ Sync ඉවරයි: "${name}" · files ${placed}${failed ? ` (බැරි වුණා ${failed})` : ""} · master audio: ${res.anchor}`, "ok");
  for (const p of pl.filter((x) => !x.unsynced && x.confidence < 2)) log(`  ⚠ "${p.name}" sync විශ්වාසය අඩුයි (${p.confidence}) — ඇහෙන් බලන්න`, "warn");
  if (un.length) log(`  Sync වුණේ නැති files ${un.length}ක් timeline එකේ අගට දැම්මා: ${un.map((p) => p.name).join(", ")}`, "warn");
  for (const d of res.drift || []) log(`  Clock drift: "${d.unit}" විනාඩි ${d.over_min} කට ${d.drift_ms} ms`, "warn");
  if (un.length) await addMarkers(project, await S(), un.map((p) => ({ name: "Sync නොවුණු file: " + p.name, start: p.start, dur: p.end - p.start })));
  $("syncinfo").textContent = `"${name}" · files ${placed}`;
}

// =====================================================================================
// 3 · Batch: several ranges / events edited one after another
// =====================================================================================
let QUEUE = [];
function qKey() { return "autowed.queue"; }
function qSave() { try { localStorage.setItem(qKey(), JSON.stringify(QUEUE)); } catch (e) {} }
function qLoad() { try { QUEUE = JSON.parse(localStorage.getItem(qKey()) || "[]"); } catch (e) { QUEUE = []; } }
function qRender() {
  const box = $("queue");
  box.innerHTML = "";
  if (!QUEUE.length) { box.innerHTML = '<div class="mute">ලිස්ට් එක හිස්.</div>'; return; }
  QUEUE.forEach((q, i) => {
    const row = document.createElement("div");
    row.className = "qrow";
    const st = q.status === "done" ? " ✔" : q.status === "error" ? " ✘" : q.status === "running" ? " …" : "";
    const sp = document.createElement("span");
    sp.textContent = `${i + 1}. ${q.seqName} · ${fmt(q.range[0])}→${fmt(q.range[1])}${st}`;
    const ev = document.createElement("select");
    for (const o of $("event").options) { const n = document.createElement("option"); n.value = o.value; n.textContent = o.textContent.split("  ·  ")[0]; ev.appendChild(n); }
    ev.value = q.event;
    ev.onchange = () => { q.event = ev.value; qSave(); };
    const kd = document.createElement("select");
    for (const [v, t] of [["stacked", "Manual"], ["multicam", "Multicam"]]) { const n = document.createElement("option"); n.value = v; n.textContent = t; kd.appendChild(n); }
    kd.value = q.kind;
    kd.onchange = () => { q.kind = kd.value; qSave(); };
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.onclick = () => { QUEUE.splice(i, 1); qSave(); qRender(); };
    row.append(sp, ev, kd, rm);
    box.appendChild(row);
  });
}

async function qAddInOut() {
  const project = await ppro.Project.getActiveProject();
  const seq = project && (await project.getActiveSequence());
  if (!seq) { log("Active sequence එකක් නැහැ.", "bad"); return; }
  const inT = (await seq.getInPoint()).seconds, outT = (await seq.getOutPoint()).seconds, endT = (await seq.getEndTime()).seconds;
  if (!(outT > inT + 0.5) || (inT < 0.01 && Math.abs(outT - endT) < 0.05)) { log("ලිස්ට් එකට දාන්න කලින් In (I) සහ Out (O) දාන්න.", "bad"); return; }
  const ev = $("event").value || "auto";
  const dup = QUEUE.find((q) => q.seqGuid === seq.guid.toString() && q.status !== "done" && q.event === ev &&
                               Math.abs(q.range[0] - inT) < 0.5 && Math.abs(q.range[1] - outT) < 0.5);
  if (dup) { log(`මේ කොටස (${fmt(inT)} → ${fmt(outT)}) දැනටමත් ලිස්ට් එකේ තියෙනවා.`, "warn"); return; }
  // same part of the timeline under another event: in live / end mode the second edit cuts over the first
  const over = QUEUE.find((q) => q.seqGuid === seq.guid.toString() && q.event !== ev &&
                                 Math.min(q.range[1], outT) - Math.max(q.range[0], inT) > 0.5 * Math.min(outT - inT, q.range[1] - q.range[0]));
  if (over) log(`⚠ මේ කොටස ${fmt(over.range[0])} → ${fmt(over.range[1])} "${eventName(over.event)}" එකටත් ලිස්ට් එකේ තියෙනවා. ` +
                `Settings > output "live" නම් දෙවෙනි edit එක පළවෙනි එකට උඩින් cut වෙනවා. වැරදීමක් නම් ලිස්ට් එකෙන් අයින් කරන්න.`, "warn");
  QUEUE.push({ seqGuid: seq.guid.toString(), seqName: seq.name, range: [inT, outT], event: ev, kind: $("etype").value, status: "" });
  qSave(); qRender(); goLabel();
  log(`ලිස්ට් එකට දැම්මා: ${seq.name} · ${fmt(inT)} → ${fmt(outT)} · ${eventName($("event").value)}`, "ok");
}

async function qRun() {
  if (busy) return;
  const todo = QUEUE.filter((q) => q.status !== "done");
  if (!todo.length) { log("ලිස්ට් එකේ edit කරන්න දෙයක් නැහැ.", "warn"); return; }
  busy = true; $("go").disabled = true; $("qrun").disabled = true; showStop(true);
  try {
    let n = 0;
    for (const q of todo) {
      if (STOPPING) { log("■ Batch ලිස්ට් එක නැවැත්තුවා.", "warn"); break; }
      n++;
      log(`── Batch ${n}/${todo.length}: ${q.seqName} · ${fmt(q.range[0])} → ${fmt(q.range[1])} · ${eventName(q.event)}`, "ok");
      const outMode = ($("x_live") && $("x_live").value) || "new";
      const clash = outMode !== "new" && QUEUE.find((o) => o !== q && o.status === "done" && o.seqGuid === q.seqGuid &&
        Math.min(o.range[1], q.range[1]) - Math.max(o.range[0], q.range[0]) > 0.5 * Math.min(q.range[1] - q.range[0], o.range[1] - o.range[0]));
      if (clash) {
        log(`⚠ මඟ හැරියා: ${fmt(q.range[0])} → ${fmt(q.range[1])} කොටස "${eventName(clash.event)}" විදිහට දැනටමත් edit කරලා. ` +
            `ආයෙ කළොත් ඒ edit එකට උඩින් cuts වැටෙනවා. ඕන නම් Settings > output "new" කරලා ආයෙ run කරන්න.`, "warn");
        q.status = "error"; qSave(); qRender(); goLabel();
        continue;
      }
      q.status = "running"; qRender();
      let ok = false;
      try {
        const project = await ppro.Project.getActiveProject();      // fresh: multicam reopens the project
        const seq = (await project.getSequences()).find((x) => x.guid.toString() === q.seqGuid)
                 || (await project.getSequences()).find((x) => x.name === q.seqName);
        if (!seq) throw new Error("sequence එක හමු වුණේ නැහැ: " + q.seqName);
        await project.openSequence(seq);
        await sleep(400);
        ok = await runAutoEdit({ range: q.range, event: q.event, kind: q.kind });
      } catch (e) { log("Batch: " + (e && e.message || e), "bad"); }
      q.status = ok ? "done" : "error"; qSave(); qRender(); goLabel();
      if (!STOPPING) notifyPhone(ok ? `✔ ${eventName(q.event)} (${n}/${todo.length})` : `✖ ${eventName(q.event)} (${n}/${todo.length})`,
        ok ? (LAST_DONE || "edit එක ඉවරයි") : (LAST_BAD || "Log එක බලන්න"), ok ? "white_check_mark" : "warning");
    }
    log("✔ Batch ලිස්ට් එක ඉවරයි.", "ok");
    const okN = todo.filter((q) => q.status === "done").length;
    notifyPhone("🎬 Batch ඉවරයි", `${okN}/${todo.length} edits ඉවරයි${okN < todo.length ? " · " + (todo.length - okN) + "ක් බැරි වුණා" : ""}.`, "tada", "high");
  } finally { busy = false; $("go").disabled = false; $("qrun").disabled = false; showStop(false); }
}

// =====================================================================================
// Events: speech / chanting / music / drums blocks -> markers (+ batch list)
// =====================================================================================
async function findEvents() {
  if (busy) return;
  busy = true; $("evfind").disabled = true; showStop(true);
  try {
    const sd = await readSequence();
    if (!sd) return;
    if (!(await ping())) { log("Engine එකට සම්බන්ධ වෙන්න බැහැ.", "bad"); return; }
    const range = sd.hasInOut ? [sd.inT, sd.outT] : [0, sd.endT];
    log(`Events හොයනවා: ${sd.name} · ${fmt(range[0])} → ${fmt(range[1])} (audio එකෙන්)…`);
    const body = { project: { name: sd.name, fps: sd.fps, master_cam: 0, range,
                              cameras: sd.cams.map((c) => ({ name: c.name, clips: c.clips })), audio_tracks: sd.audio }, range };
    const job = (await call("POST", "/api/events", body)).job;
  CUR_JOB = job;
    let res = null;
    while (true) {
      await sleep(1000);
      let j;
      if (await stopRequested(job)) return false;
    try { j = await call("GET", "/api/job/" + job); } catch (e) { continue; }
    if (j.status === "cancelled") { log("■ නැවැත්තුවා. Timeline එකේ දැනටමත් වැටුණ cuts එහෙමම තියෙනවා (ඕන නම් Ctrl+Z).", "warn"); showProgress("done", j.progress || 0, null); return false; }
      showProgress("audio", j.progress || 0, null);
      if (j.status === "error") { for (const l of j.log) log(l, l.startsWith("ERROR") ? "bad" : "mute"); return; }
      if (j.status === "done") { res = j.result; break; }
    }
    const project = await ppro.Project.getActiveProject();
    const evs = res.events.filter((e) => e.event);
    const n = await addMarkers(project, sd.seq, res.events.filter((e) => e.kind !== "quiet").map((e) => ({
      name: `AutoWed · ${e.label}`, start: e.start, dur: e.end - e.start,
      comment: `${e.kind} · ${eventName(e.event || "")} ? · විශ්වාසය ${Math.round(e.confidence * 100)}%` })));
    for (const e of res.events) log(`  ${fmt(e.start)} → ${fmt(e.end)}  ${e.label}${e.event ? " → " + eventName(e.event) : ""}`, "mute");
    for (const e of evs) QUEUE.push({ seqGuid: sd.guid, seqName: sd.name, range: [e.start, e.end], event: e.event, kind: $("etype").value, status: "" });
    qSave(); qRender();
    showProgress("done", 1, null);
    log(`✔ Events ${res.events.length}ක් · markers ${n} · batch ලිස්ට් එකට ${evs.length}ක් දැම්මා. Event එක වැරදි නම් ලිස්ට් එකේ වෙනස් කරන්න.`, "ok");
  } catch (e) { log("Events: " + (e && e.message || e), "bad"); }
  finally { busy = false; $("evfind").disabled = false; showStop(false); }
}

// =====================================================================================
// Stabilize: Warp Stabilizer on the chosen pieces that are still shaky
// =====================================================================================
// Camera colour match: the engine measures each camera against the main one (skin tones of the faces,
// else the scene) and sends RGB gains; here every used piece of that camera gets a "Color Balance (RGB)"
// effect with those gains. Our effect is named "AutoWed color", so a second run updates it instead of
// stacking another one. Safe: if the effect or its parameters are not what we expect, nothing is changed.
const COLOR_TAG = "AutoWed color";
let COLOR_MATCH_NAME = null;
async function colorEffectName() {
  if (COLOR_MATCH_NAME) return COLOR_MATCH_NAME;
  const F = ppro.VideoFilterFactory;
  if (!F || typeof F.createComponent !== "function") return null;
  try {
    const mn = await F.getMatchNames();
    const dn = await F.getDisplayNames();
    let i = dn.findIndex((d) => /color balance \(rgb\)/i.test(d));
    if (i < 0) i = mn.findIndex((m) => /colou?r ?balance/i.test(m) && /rgb/i.test(m));
    if (i >= 0) COLOR_MATCH_NAME = mn[i];
  } catch (e) {}
  return COLOR_MATCH_NAME;
}
// Lumetri Color (measured on Premiere 26.5 with exported frames): Temperature t multiplies R/B by
// exp(0.0115 t); Tint +t lowers G against R,B by exp(-0.0059 t) (magenta), -t raises it by exp(0.0086 t);
// Exposure +1 = x1.41 brighter (8-bit picture), -1 = x0.59.
function gainToLumetri(g) {
  const k = (0.299 * g[0] + 0.587 * g[1] + 0.114 * g[2]) / 100;           // brightness change wanted
  const ex = k >= 1 ? Math.log2(k) / 0.5 : Math.log2(k) / 0.76;
  const r = g[0] / k, gg = g[1] / k, b = g[2] / k;
  const temp = Math.log(r / b) / 0.0115;
  const rg = gg / ((r + b) / 2);
  const tint = rg < 1 ? -Math.log(rg) / 0.0059 : -Math.log(rg) / 0.0086;
  const cl = (v, m) => Math.max(-m, Math.min(m, v));
  return { exposure: +cl(ex, 1.5).toFixed(3), temperature: +cl(temp, 60).toFixed(2), tint: +cl(tint, 40).toFixed(2) };
}
function lumetriParams(comp) {
  const n = comp.getParamCount();
  const ps = {};
  for (let i = 0; i < n; i++) {
    const q = comp.getParam(i);
    const d = String(q.displayName || "").trim().toLowerCase();
    if (!ps.temperature && d === "temperature") ps.temperature = q;
    else if (!ps.tint && d === "tint") ps.tint = q;
    else if (!ps.exposure && d === "exposure") ps.exposure = q;
  }
  if (!(ps.temperature && ps.tint && ps.exposure) && n > 19) {   // names missing: Basic Correction indexes
    ps.temperature = comp.getParam(14); ps.tint = comp.getParam(15); ps.exposure = comp.getParam(19);
  }
  return ps.temperature && ps.tint && ps.exposure ? ps : null;
}
function rgbParams(comp) {
  const n = comp.getParamCount();
  const ps = {};
  for (let i = 0; i < n; i++) {
    const p = comp.getParam(i);
    const d = String(p.displayName || "").toLowerCase();
    if (!ps.r && /^red\b|\bred$/.test(d)) ps.r = p;
    else if (!ps.g && /^green\b|\bgreen$/.test(d)) ps.g = p;
    else if (!ps.b && /^blue\b|\bblue$/.test(d)) ps.b = p;
  }
  return ps.r && ps.g && ps.b ? ps : null;
}
async function paramNumber(p) {
  try { const k = await p.getStartValue(); const v = k && k.value; return typeof v === "number" ? v : (v && typeof v.value === "number" ? v.value : null); }
  catch (e) { return null; }
}
async function colorMatch(project, sd, cams, result) {
  // x_color: "on" / "avg" = Lumetri (main camera / both cameras meet in the middle),
  //          "rgb" / "rgbavg" = Color Balance (RGB), "off"
  const sel = ($("x_color") || {}).value || "on";
  const lumetri = sel === "on" || sel === "avg";
  const mode = sel === "avg" || sel === "rgbavg" ? "avg" : (sel === "off" ? "off" : "main");
  const cm = result.color;
  if (mode === "off" || !cm) return;
  const pieces = (cm.pieces || []).map((p) => ({ ...p, g: mode === "avg" ? p.rgb_avg : p.rgb }));
  // per-camera fallback (older engine / no data around a piece): the whole-event gains
  const camGain = {};
  for (const x of cm.cams || []) camGain[x.cam] = x.rgb;
  if (!pieces.length && !Object.keys(camGain).length) return;
  const match = lumetri ? "AE.ADBE Lumetri" : await colorEffectName();
  if (!match) { log("Color match: 'Color Balance (RGB)' effect එක මේ Premiere එකේ හම්බුණේ නැහැ. Gains log එකේ තියෙනවා, අතින් දාන්න පුළුවන්.", "warn"); return; }
  const [r0, r1] = result.range;
  const near = (g) => !g || g.every((v) => Math.abs(v - 100) < 1.5);
  const gainAt = (cam, a, b) => {
    const m = (a + b) / 2;
    let best = null, bd = 1e9;
    for (const p of pieces) {
      if (p.cam !== cam) continue;
      const d = m >= p.start - 0.05 && m <= p.end + 0.05 ? 0 : Math.min(Math.abs(m - p.start), Math.abs(m - p.end));
      if (d < bd) { bd = d; best = p; }
    }
    if (best && bd < 2.0) return best.g;
    return mode === "avg" ? null : camGain[cam];
  };
  let done = 0, failed = 0, checked = false, skipped = 0;
  for (let ci = 0; ci < cams.length; ci++) {
    const c = cams[ci];
    if (!c) continue;
    let items = [];
    try {
      const seq = await freshSeq(project, sd.seq, sd.guid);
      items = (await itemsOf(await seq.getVideoTrack(c.track))).filter((o) => o.b > r0 + 0.01 && o.a < r1 - 0.01);
      const en = [];
      for (const o of items) { try { if (!(await o.it.isDisabled())) en.push(o); } catch (e) { en.push(o); } }
      items = en;
    } catch (e) { log("Color match: " + (e && e.message || e), "warn"); continue; }
    let n = 0;
    for (const o of items) {
      if (STOPPING) return;
      const g = gainAt(ci, o.a, o.b);
      if (near(g)) { skipped++; continue; }
      try {
        let chain = await o.it.getComponentChain();
        let comp = null;
        for (let i = chain.getComponentCount() - 1; i >= 0; i--) {
          const k = chain.getComponentAtIndex(i);
          try { if ((await k.getInstanceName()) === COLOR_TAG) { comp = k; break; } } catch (e) {}
        }
        if (comp) {   // tool changed (Color Balance <-> Lumetri): take the old one away
          let mn = null;
          try { mn = await comp.getMatchName(); } catch (e) {}
          if (mn && mn !== match) {
            transact(project, "AutoWed color", () => [chain.createRemoveComponentAction(comp)]);
            chain = await o.it.getComponentChain();
            comp = null;
          }
        }
        if (!comp) {
          const nc = await ppro.VideoFilterFactory.createComponent(match);
          if (!transact(project, "AutoWed color", () => [chain.createAppendComponentAction(nc)])) throw new Error("effect එක දාන්න බැරි වුණා");
          chain = await o.it.getComponentChain();
          comp = chain.getComponentAtIndex(chain.getComponentCount() - 1);
          try { transact(project, "AutoWed color", () => [comp.createSetInstanceNameAction(COLOR_TAG)]); } catch (e) {}
        }
        if (lumetri) {
          const lp = lumetriParams(comp);
          if (!lp) throw new Error("Lumetri: Temperature / Tint / Exposure හම්බුණේ නැහැ");
          const L = gainToLumetri(g);
          const ok = transact(project, "AutoWed color", () => [
            lp.exposure.createSetValueAction(lp.exposure.createKeyframe(L.exposure), true),
            lp.temperature.createSetValueAction(lp.temperature.createKeyframe(L.temperature), true),
            lp.tint.createSetValueAction(lp.tint.createKeyframe(L.tint), true)]);
          if (!ok) throw new Error("Lumetri values දාන්න බැරි වුණා");
          if (!n) log(`Lumetri ${c.name || "Cam " + (ci + 1)}: exposure ${L.exposure > 0 ? "+" : ""}${L.exposure} · temperature ${L.temperature} · tint ${L.tint} (පළවෙනි කෑල්ල)`, "mute");
          done++; n++;
          await breathe();
          continue;
        }
        const ps = rgbParams(comp);
        if (!ps) throw new Error("Red / Green / Blue parameters හම්බුණේ නැහැ");
        if (!checked) {
          // the neutral value must be 100 (%): otherwise this is not the effect we think, change nothing more
          const v = await paramNumber(ps.g);
          if (v != null && (v < 50 || v > 200)) {
            log(`Color match නැවැත්තුවා: '${match}' Green = ${v} (100 බලාපොරොත්තු වුණා).`, "warn");
            try { transact(project, "AutoWed color", () => [chain.createRemoveComponentAction(comp)]); } catch (e) {}
            return;
          }
          checked = true;
        }
        const ok = transact(project, "AutoWed color", () => [
          ps.r.createSetValueAction(ps.r.createKeyframe(g[0]), true),
          ps.g.createSetValueAction(ps.g.createKeyframe(g[1]), true),
          ps.b.createSetValueAction(ps.b.createKeyframe(g[2]), true)]);
        if (!ok) throw new Error("values දාන්න බැරි වුණා");
        done++; n++;
      } catch (e) {
        failed++;
        if (failed === 1) log("Color match: " + (e && e.message || e), "warn");
        if (failed >= 5 && !done) { log("Color match නැවැත්තුවා (effect එක දාන්න බැහැ).", "warn"); return; }
      }
      await breathe();
    }
    if (n) log(`Exposure + WB: ${c.name || "Cam " + (ci + 1)} කෑලි ${n}කට ${mode === "avg" ? "(කැමරා දෙකේ මැදට)" : `(${cm.ref_name} එකට)`}`, "ok");
  }
  if (failed) log(`Color match: කෑලි ${failed}කට බැරි වුණා.`, "warn");
  if (!done && !failed) log("Exposure + WB: කැමරා දැනටමත් එක වගේ, මුකුත් වෙනස් කළේ නැහැ.", "mute");
}

const NO_STAB_EVENTS = new Set(["dance_floor", "surprise_dance"]);   // handheld moving to the music is wanted
async function stabilizePieces(project, sd, cams, result, kind) {
  const mode = ($("stab") || {}).value || "off";
  if (mode === "off" || NO_STAB_EVENTS.has(result.event)) return;
  const thr = numOrNull("stab_thr") || 1.5;
  const shaky = (result.segments || []).filter((s) => (s.shake || 0) >= thr && s.end - s.start >= 1.0);
  if (!shaky.length) { log("Stabilize: shake වැඩි කෑලි නැහැ.", "mute"); return; }
  const canWarp = mode === "warp" && kind === "stacked" && ppro.VideoFilterFactory && typeof ppro.VideoFilterFactory.createComponent === "function";
  if (!canWarp) {
    const n = await addMarkers(project, sd.seq, shaky.map((s) => ({ name: `Shaky (Cam ${s.cam + 1})`, start: s.start, dur: s.end - s.start, comment: `shake ${s.shake}` })));
    log(`Stabilize: shake වැඩි කෑලි ${shaky.length}කට markers ${n}ක් දැම්මා${mode === "warp" && kind !== "stacked" ? " (Multicam clips වලට Warp Stabilizer කෙලින්ම දාන්න බැහැ)" : ""}.`, "ok");
    return;
  }
  let done = 0, failed = 0;
  for (const s of shaky) {
    const c = cams[s.cam];
    if (!c) continue;
    try {
      await retryStale(async () => {
        const seq = await freshSeq(project, sd.seq, sd.guid);
        const hit = (await itemsOf(await seq.getVideoTrack(c.track))).find((x) => Math.abs(x.a - s.start) < 0.05 && Math.abs(x.b - s.end) < 0.05);
        if (!hit) throw new Error("piece not found");
        const chain = await hit.it.getComponentChain();
        const comp = await ppro.VideoFilterFactory.createComponent("AE.ADBE SubspaceStabilizer");
        transact(project, "AutoWed stabilize", () => [chain.createAppendComponentAction(comp)]);
      });
      done++;
    } catch (e) {
      failed++;
      if (failed === 1) log("Stabilize: " + (e && e.message || e), "warn");
    }
  }
  log(`Stabilize: Warp Stabilizer කෑලි ${done}කට දැම්මා${failed ? `, ${failed}කට බැරි වුණා` : ""}. Premiere background එකේ analyze කරනවා.`, done ? "ok" : "warn");
  if (failed) await addMarkers(project, sd.seq, shaky.slice(0, 50).map((s) => ({ name: `Shaky (Cam ${s.cam + 1})`, start: s.start, dur: s.end - s.start })));
}

$("sync").onclick = runSync;
if ($("stop")) $("stop").onclick = stopNow;
if ($("reset")) $("reset").onclick = () => resetPanel().catch(() => {});
$("qadd").onclick = qAddInOut;
$("qrun").onclick = qRun;
$("qclear").onclick = () => { QUEUE = []; qSave(); qRender(); };
$("evfind").onclick = findEvents;
qLoad(); qRender();


// =====================================================================================
// Extras: options, QC markers, event transitions, learning, studio presets, stats, language
// =====================================================================================
function gatherExtras() {
  const v = (id, d) => (($(id) || {}).value || d);
  return {
    silence: v("x_sil", "off") === "copy",
    flash_frames: numOrNull("x_flash") || 8,
    trim: v("x_trim", "on") === "on",
    loudness: v("x_loud", "copy") === "copy",
    best_mic: v("x_mic", "copy") === "copy",
    couple: v("x_couple", "on") === "on",
    qc_markers: v("x_qc", "on") !== "off",
  };
}

// new-timeline output: the checks are already markers in the new timeline (engine), only the report here
async function qcReportOnly(result) {
  try { renderMoments(result); } catch (e) {}
  const L = result.loudness;
  if (L) log(`Loudness: ${L.lufs} LUFS (ඉලක්කය ${L.target}) → ${L.gain_db > 0 ? "+" : ""}${L.gain_db} dB`, "mute");
  if (result.trim) log(`Smart trim: ${fmt(result.range[0])}–${fmt(result.trim[0])} සහ ${fmt(result.trim[1])}–${fmt(result.range[1])} හිස් කොටස් අයින් කළා.`, "ok");
  if (result.silences && result.silences.length) log(`Silence: කොටස් ${result.silences.length}ක් (${Math.round(result.silences.reduce((a, x) => a + x.end - x.start, 0))}s) කපලා white flash දැම්මා.`, "ok");
  if (((($("x_qc") || {}).value) || "on") === "on")
    log(`QC markers අලුත් timeline එකේ: moments ${(result.moments || []).length}, weak takes ${(result.bad || []).length}, clipping ${(result.clipping || []).length}, mic gaps ${(result.mic_gaps || []).length}`, "ok");
}

async function qcReport(project, sd, result, cams, kind) {
  try { renderMoments(result, sd && sd.guid); } catch (e) {}
  const L = result.loudness;
  if (L) log(`Loudness: ${L.lufs} LUFS (ඉලක්කය ${L.target}) → ${L.gain_db > 0 ? "+" : ""}${L.gain_db} dB${gatherExtras().loudness ? " copy sequence එකේ දැම්මා" : ""}`, "mute");
  if (result.trim) log(`Smart trim: ${fmt(result.range[0])}–${fmt(result.trim[0])} සහ ${fmt(result.trim[1])}–${fmt(result.range[1])} හිස් කොටස් off කළා.`, "ok");
  if (result.silences && result.silences.length) log(`Silence: කොටස් ${result.silences.length}ක් (${Math.round(result.silences.reduce((a, x) => a + x.end - x.start, 0))}s) copy sequence එකෙන් කපලා white flash දැම්මා.`, "ok");
  if (((($("x_qc") || {}).value) || "on") !== "on") return;
  const mk = [];
  const KIND = { applause: "අත්පොළසන් / cheer", smile: "සිනහ", hug: "බදාගැනීම / emotion" };
  for (const m of result.moments || []) mk.push({ name: `★ Moment: ${KIND[m.kind] || m.kind}`, start: m.time, dur: 0.1, comment: `score ${m.score}` });
  for (const b of (result.bad || []).slice(0, 60)) mk.push({ name: `⚠ ${b.name}: ${b.why}`, start: b.start, dur: b.end - b.start });
  for (const c of (result.clipping || []).slice(0, 40)) mk.push({ name: `⚠ Audio clipping ${c.name}`, start: c.start, dur: c.end - c.start });
  for (const g of (result.mic_gaps || []).slice(0, 40)) mk.push({ name: `⚠ Mic drop-out ${g.name}`, start: g.start, dur: g.end - g.start });
  for (const j of (result.jumps || []).slice(0, 60)) mk.push({ name: "⚠ Jump cut? (framing එක සමානයි)", start: j.time, dur: 0.1 });
  for (const r of result.rituals || []) mk.push({ name: `◆ ${r.label}`, start: r.start, dur: r.end - r.start });
  if (result.trim) {
    if (result.trim[0] > result.range[0]) mk.push({ name: "Smart trim (හිස්)", start: result.range[0], dur: result.trim[0] - result.range[0] });
    if (result.trim[1] < result.range[1]) mk.push({ name: "Smart trim (හිස්)", start: result.trim[1], dur: result.range[1] - result.trim[1] });
  }
  if (!mk.length) return;
  const n = await addMarkers(project, sd.seq, mk);
  log(`QC markers ${n}: moments ${(result.moments || []).length}, weak takes ${(result.bad || []).length}, clipping ${(result.clipping || []).length}, mic gaps ${(result.mic_gaps || []).length}`, "ok");
}

async function eventTransitions(project, sd, cams, result) {
  if (((($("x_trans") || {}).value) || "off") !== "on") return;
  try {
    if (!ppro.TransitionFactory || !ppro.AddTransitionOptions) { log("Transitions: මේ Premiere version එකේ API එක නැහැ.", "warn"); return; }
    const names = await ppro.TransitionFactory.getVideoTransitionMatchNames();
    const mn = (names || []).find((x) => /cross.?dissolve/i.test(x)) || (names || []).find((x) => /dissolve/i.test(x));
    if (!mn) { log("Transitions: Cross Dissolve හමු වුණේ නැහැ.", "warn"); return; }
    const segs = (result.segments || []).filter((x) => x.cam >= 0);
    if (!segs.length) return;
    const ends = [{ seg: segs[0], start: true }, { seg: segs[segs.length - 1], start: false }];
    let n = 0;
    for (const e of ends) {
      const c = cams[e.seg.cam];
      if (!c) continue;
      await retryStale(async () => {
        const seq = await freshSeq(project, sd.seq, sd.guid);
        const hit = (await itemsOf(await seq.getVideoTrack(c.track))).find((x) => Math.abs((e.start ? x.a : x.b) - (e.start ? e.seg.start : e.seg.end)) < 0.05);
        if (!hit) return;
        const tr = ppro.TransitionFactory.createVideoTransition(mn);
        const opt = ppro.AddTransitionOptions();
        opt.setApplyToStart(e.start); opt.setDuration(tt(Math.min(1.0, (e.seg.end - e.seg.start) / 3))); opt.setForceSingleSided(true);
        transact(project, "AutoWed transition", () => [hit.it.createAddVideoTransitionAction(tr, opt)]);
        n++;
      });
    }
    log(`Event transitions: Cross Dissolve ${n}ක් දැම්මා.`, "ok");
  } catch (e) { log("Transitions: " + (e && e.message || e), "warn"); }
}

function saveLastRun(sd, cams, result, kind) {
  try {
    localStorage.setItem("autowed.lastrun", JSON.stringify({ seqGuid: sd.guid, seqName: sd.name, range: result.range, kind,
      event: result.event, tracks: cams.map((c) => c.track), segments: result.segments }));
  } catch (e) {}
}

async function learnFromMe() {
  let lr = null;
  try { lr = JSON.parse(localStorage.getItem("autowed.lastrun") || "null"); } catch (e) {}
  if (!lr) { log("ඉගෙනගන්න කලින් Manual cutting Auto Edit එකක් කරලා, ඕන තැන් Shift+E එකෙන් වෙනස් කරන්න.", "warn"); return; }
  if (lr.kind !== "stacked") { log("Learn වැඩ කරන්නේ Manual cutting edit එකකට විතරයි (multicam angles API එකෙන් කියවන්න බැහැ).", "warn"); return; }
  const project = await ppro.Project.getActiveProject();
  const seq = (await project.getSequences()).find((x) => x.guid.toString() === lr.seqGuid);
  if (!seq) { log("කලින් edit කරපු sequence එක හමු වුණේ නැහැ.", "bad"); return; }
  // the editor's timeline now: which camera is enabled when
  const marks = [];
  for (let c = 0; c < lr.tracks.length; c++) {
    for (const x of await itemsOf(await seq.getVideoTrack(lr.tracks[c]))) {
      if (x.b <= lr.range[0] + 0.01 || x.a >= lr.range[1] - 0.01) continue;
      let dis = false; try { dis = await x.it.isDisabled(); } catch (e) {}
      if (!dis) marks.push({ cam: c, start: Math.max(x.a, lr.range[0]), end: Math.min(x.b, lr.range[1]) });
    }
  }
  marks.sort((a, b) => a.start - b.start);
  const shots = [];
  for (const m of marks) {
    const last = shots[shots.length - 1];
    if (last && last.cam === m.cam && m.start - last.end < 0.05) last.end = m.end; else shots.push({ ...m });
  }
  const avg = (a) => a.reduce((s, x) => s + (x.end - x.start), 0) / Math.max(1, a.length);
  const auto = lr.segments.filter((x) => x.cam >= 0);
  if (shots.length < 3) { log("ඉගෙනගන්න shots මදි.", "warn"); return; }
  const r = await call("POST", "/api/learn", { event: lr.event, user_avg_shot: avg(shots), auto_avg_shot: avg(auto) });
  log(`ඉගෙනගත්තා (${eventName(lr.event)}): ඔබේ සාමාන්‍ය shot ${avg(shots).toFixed(1)}s, AutoWed ${avg(auto).toFixed(1)}s → ඊළඟ පාරට shot දිග x${r.shot_scale} (samples ${r.samples})`, "ok");
}

async function showStats() {
  const r = await call("GET", "/api/stats");
  const f = (x) => `runs ${x.runs} · footage ${x.footage_min} min · cuts ${x.cuts} · processing ${x.processing_min} min · ඉතුරු වුණ කාලය ≈ ${x.saved_hours} h`;
  log("Stats (සියල්ල): " + f(r.all), "ok");
  log("Stats (දින 30): " + f(r.last_30_days), "ok");
  log("Events: " + Object.entries(r.by_event || {}).map(([k, v]) => `${eventName(k)} ${v}`).join(", "), "mute");
}

// studio presets: every panel setting, saved as a file others can copy (Documents\AutoWed Presets)
const PRESET_IDS = ["event", "etype", "x_live", "x_audon", "x_alink", "min_shot", "max_shot", "stab", "stab_thr", "x_sil", "x_flash", "x_trim", "x_trans", "x_loud", "x_mic", "x_couple", "x_qc", "x_color"];
let STUDIO = [];
async function presetsRefresh(quiet) {
  try {
    const r = await call("GET", "/api/studio_presets");
    STUDIO = r.presets || [];
    const sel = $("st_list");
    sel.innerHTML = "";
    const o0 = document.createElement("option"); o0.value = ""; o0.textContent = LANG === "en" ? "— pick —" : "— තෝරන්න —"; sel.appendChild(o0);
    for (const p of STUDIO) { const o = document.createElement("option"); o.value = p.name; o.textContent = p.name; sel.appendChild(o); }
    if (!quiet) log(`Presets ${STUDIO.length} · folder: ${r.folder}`, "mute");
  } catch (e) { log("Presets: " + (e && e.message || e), "warn"); }
}
async function presetSave() {
  const name = ($("st_name").value || "").trim() || "AutoWed preset";
  const settings = {};
  for (const id of PRESET_IDS) if ($(id)) settings[id] = $(id).value;
  settings.tlColors = tlColors;
  const r = await call("POST", "/api/studio_presets", { name, settings });
  log(`Preset save කළා: ${r.path} (මේ file එක අනිත් editors ලාට දෙන්න පුළුවන්)`, "ok");
  await presetsRefresh();
}
function presetApply(name) {
  const p = STUDIO.find((x) => x.name === name);
  if (!p) return;
  for (const [k, v] of Object.entries(p.settings || {})) if ($(k) && k !== "tlColors") $(k).value = v;
  if (typeof p.settings.tlColors === "boolean") { tlColors = p.settings.tlColors; try { showTlColors(); } catch (e) {} }
  log(`Preset "${name}" දැම්මා.`, "ok");
}

// language: the panel's own labels (the log stays mostly Sinhala)
const I18N = {
  s0: ["0 · Sync", "0 · Sync"], s0_help: [null, "Select camera, Zoom and DJI mic files (or their bins) in the Project panel and press Sync. They are lined up by audio into a new Synced Sequence."],
  sync_btn: [null, "Sync selected files"], s1: [null, "1 · Sequence"], s1_help: [null, "Open the synced timeline and set In (I) and Out (O) around the part to edit. AutoWed edits only that part, in that same timeline."],
  read_btn: [null, "Read sequence"], rtest_btn: [null, "Razor test"], s2: [null, "2 · Edit"], l_event: [null, "Event"], l_etype: [null, "Edit type"],
  l_min: [null, "Min shot (s)"], l_max: [null, "Max shot (s)"], l_stab: [null, "Shaky pieces"], o_stab_off: [null, "Don't stabilize"], o_stab_mark: [null, "Markers only"],
  o_stab_warp: [null, "Add Warp Stabilizer"], l_stabthr: [null, "Shake limit"], l_colors: [null, "Camera colors"], s_x: [null, "Extras (silence, audio, faces, checks)"],
  l_sil: [null, "Remove silences"], o_off: [null, "Off"], o_sil_copy: [null, "Cut them in the copy sequence"], l_flash: [null, "White flash (frames)"], l_trim: [null, "Smart trim"],
  o_on: [null, "On (empty head/tail off)"], l_trans: [null, "Event transitions"], o_trans_on: [null, "Cross dissolve at start/end"], l_loud: [null, "Loudness"],
  o_loud_copy: [null, "Set levels in the copy"], l_mic: [null, "Best mic + gap fill"], o_mic_copy: [null, "Best mic in the copy"], l_couple: [null, "Couple recognition"], o_on2: [null, "On"],
  l_qc: [null, "QC markers"], o_qc_on: [null, "Moments, weak takes, clipping, mic gaps"], go_btn: [null, "Auto Edit"], s3: [null, "3 · Batch (several events)"],
  s3_help: [null, "Set In/Out, pick the event and add it to the list, or find events to build the list from markers. Then edit them all at once."],
  qadd_btn: [null, "Add In/Out to list +"], evfind_btn: [null, "Find events"], qrun_btn: [null, "Edit the list ▶"], qclear_btn: [null, "Clear"],
  s4: [null, "Studio (presets, learn, stats)"], l_preset: [null, "Preset"], o_pick: [null, "— pick —"], l_pname: [null, "New name"], st_save: [null, "Save preset"],
  st_refresh: [null, "List"], stop_btn: [null, "Stop"], reset_btn: [null, "Reset"], learn_btn: [null, "Learn from my changes"], stats_btn: [null, "Stats"], chk_btn: [null, "Check update"], upd_btn: [null, "Update"],
};
let LANG = "si";
try { LANG = localStorage.getItem("autowed.lang") || "si"; } catch (e) {}
const I18N_SI = {};
function applyLang() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    const k = el.getAttribute("data-i18n");
    if (!(k in I18N_SI)) I18N_SI[k] = el.textContent;
    const en = I18N[k] && I18N[k][1];
    el.textContent = LANG === "en" && en ? en : I18N_SI[k];
  }
  if ($("lang")) $("lang").textContent = LANG === "en" ? "සිං" : "EN";
}

if ($("learn")) $("learn").onclick = () => learnFromMe().catch((e) => log("Learn: " + (e && e.message || e), "bad"));
if ($("stats")) $("stats").onclick = () => showStats().catch((e) => log("Stats: " + (e && e.message || e), "bad"));
if ($("st_save")) $("st_save").onclick = () => presetSave().catch((e) => log("Preset: " + (e && e.message || e), "bad"));
if ($("st_refresh")) $("st_refresh").onclick = () => presetsRefresh(false);
if ($("st_list")) $("st_list").onchange = () => presetApply($("st_list").value);
if ($("lang")) $("lang").onclick = () => { LANG = LANG === "en" ? "si" : "en"; try { localStorage.setItem("autowed.lang", LANG); } catch (e) {} applyLang(); };
try { applyLang(); } catch (e) {}
setTimeout(() => { presetsRefresh(true); }, 2500);


// =====================================================================================
// New layout: left menu (steps + settings), event pills, mode switch, moments list
// =====================================================================================
const TABS = {
  sync: ["STEP 0 · SYNC", "Sync by audio"], seq: ["STEP 1 · SEQUENCE", "Sequence"], edit: ["STEP 2 · EDIT", "Auto Edit"],
  batch: ["STEP 3 · BATCH", "Wedding list"], set: ["SETTINGS", "Settings"], log: ["LOG", "Log"],
};
let CUR_TAB = "edit";
function goLabel() {
  if (!$("go")) return;
  const left = QUEUE.filter((q) => q.status !== "done").length;
  $("go").textContent = CUR_TAB === "batch" ? `ලිස්ට් එක Edit කරන්න (${left})` : "Auto Edit";
}
function showTab(t) {
  CUR_TAB = t;
  goLabel();
  for (const b of document.querySelectorAll(".rbtn")) b.className = "rbtn" + (b.getAttribute("data-tab") === t ? " on" : "");
  for (const p of document.querySelectorAll(".pane")) p.className = "pane" + (p.id === "p_" + t ? " on" : "");
  const h = TABS[t] || ["", ""];
  if ($("h_step")) $("h_step").textContent = h[0];
  if ($("h_title")) $("h_title").textContent = t === "edit" && seqData ? seqData.name : h[1];
  try { localStorage.setItem("autowed.tab", t); } catch (e) {}
}
for (const b of document.querySelectorAll(".rbtn")) b.onclick = () => showTab(b.getAttribute("data-tab"));

function renderPills() {
  const box = $("evpills");
  if (!box) return;
  box.innerHTML = "";
  const sel = $("event");
  for (const o of sel.options) {
    const b = document.createElement("div");
    b.className = "pill" + (o.value === sel.value ? " on" : "");
    b.textContent = o.textContent.split("  ·  ")[0];
    b.onclick = () => { sel.value = o.value; try { localStorage.setItem("autowed.event", o.value); } catch (e) {} renderPills(); };
    box.appendChild(b);
  }
}
function renderKind() {
  // only manual edit
  const et = $("etype");
  et.value = "stacked";
  for (const b of document.querySelectorAll("#kindseg .seg")) b.className = "seg" + (b.getAttribute("data-k") === et.value ? " on" : "");
  if ($("etypehint")) $("etypehint").textContent = HINTS[et.value] || "";
}
for (const b of document.querySelectorAll("#kindseg .seg")) b.onclick = () => {
  $("etype").value = b.getAttribute("data-k");
  try { localStorage.setItem("autowed.kind", $("etype").value); } catch (e) {}
  renderKind();
};
try { const k = localStorage.getItem("autowed.kind"); if (k) $("etype").value = k; } catch (e) {}
renderKind();

// moments & checks after an edit, in the Edit page
// ---------- review list: every check, moment and cut of the last edit; click = go there ----------
let REVIEW = null;
function reviewItems(result) {
  const KIND = { applause: "අත්පොළසන් / cheer", smile: "සිනහ", hug: "බදාගැනීම / emotion" };
  const th = result.thumbs || {};
  const it = [];
  (result.bad || []).forEach((b, i) => it.push({ key: "bad:" + i, grp: "chk", t: b.start, end: b.end, col: "#e8a04a", txt: `⚠ ${b.name}: ${b.why}`, img: th["bad:" + i] }));
  (result.jumps || []).forEach((j, i) => it.push({ key: "jump:" + i, grp: "chk", t: j.time, col: "#e8c84a", txt: `⚠ Jump cut? ${j.why || ""}`, img: th["jump:" + i] }));
  (result.dead || []).forEach((d, i) => it.push({ key: "dead:" + i, grp: "chk", t: d.start, end: d.end, col: "#e8685a", txt: "⚠ කැමරා ඔක්කොම දුර්වලයි", img: th["dead:" + i] }));
  (result.clipping || []).forEach((c, i) => it.push({ key: "clip:" + i, grp: "chk", t: c.start, end: c.end, col: "#e8685a", txt: `⚠ Audio clipping ${c.name || ""}` }));
  (result.mic_gaps || []).forEach((g, i) => it.push({ key: "mic:" + i, grp: "chk", t: g.start, end: g.end, col: "#e8a04a", txt: `⚠ Mic drop-out ${g.name || ""}` }));
  (result.moments || []).forEach((m, i) => it.push({ key: "mom:" + i, grp: "mom", t: m.time, col: "#b48cff", txt: `★ ${KIND[m.kind] || m.kind}${m.cam != null ? " · Cam " + (m.cam + 1) : ""}`, img: th["mom:" + i] }));
  (result.rituals || []).forEach((r, i) => it.push({ key: "rit:" + i, grp: "mom", t: r.start, end: r.end, col: "#5ac8c8", txt: `◆ ${r.label}` }));
  (result.segments || []).forEach((sg, i) => { if (i > 0 && sg.cam >= 0) it.push({ key: "cut:" + i, grp: "cut", t: sg.start, col: "#9a9ba3", txt: `✂ Cam ${sg.cam + 1}${sg.end - sg.start < 1.2 ? " (කෙටියි)" : ""}` }); });
  it.sort((a, b) => a.t - b.t);
  return it;
}
function renderMoments(result, seqGuid) {
  const box = $("mlist");
  if (!box) return;
  let seen = [];
  try { const o = JSON.parse(localStorage.getItem("autowed.review") || "{}"); if (o.guid === seqGuid && o.range && result.range && Math.abs(o.range[0] - result.range[0]) < 0.5) seen = o.seen || []; } catch (e) {}
  const items = reviewItems(result);
  const grp = ["chk", "mom", "cut"].find((g) => items.some((x) => x.grp === g)) || "chk";
  REVIEW = { guid: seqGuid || null, range: result.range, items, grp, seen: new Set(seen) };
  if ($("t_mom")) $("t_mom").textContent = String((result.moments || []).length);
  reviewRender();
}
function reviewSave() {
  try { localStorage.setItem("autowed.review", JSON.stringify({ guid: REVIEW.guid, range: REVIEW.range, seen: [...REVIEW.seen] })); } catch (e) {}
}
function reviewRender() {
  const box = $("mlist"), chips = $("rvchips");
  if (!box || !REVIEW) return;
  const n = (g) => REVIEW.items.filter((x) => x.grp === g).length;
  if (chips) {
    chips.innerHTML = "";
    for (const [g, label] of [["chk", "Checks"], ["mom", "Moments"], ["cut", "Cuts"]]) {
      const c = document.createElement("div");
      const left = REVIEW.items.filter((x) => x.grp === g && !REVIEW.seen.has(x.key)).length;
      c.className = "chip" + (REVIEW.grp === g ? " on" : "");
      c.textContent = `${label} ${left}/${n(g)}`;
      c.onclick = () => { REVIEW.grp = g; reviewRender(); };
      chips.appendChild(c);
    }
  }
  box.innerHTML = "";
  const list = REVIEW.items.filter((x) => x.grp === REVIEW.grp);
  if (!list.length) { const d = document.createElement("div"); d.className = "mute"; d.textContent = "මෙතන බලන්න දෙයක් නැහැ."; box.appendChild(d); return; }
  for (const x of list.slice(0, 250)) {
    const row = document.createElement("div");
    row.className = "rv" + (REVIEW.seen.has(x.key) ? " done" : "");
    if (x.img) { const im = document.createElement("img"); im.src = x.img; row.appendChild(im); }
    const tx = document.createElement("div"); tx.className = "tx";
    const a = document.createElement("div"); a.textContent = x.txt; a.style.color = x.col;
    const b = document.createElement("div"); b.className = "tm"; b.textContent = fmt(x.t) + (x.end ? " → " + fmt(x.end) : "");
    tx.appendChild(a); tx.appendChild(b); row.appendChild(tx);
    const ck = document.createElement("div"); ck.className = "ck"; ck.textContent = REVIEW.seen.has(x.key) ? "✓" : "";
    ck.title = "බැලුවා";
    ck.onclick = (ev) => { ev.stopPropagation(); if (REVIEW.seen.has(x.key)) REVIEW.seen.delete(x.key); else REVIEW.seen.add(x.key); reviewSave(); reviewRender(); };
    row.appendChild(ck);
    row.onclick = () => reviewGo(x).catch((e) => log("Review: " + (e && e.message || e), "warn"));
    box.appendChild(row);
  }
  if (list.length > 250) { const d = document.createElement("div"); d.className = "mute"; d.textContent = `… තව ${list.length - 250}`; box.appendChild(d); }
}
async function reviewGo(x) {
  const project = await ppro.Project.getActiveProject();
  if (!project) return;
  let seq = await project.getActiveSequence();
  if (REVIEW.guid && (!seq || seq.guid.toString() !== REVIEW.guid)) {
    const s = (await project.getSequences()).find((q) => q.guid.toString() === REVIEW.guid);
    if (s) { await project.openSequence(s); await sleep(400); seq = await project.getActiveSequence(); }
  }
  if (!seq || typeof seq.setPlayerPosition !== "function") return;
  await seq.setPlayerPosition(tt(Math.max(0, x.t)));
  REVIEW.seen.add(x.key); reviewSave(); reviewRender();
}

// move to the page that shows what is running
(function wrapForTabs() {
  const sy = $("sync").onclick; $("sync").onclick = () => { showTab("sync"); return sy && sy(); };
  const qr = $("qrun").onclick; $("qrun").onclick = () => { showTab("edit"); return qr && qr(); };
})();
if ($("rver")) $("rver").textContent = "v" + PANEL_VERSION;
try { showTab(localStorage.getItem("autowed.tab") || "edit"); } catch (e) { showTab("edit"); }

// Live cuts on/off is remembered (off = every cut at the end, the safe way if Premiere crashes)
if ($("x_live")) {
  try { const v = localStorage.getItem("autowed.out"); if (v) $("x_live").value = v; } catch (e) {}
  $("x_live").addEventListener("change", () => { try { localStorage.setItem("autowed.out", $("x_live").value); } catch (e) {} });
}

// "Enabled audio only" is remembered too
if ($("x_audon")) {
  try { const v = localStorage.getItem("autowed.audon"); if (v) $("x_audon").value = v; } catch (e) {}
  $("x_audon").addEventListener("change", () => { try { localStorage.setItem("autowed.audon", $("x_audon").value); } catch (e) {} });
}

// Camera audio cut / not cut is remembered
if ($("x_alink")) {
  try { const v = localStorage.getItem("autowed.alink"); if (v) $("x_alink").value = v; } catch (e) {}
  $("x_alink").addEventListener("change", () => { try { localStorage.setItem("autowed.alink", $("x_alink").value); } catch (e) {} });
}

for (const id of ["x_sact", "x_smin"]) if ($(id)) {
  try { const v = localStorage.getItem("autowed." + id); if (v) $(id).value = v; } catch (e) {}
  $(id).addEventListener("change", () => { try { localStorage.setItem("autowed." + id, $(id).value); } catch (e) {} });
}

// Cache clear: the engine's analysis / audio / sync caches (also this sequence's media cache folders)
async function clearCache() {
  if (busy) { log("Edit එකක් run වෙනවා: ඉවර වුණාම cache clear කරන්න.", "warn"); return; }
  let paths = [];
  try {
    const sd = seqData || await readSequence();
    if (sd) {
      for (const c of sd.cams || []) for (const cl of c.clips || []) if (cl.path) paths.push(cl.path);
      for (const tr of sd.audio || []) for (const cl of tr) if (cl.path) paths.push(cl.path);
    }
  } catch (e) {}
  paths = [...new Set(paths)];
  let info = null;
  try { info = await call("GET", "/api/cache"); } catch (e) {}
  const r = await call("POST", "/api/cache/clear", { media_paths: paths });
  if (r && r.ok) {
    log(`Cache clear කළා: files ${r.files} · ${r.freed_mb} MB. ඊළඟ run එකේ ආයෙ analyze වෙනවා.`, "ok");
    if (r.errors && r.errors.length) log("සමහර files පාවිච්චි වෙන නිසා මකන්න බැරි වුණා: " + r.errors.slice(0, 2).join(" | "), "warn");
  } else log("Cache clear: " + ((r && r.error) || "බැරි වුණා"), "bad");
}
if ($("cacheclr")) $("cacheclr").onclick = () => clearCache().catch((e) => log("Cache clear: " + (e && e.message || e), "bad"));

// ---------- Wedding එකම: events find -> batch list -> run (optionally at a set time) ----------
async function weddingAuto() {
  if (busy) return;
  const before = QUEUE.length;
  await findEvents();
  const added = QUEUE.length - before;
  if (added <= 0) { log("Events හමු වුණේ නැහැ: Batch එක run කළේ නැහැ.", "warn"); return; }
  log(`Wedding එකම: events ${added}ක් ලිස්ට් එකට දැම්මා. දැන් ඔක්කොම edit කරනවා…`, "ok");
  notifyPhone("AutoWed: wedding එක පටන් ගත්තා", `Events ${added}ක් හමු වුණා. Edit එක ඉවර වුණාම කියන්නම්.`, "movie_camera");
  await qRun();
}
let SCHED = null;
function scheduleRun() {
  const v = ($("q_at") && $("q_at").value) || "";
  if (SCHED) { clearTimeout(SCHED); SCHED = null; log("Schedule එක අයින් කළා.", "mute"); if ($("qsched")) $("qsched").textContent = "වෙලාවට Run"; return; }
  const m = /^(\d{1,2}):(\d{2})$/.exec(v);
  if (!m) { log("වෙලාව HH:MM විදිහට දාන්න (උදා 23:30).", "warn"); return; }
  const now = new Date(), at = new Date();
  at.setHours(+m[1], +m[2], 0, 0);
  if (at <= now) at.setDate(at.getDate() + 1);
  const ms = at - now;
  const mode = ($("q_mode") && $("q_mode").value) || "list";
  SCHED = setTimeout(() => {
    SCHED = null;
    if ($("qsched")) $("qsched").textContent = "වෙලාවට Run";
    (mode === "wedding" ? weddingAuto() : qRun()).catch((e) => log("Schedule: " + (e && e.message || e), "bad"));
  }, ms);
  if ($("qsched")) $("qsched").textContent = `${v} ට run වෙනවා (cancel)`;
  log(`${v} ට ${mode === "wedding" ? "wedding එකම auto" : "Batch ලිස්ට් එක"} run වෙනවා (තව විනාඩි ${Math.round(ms / 60000)}). Premiere සහ panel එක open කරලා තියන්න.`, "ok");
}
if ($("wedauto")) $("wedauto").onclick = () => weddingAuto().catch((e) => log("Wedding auto: " + (e && e.message || e), "bad"));
if ($("qsched")) $("qsched").onclick = scheduleRun;
if ($("x_ntfy")) {
  $("x_ntfy").value = ntfyTopic();
  $("x_ntfy").addEventListener("change", () => { try { localStorage.setItem("autowed.ntfy", $("x_ntfy").value.trim()); } catch (e) {} });
}
if ($("x_ntfy_on")) {
  try { const v = localStorage.getItem("autowed.ntfy_on"); if (v) $("x_ntfy_on").value = v; } catch (e) {}
  $("x_ntfy_on").addEventListener("change", () => { try { localStorage.setItem("autowed.ntfy_on", $("x_ntfy_on").value); } catch (e) {} });
}
if ($("ntfytest")) $("ntfytest").onclick = async () => {
  const ok = await notifyPhone("AutoWed test", "Phone notifications වැඩ ✔", "bell");
  log(ok ? `Test notification එක යැව්වා (topic: ${($("x_ntfy") && $("x_ntfy").value) || ntfyTopic()}).` : "Notification එක යවන්න බැරි වුණා (internet / topic බලන්න).", ok ? "ok" : "bad");
};

// ---------- licence ----------
let LIC_CHECKED = false, LIC = null;
function showLicense(st) {
  LIC = st || LIC;
  if ($("licov")) $("licov").style.display = "flex";
  if ($("licmsg")) $("licmsg").textContent = (st && st.message) || "License key එක දාන්න, නැත්නම් free trial එක පටන් ගන්න.";
  if ($("licpc")) $("licpc").textContent = st && st.machine ? "PC ID: " + st.machine : "";
}
function hideLicense() { if ($("licov")) $("licov").style.display = "none"; }
function renderLicInfo(st) {
  if (!$("licinfo")) return;
  if (!st || !st.ok) { $("licinfo").textContent = "නැහැ"; return; }
  $("licinfo").textContent = st.plan === "trial"
    ? `Free trial · ${st.expires} දක්වා (දවස් ${st.days_left})`
    : `${st.name ? st.name + " · " : ""}${st.key} · ${st.expires} දක්වා (දවස් ${st.days_left})`;
}
async function checkLicense() {
  let st = null;
  try { st = await call("GET", "/api/license"); } catch (e) { return; }
  LIC = st;
  renderLicInfo(st);
  if (!st || !st.ok) { showLicense(st); return; }
  hideLicense();
  if (st.days_left <= 14) log(`License එක ${st.expires} දින expire වෙනවා (දවස් ${st.days_left}). Renew කරන්න: ${st.price}`, "warn");
}
async function licActivate() {
  const key = (($("lickey") && $("lickey").value) || "").trim().toUpperCase();
  if (!/^[A-Z0-9]{2,6}(-[A-Z0-9]{3,6}){2,5}$/.test(key)) { $("licmsg").textContent = "Key එක හරියට දාන්න (උදා. AW-XXXX-XXXX-XXXX)."; return; }
  $("licmsg").textContent = "Activate කරනවා…";
  const r = await call("POST", "/api/license/activate", { key });
  if (r && r.ok) { hideLicense(); renderLicInfo(r); log(`✔ License activate කළා · ${r.expires} දක්වා. ස්තූතියි!`, "ok"); }
  else $("licmsg").textContent = (r && r.message) || "Activate කරන්න බැරි වුණා.";
}
async function licTrial() {
  $("licmsg").textContent = "Trial එක පටන් ගන්නවා…";
  const r = await call("POST", "/api/license/trial", {});
  if (r && r.ok) { hideLicense(); renderLicInfo(r); log(`Free trial එක පටන් ගත්තා · ${r.expires} දක්වා.`, "ok"); }
  else $("licmsg").textContent = (r && r.message) || "Trial එක පටන් ගන්න බැරි වුණා.";
}
if ($("licact")) $("licact").onclick = () => licActivate().catch((e) => { $("licmsg").textContent = String(e && e.message || e); });
if ($("lictrial")) $("lictrial").onclick = () => licTrial().catch((e) => { $("licmsg").textContent = String(e && e.message || e); });
if ($("licrefresh")) $("licrefresh").onclick = async () => {
  try { const st = await call("POST", "/api/license/refresh", {}); renderLicInfo(st); if (!st.ok) showLicense(st); else log("License එක OK ✔", "ok"); }
  catch (e) { log("License check: " + (e && e.message || e), "bad"); }
};
if ($("licremove")) $("licremove").onclick = async () => {
  try { const st = await call("POST", "/api/license/remove", {}); renderLicInfo(st); showLicense(st); } catch (e) {}
};
setInterval(() => { if (LIC_CHECKED) checkLicense().catch(() => {}); }, 3600 * 1000);
