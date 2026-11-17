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
      try { const j = await httpCall(base, method, path, body); transport = "http"; engineBase = base; return j; }
      catch (e) { lastNetErr = String(e); }
    }
  }
  const j = await fileCall(method, path, body, timeoutMs);
  transport = "file";
  return j;
}

function log(msg, cls) {
  const el = $("log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
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
    $("eng").textContent = `● engine v${p.version} · GPU ${p.nvdec ? "ON" : "OFF"} · ${transport}`;
    $("eng").className = "ok";
    if (!$("event").options.length) loadEvents();
    return true;
  } catch (e) {
    $("eng").textContent = "● engine offline";
    $("eng").className = "bad";
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
}

async function checkUpdate(manual) {
  try {
    const u = await call("GET", manual ? "/api/update?force=1" : "/api/update");
    if (u.available) {
      $("upd").style.display = "block";
      $("updtxt").textContent = `අලුත් version එකක්: v${u.latest} (දැන් v${u.current})`;
      $("updnotes").textContent = (u.notes || "").split("\n").slice(0, 4).join("\n");
    } else if (manual) {
      log(u.error ? "Update check කරන්න බැරි වුණා: " + u.error : `ඔබ ළඟ තියෙන්නේ අලුත්ම version එක (v${u.current}) ✔`, u.error ? "bad" : "ok");
    }
  } catch (e) { if (manual) log("Engine එකට සම්බන්ධ වෙන්න බැහැ: " + e, "bad"); }
}

async function installUpdate() {
  $("updbtn").disabled = true;
  // save the open project first: the update closes Premiere and opens it again afterwards
  let reopen = "";
  try {
    const project = await ppro.Project.getActiveProject();
    if (project) {
      const ok = await project.save();
      reopen = project.path || "";
      log(ok ? "Project එක save කළා ✔" : "Project save වුණේ නැද්ද කියලා සැකයි", ok ? "ok" : "warn");
    }
  } catch (e) { log("Save: " + e, "warn"); }
  try {
    const r = await call("POST", "/api/update/install", { close_premiere: true, reopen });
    if (r.status === "error") { log(r.message, "bad"); $("updbtn").disabled = false; return; }
  } catch (e) { log("Update පටන් ගන්න බැරි වුණා: " + e, "bad"); $("updbtn").disabled = false; return; }
  const poll = async () => {
    let s;
    try { s = await call("GET", "/api/update/status", null, 3000); }
    catch (e) {
      $("updtxt").textContent = "Update install වෙනවා... විනාඩියකින් Premiere restart කරන්න.";
      $("updnotes").textContent = ""; $("updbtn").style.display = "none";
      return;
    }
    if (s.status === "downloading") $("updtxt").textContent = `Download වෙනවා... ${Math.round(s.progress * 100)}% · ඉවර වුණාම Premiere close වෙලා update වෙලා ආයෙ open වෙනවා`;
    else if (s.status === "installing") $("updtxt").textContent = s.message;
    if (s.status === "error") { log(s.message, "bad"); $("updbtn").disabled = false; return; }
    setTimeout(poll, 700);
  };
  poll();
}

// ---------- progress display ----------
const STAGES = ["prepare", "audio", "analysis", "finish"];
const STAGE_TXT = {
  prepare: "Sequence සූදානම් කරමින්…", queued: "පෝලිමේ…", sync: "Audio sync…",
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
  return { start, end, srcIn, speed, path, why, name: await item.getName() };
}

async function readSequence() {
  $("tracks").innerHTML = "";
  const project = await ppro.Project.getActiveProject();
  const seq = project && (await project.getActiveSequence());
  if (!seq) { log("Active sequence එකක් නැහැ.", "bad"); return null; }
  let fps = 25;
  try { const tb = parseFloat(await seq.getTimebase()); if (tb > 0) fps = TICKS / tb; } catch (e) {}
  const endT = (await seq.getEndTime()).seconds;
  let inT = 0, outT = endT;
  try { inT = (await seq.getInPoint()).seconds; outT = (await seq.getOutPoint()).seconds; } catch (e) {}
  // In/Out must be set by the editor: AutoWed only ever touches that part of the timeline
  const hasInOut = outT > inT + 0.5 && !(inT < 0.01 && Math.abs(outT - endT) < 0.05);
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
      clips.push({ path: c.path, offset: c.start - c.srcIn, src_in: c.srcIn, tl_start: c.start, tl_end: c.end });
    }
    if (clips.length) cams.push({ name: track.name || `V${t + 1}`, track: t, clips });
  }
  const audio = [];
  const aCount = await seq.getAudioTrackCount();
  for (let t = 0; t < aCount; t++) {
    const track = await seq.getAudioTrack(t);
    try { if (await track.isMuted()) continue; } catch (e) {}
    const items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
    const clips = [];
    for (const it of items) {
      const c = await readItem(it);
      if (!c.path || Math.abs(c.speed - 1) > 0.001) continue;
      clips.push({ path: c.path, offset: c.start - c.srcIn, src_in: c.srcIn, tl_start: c.start, tl_end: c.end });
    }
    if (clips.length) audio.push(clips);
  }
  warnings.forEach((w) => log(w, "warn"));
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
async function itemsOf(track) {
  if (typeof track === "function") track = await track();
  const out = [];
  for (const it of track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
    out.push({ it, a: (await it.getStartTime()).seconds, b: (await it.getEndTime()).seconds });
  }
  return out.sort((x, y) => x.a - y.a);
}
async function readTI(it) {
  return { a: (await it.getStartTime()).seconds, b: (await it.getEndTime()).seconds,
           inp: (await it.getInPoint()).seconds, out: (await it.getOutPoint()).seconds };
}

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
  const setTo = async (x) => { const it = await getIt(); transact(project, "AutoWed cut", () => [it.createSetInPointAction(tt(x))]); };
  const readIn = async () => (await (await getIt()).getInPoint()).seconds;
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
  const it = await getIt();
  if (f === "start") transact(project, "AutoWed cut", () => [it.createSetStartAction(tt(want.a))]);
  if (f === "end") transact(project, "AutoWed cut", () => [it.createSetEndAction(tt(want.b))]);
}
const fieldOk = (c, want, f) => f === "in" ? Math.abs(c.inp - want.inp) <= 0.004
  : f === "start" ? Math.abs(c.a - want.a) <= 0.002 : Math.abs(c.b - want.b) <= 0.002;
const allOk = (c, want) => fieldOk(c, want, "in") && fieldOk(c, want, "start") && fieldOk(c, want, "end");

async function fixItem(project, getIt, want) {
  const read = async () => readTI(await getIt());
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

async function removeItems(project, seq, items, mediaType) {
  const sel = await selectionOf(items);
  transact(project, "AutoWed undo cut", () => [ppro.SequenceEditor.getEditor(seq).createRemoveItemsAction(sel, false, mediaType)]);
}

async function emulatedRazor(project, seq, vIndex, t) {
  // Premiere 26.5 also invalidates sequence and track objects after an edit: fetch them every time
  const g = seq.guid.toString();
  const seqNow = async () => (seq = await freshSeq(project, seq, g));
  const track = async () => (await seqNow()).getVideoTrack(vIndex);
  const hit = (await itemsOf(track)).find((x) => x.a < t - EPS && t + EPS < x.b);
  if (!hit) return false;
  const { a, b } = hit;
  const o = await readTI(hit.it);
  const rate = o.out > o.inp && b > a ? (o.out - o.inp) / (b - a) : 1;   // source seconds per timeline second
  const srcAt = (time) => o.inp + (time - a) * rate;
  const left = t - a, right = b - t;
  const cloneAction = async (win, offset) => {
    const it = await locate(track, win);
    const editor = ppro.SequenceEditor.getEditor(await seqNow());
    transact(project, "AutoWed cut", () => [editor.createCloneTrackItemAction(it, tt(offset), 0, 0, true, false)]);
  };
  let cloneWin = null;
  try {
    if (left >= right) {
      await fixItem(project, locator(track, [t, b]), { a: t, b, inp: srcAt(t) });          // original keeps [t, b]
      await cloneAction([t, b], -left);
      cloneWin = [a, t];
      await fixItem(project, locator(track, cloneWin, [t, b]), { a, b: t, inp: o.inp });    // clone fills [a, t]
    } else {
      await fixItem(project, locator(track, [a, t]), { a, b: t, inp: o.inp });              // original keeps [a, t]
      await cloneAction([a, t], left);
      cloneWin = [t, b];
      await fixItem(project, locator(track, cloneWin, [a, t]), { a: t, b, inp: srcAt(t) }); // clone fills [t, b]
    }
  } catch (e) {
    // put this clip back exactly as it was before the failed cut
    try {
      if (cloneWin) {
        const c = await locate(track, cloneWin);
        if (c) await removeItems(project, await seqNow(), [c], ppro.Constants.MediaType.VIDEO);
      }
      await fixItem(project, locator(track, [a, b]), { a, b, inp: o.inp });
      log(`Cut ${fmt(t)} ආපස්සට හැරෙව්වා: clip එක කලින් තිබුණු විදිහටම තියෙනවා.`, "warn");
    } catch (e2) {
      log(`⚠ Cut ${fmt(t)} ආපස්සට හරවන්න බැරි වුණා (V${vIndex + 1}). Ctrl+Z එකෙන් "AutoWed cut" ආපස්සට ගන්න.`, "bad");
    }
    throw e;
  }
  return true;
}

async function razorTracks(project, seq, tracks, t, native) {
  if (native) {
    const editor = ppro.SequenceEditor.getEditor(seq);
    const V = ppro.Constants.MediaType.VIDEO;
    transact(project, "AutoWed cut", () => tracks.map((ti) => editor.createRazorTrackAction(V, ti, tt(t), false, V)));
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
  let followed = false;
  async function cutAt(times) {
    for (const t of times) {
      const key = t.toFixed(3);
      if (done.has(key) || t <= 0.001) continue;
      try { await razorTracks(project, await tgt(), cutTracks, t, native); }
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
    const acts = [];
    let processed = enabledUntil;
    for (let c = 0; c < trackOfCam.length; c++) {
      const track = await target.getVideoTrack(trackOfCam[c]);
      for (const { it, a, b } of await itemsOf(track)) {
        if (b <= enabledUntil + 1e-3 || a < rangeT0 - 1e-3 || b > until + 1e-3) continue;
        const chosen = camAt((a + b) / 2);
        if (chosen === null) continue;
        if (c === 0) processed = Math.max(processed, b);
        const want = chosen !== c;
        let cur = false;
        try { cur = await it.isDisabled(); } catch (e) {}
        if (cur !== want) acts.push(it.createSetDisabledAction(want));
      }
    }
    if (acts.length) transact(project, "AutoWed angles", () => acts);
    enabledUntil = processed;
  }
  return {
    async apply(segments, until, final) {
      const cuts = [rangeT0];
      for (let i = 1; i < segments.length; i++) cuts.push(segments[i].start);
      if (final) cuts.push(rangeT1);
      await cutAt(cuts);
      await enablePieces(segments, until);
      // keep the timeline view on the newest cut, so the edit can be watched as it happens
      try {
        const seq = await tgt();
        if (!followed) { followed = true; try { await project.openSequence(seq); } catch (e) {} }
        const last = cuts.length ? Math.min(Math.max(...cuts), rangeT1) : until;
        if (typeof seq.setPlayerPosition === "function") await seq.setPlayerPosition(tt(last));
      } catch (e) {}
    },
  };
}

// ---------- live strip in the panel ----------
const CAM_COLORS = ["#d4a24c", "#3fa7d6", "#e05d9e", "#5fb37a", "#a77de0", "#e07b39"];
function renderStrip(segments, t0, t1, until) {
  const strip = $("strip");
  strip.innerHTML = "";
  const total = Math.max(0.001, t1 - t0);
  for (const s of segments) {
    const d = document.createElement("div");
    d.style.width = (Math.max(0, s.end - s.start) / total * 100).toFixed(3) + "%";
    d.style.height = "16px";
    d.style.backgroundColor = CAM_COLORS[s.cam % CAM_COLORS.length];
    d.style.borderRight = "1px solid #1b1b1b";
    strip.appendChild(d);
  }
  const rest = document.createElement("div");
  rest.style.flex = "1"; rest.style.height = "16px"; rest.style.backgroundColor = "#1b1b1b";
  strip.appendChild(rest);
  $("striplbl").textContent = `Cuts ${Math.max(0, segments.length - 1)} · ${fmt((until || t0) - t0)} / ${fmt(total)}`;
}
function renderLegend(names) {
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
async function camerasInsideNest(nest) {
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
      clips.push({ path: c.path, offset: c.start - c.srcIn + d, src_in: c.srcIn, tl_start: s, tl_end: e });
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
  const nName = await uniqueName(project, `${sd.name} - AutoWed Nest ${fmt(range[0])}-${fmt(range[1])}`);
  await renameSequence(project, nseq, nName);
  const seqG = sd.seq.guid.toString();
  const outer = async () => (await project.getSequences()).find((x) => x.guid.toString() === seqG) || sd.seq;
  const native = hasNativeRazor(await outer());
  const camTracks = sd.cams.map((c) => c.track);
  // 2) clear the camera video between In and Out (audio is not touched)
  for (const t of range) await razorTracks(project, await outer(), camTracks, t, native);
  const victims = [];
  for (const ti of camTracks) {
    for (const x of await itemsOf(await (await outer()).getVideoTrack(ti))) {
      if (x.a >= range[0] - EPS && x.b <= range[1] + EPS) victims.push(x.it);
    }
  }
  if (victims.length) {
    const sel = await selectionOf(victims);
    const o1 = await outer();
    transact(project, "AutoWed nest", () => [ppro.SequenceEditor.getEditor(o1).createRemoveItemsAction(sel, false, ppro.Constants.MediaType.VIDEO)]);
  }
  // 3) drop the nest on the first camera track at In; its audio goes to a new track and is removed
  const vIdx = Math.min(...camTracks);
  const o2 = await outer();
  const aCount = await o2.getAudioTrackCount();
  const nFresh = (await project.getSequences()).find((x) => x.guid.toString() === nGuid) || nseq;
  const nestItem = await nFresh.getProjectItem();
  transact(project, "AutoWed nest", () => [ppro.SequenceEditor.getEditor(o2).createOverwriteItemAction(nestItem, tt(range[0]), vIdx, aCount)]);
  const extraAudio = [];
  const o3 = await outer();
  const aNow = await o3.getAudioTrackCount();
  for (let i = aCount; i < aNow; i++) for (const x of await itemsOf(await o3.getAudioTrack(i))) extraAudio.push(x.it);
  if (extraAudio.length) {
    const sel = await selectionOf(extraAudio);
    const o4 = await outer();
    transact(project, "AutoWed nest", () => [ppro.SequenceEditor.getEditor(o4).createRemoveItemsAction(sel, false, ppro.Constants.MediaType.AUDIO)]);
  }
  sd.seq = await outer();
  const placed = (await itemsOf(await sd.seq.getVideoTrack(vIdx))).find((x) => Math.abs(x.a - range[0]) < 0.05);
  if (!placed) throw new Error("nest clip timeline එකට දාන්න බැරි වුණා");
  log(`In/Out කොටස nest කළා: "${nName}" (V${vIdx + 1})`, "ok");
  return { track: vIdx, it: placed.it, start: placed.a, end: placed.b, srcIn: (await placed.it.getInPoint()).seconds, name: nName, guid: nGuid };
}

// ---------- the main flow ----------
async function autoEdit() {
  if (busy) return;
  busy = true; $("go").disabled = true;
  try { await runAutoEdit(); }
  catch (e) { log("Error: " + (e && e.message || e), "bad"); }
  finally { busy = false; $("go").disabled = false; }
}

async function runAutoEdit() {
  const sd = await readSequence();
  if (!sd) return;
  if (!sd.hasInOut) { log("මුලින්ම timeline එකේ edit කරන්න ඕන කොටසට In (I) සහ Out (O) දාන්න. AutoWed edit කරන්නේ ඒ කොටස විතරයි.", "bad"); return; }
  if (!(await ping())) {
    log("Engine එකට සම්බන්ධ වෙන්න බැහැ. Start menu › AutoWed › AutoWed Engine run කරලා ආයෙ උත්සාහ කරන්න.", "bad");
    log("විස්තර: " + lastPingErr, "mute");
    return;
  }
  const project = await ppro.Project.getActiveProject();
  sd.guid = sd.seq.guid.toString();
  const kind = $("etype").value;
  const range = [sd.inT, sd.outT];
  const event = $("event").value || "auto";
  const opt = $("event").selectedOptions && $("event").selectedOptions[0];
  const evName = opt ? opt.textContent.split("  ·  ")[0] : event;
  showProgress("prepare", 0, null);

  let cams, applier, nest = null, nestInfo = null;
  if (kind === "stacked") {
    if (sd.cams.length < 2) { log("Manual cutting එකට කැමරා tracks 2ක් වත් ඕන (nest එකක් නෙවෙයි).", "bad"); return; }
    cams = sd.cams;
    applier = makeApplier(project, sd.seq, "stacked", cams.map((c) => c.track), cams.map((c) => c.track), range[0], range[1]);
    log(`"${sd.name}" timeline එකේම, ${fmt(range[0])} → ${fmt(range[1])} අතර cuts live වැටෙනවා…`, "ok");
  } else {
    nest = sd.nestInRange;
    if (nest) log(`ඔබේ nest එක පාවිච්චි කරනවා: "${nest.name}"`, "ok");
    else {
      if (sd.cams.length < 2) { log("කැමරා tracks 2ක් වත් හමු නොවුණි.", "bad"); return; }
      nest = await nestInPlace(project, sd, range);
    }
    nestInfo = await camerasInsideNest(nest);
    cams = nestInfo.cams;
    if (cams.length < 2) { log("Nest එක ඇතුළේ කැමරා tracks 2ක් වත් හමු නොවුණි.", "bad"); return; }
    applier = makeApplier(project, sd.seq, "multicam", [nest.track], [], Math.max(range[0], nest.start), Math.min(range[1], nest.end));
    log(`Multicam: nest clip එක "${sd.name}" timeline එකේම live cut වෙනවා…`, "ok");
  }
  renderLegend(cams.map((c, i) => `Cam ${i + 1}`));
  renderStrip([], range[0], range[1], range[0]);

  const body = {
    project: { name: sd.name, fps: sd.fps, master_cam: 0, range,
               cameras: cams.map((c) => ({ name: c.name, clips: c.clips })), audio_tracks: sd.audio },
    event,
    options: { min_shot: numOrNull("min_shot"), max_shot: numOrNull("max_shot"), layout: "stacked",
               xml_name: await uniqueName(project, `${sd.name} - AutoWed ${evName} ${stamp()}`) },
  };
  log(`Auto edit: ${evName} · ${kind === "multicam" ? "Multicamera" : "Manual cutting"} · ${fmt(range[0])} → ${fmt(range[1])}`);
  const job = (await call("POST", "/api/edit", body)).job;
  let shown = 0, lastSegCount = -1;
  while (true) {
    await sleep(1000);
    let j;
    try { j = await call("GET", "/api/job/" + job); } catch (e) { continue; }
    showProgress(j.stage || "queued", j.progress || 0, j.eta);
    for (; shown < j.log.length; shown++) log(j.log[shown], j.log[shown].startsWith("ERROR") ? "bad" : "");
    if (j.segments && j.segments.length !== lastSegCount) {
      lastSegCount = j.segments.length;
      renderStrip(j.segments, range[0], range[1], j.committed_until || range[0]);
      if (applier) {
        try { await applier.apply(j.segments, j.committed_until || range[0], false); }
        catch (e) {
          log("Timeline cut නැවැත්තුවා: " + (e && e.message || e), "bad");
          log("Live cuts නැවැත්තුවා. Analysis එක ඉවර වෙනකම් යනවා (strip එකේ ප්‍රතිඵලය බලන්න).", "warn");
          applier = null;
        }
      }
    }
    if (j.status === "error") return;
    if (j.status === "done") { await finish(project, kind, sd, nest, nestInfo, applier, cams, j.result); return; }
  }
}

async function addDeadMarkers(project, seq, result) {
  if (!result.dead || !result.dead.length) return;
  try {
    const markers = await ppro.Markers.getMarkers(seq);
    transact(project, "AutoWed markers", () => result.dead.map((d) =>
      markers.createAddMarkerAction("CHECK – කැමරා සියල්ල දුර්වලයි", "Comment", tt(d.start), tt(Math.max(0.1, d.end - d.start)), "AutoWed")));
  } catch (e) { log("Markers: " + e, "warn"); }
}

function stamp() {
  const d = new Date(), p2 = (n) => String(n).padStart(2, "0");
  return `${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}`;
}

// a copy of the finished edit as its own sequence (from the engine's XML), next to the in-place edit
async function importCopy(proj, result) {
  if (!result || !result.xml) return;
  try {
    const ok = await proj.importFiles([result.xml], true);
    if (ok === false) throw new Error("import refused");
    log(`Copy එකක් අලුත් sequence එකක් විදිහට import කළා: "${result.xml.split(/[\\/]/).pop().replace(/\.xml$/i, "")}"`, "ok");
  } catch (e) {
    log("Copy sequence එක import කරන්න බැරි වුණා (" + (e && e.message || e) + "). File › Import එකෙන් ගන්න: " + result.xml, "warn");
  }
}

async function finish(project, kind, sd, nest, nestInfo, applier, cams, result) {
  showProgress("finish", 0.99, null);
  renderStrip(result.segments, result.range[0], result.range[1], result.range[1]);
  const s = result.stats;
  if (applier) {
    try { await applier.apply(result.segments, result.range[1], true); }
    catch (e) { log("Timeline cut: " + (e && e.message || e), "bad"); applier = null; }
  }
  reportSyncWarns();
  sd.seq = await freshSeq(project, sd.seq, sd.guid);
  await addDeadMarkers(project, sd.seq, result);
  if (kind === "stacked") {
    if (!applier) {
      log("Timeline එකේ cut කරන්න බැරි වුණ නිසා ඔබේ timeline එකට තවත් වෙනස්කම් කළේ නැහැ (අලුත් sequence එකකුත් හැදුවේ නැහැ).", "warn");
      await importCopy(project, result);
      log("Razor test එක ඔබලා ඒකේ screenshot එකක් එවන්න.", "mute");
      showProgress("done", 1, null);
      return;
    }
    await importCopy(project, result);
    showProgress("done", 1, null);
    log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s · angle මාරු කරන්න කෑල්ල select කරලා Shift+E.`, "ok");
    return;
  }
  // multicam: angles can only be written into the project file -> save, close, write, reopen
  log("Multicam angles ලියනවා: project එක save කරලා තත්පර කිහිපයකට close කරනවා…");
  const path = project.path;
  const seqName = sd.name;
  if (!(await project.save())) log("Project save වුණේ නැද්ද කියලා සැකයි – දිගටම යනවා", "warn");
  const segsNest = result.segments.map((x) => ({ ...x, start: x.start - nestInfo.d, end: x.end - nestInfo.d }));
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
};
$("etype").addEventListener("change", () => { $("etypehint").textContent = HINTS[$("etype").value]; });
$("etypehint").textContent = HINTS.multicam;
$("updbtn").addEventListener("click", () => installUpdate());
$("chk").addEventListener("click", () => checkUpdate(true));
$("read").addEventListener("click", () => readSequence().catch((e) => log("Error: " + e, "bad")));
$("rtest").addEventListener("click", () => razorTest());
$("go").addEventListener("click", () => autoEdit());
$("ver").textContent = "AutoWed v" + PANEL_VERSION;
ping().then((ok) => { if (ok) checkUpdate(false); });
setInterval(ping, 15000);
