/* PoruwaCut – Premiere Pro UXP panel (v0.2)
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
  try {
    inT = (await seq.getInPoint()).seconds; outT = (await seq.getOutPoint()).seconds;
    if (!(outT > inT)) { inT = 0; outT = endT; }
  } catch (e) {}
  const cams = [], warnings = [];
  const vCount = await seq.getVideoTrackCount();
  for (let t = 0; t < vCount; t++) {
    const track = await seq.getVideoTrack(t);
    const items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
    const clips = [];
    for (const it of items) {
      const c = await readItem(it);
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
  $("seqinfo").textContent = `${seq.name} · ${fps.toFixed(2)} fps · In ${fmt(inT)} → Out ${fmt(outT)} · audio tracks ${audio.length}`;
  cams.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "trk";
    row.innerHTML = `<span class="nm">Cam ${i + 1} · ${c.name}</span><span class="c">${c.clips.length} clips</span>`;
    $("tracks").appendChild(row);
  });
  if (cams.length < 2) { log("කැමරා tracks 2ක් වත් හමු නොවුණි (එක් කැමරාවකට එක video track එකක් ඕන).", "bad"); return null; }
  seqData = { seq, name: seq.name, guid: seq.guid.toString(), fps, inT, outT, endT, cams, audio, vCount };
  log(`කැමරා ${cams.length}ක් කියෙව්වා ✔`, "ok");
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

async function renameSequence(project, seq, name) {
  try {
    const pi = await seq.getProjectItem();
    transact(project, "PoruwaCut rename", () => [pi.createSetNameAction(name)]);
  } catch (e) { log("Rename: " + e, "warn"); }
}

async function cloneSequence(project, seq, name) {
  const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
  transact(project, "PoruwaCut clone", () => [seq.createCloneAction()]);
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
  const out = [];
  for (const it of track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false)) {
    out.push({ it, a: (await it.getStartTime()).seconds, b: (await it.getEndTime()).seconds });
  }
  return out.sort((x, y) => x.a - y.a);
}

// ---------- cutting a track at time t ----------
// Premiere 27.1+ has a razor action. 26.x does not, so there we split by: trim the clip, clone it
// into the gap that the trim left, and trim the clone to fill the gap exactly.
function hasNativeRazor(seq) {
  try { return typeof ppro.SequenceEditor.getEditor(seq).createRazorTrackAction === "function"; }
  catch (e) { return false; }
}

async function emulatedRazor(project, seq, vIndex, t) {
  const editor = ppro.SequenceEditor.getEditor(seq);
  const track = await seq.getVideoTrack(vIndex);
  const hit = (await itemsOf(track)).find((x) => x.a < t - EPS && t + EPS < x.b);
  if (!hit) return false;
  const { it, a, b } = hit;
  const inp = (await it.getInPoint()).seconds;
  const left = t - a, right = b - t;
  if (left >= right) {
    // original keeps the right part [t, b]; the clone fills [a, t]
    transact(project, "PoruwaCut cut", () => [it.createSetStartAction(tt(t))]);
    if (Math.abs((await it.getInPoint()).seconds - (inp + left)) > 0.002)
      transact(project, "PoruwaCut cut", () => [it.createSetInPointAction(tt(inp + left))]);
    transact(project, "PoruwaCut cut", () => [editor.createCloneTrackItemAction(it, tt(-left), 0, 0, true, false)]);
    const c = (await itemsOf(track)).find((x) => Math.abs(x.a - a) < EPS && x.it !== it);
    if (!c) throw new Error(`clone not found at ${a.toFixed(2)}s`);
    transact(project, "PoruwaCut cut", () => [c.it.createSetEndAction(tt(t))]);
    transact(project, "PoruwaCut cut", () => [c.it.createSetInPointAction(tt(inp)), c.it.createSetOutPointAction(tt(inp + left))]);
  } else {
    // original keeps the left part [a, t]; the clone fills [t, b]
    transact(project, "PoruwaCut cut", () => [it.createSetEndAction(tt(t))]);
    transact(project, "PoruwaCut cut", () => [editor.createCloneTrackItemAction(it, tt(left), 0, 0, true, false)]);
    const c = (await itemsOf(track)).find((x) => Math.abs(x.a - t) < EPS && x.it !== it);
    if (!c) throw new Error(`clone not found at ${t.toFixed(2)}s`);
    transact(project, "PoruwaCut cut", () => [c.it.createSetEndAction(tt(b))]);
    transact(project, "PoruwaCut cut", () => [c.it.createSetInPointAction(tt(inp + left)), c.it.createSetOutPointAction(tt(inp + (b - a)))]);
  }
  return true;
}

async function razorTracks(project, seq, tracks, t, native) {
  if (native) {
    const editor = ppro.SequenceEditor.getEditor(seq);
    const V = ppro.Constants.MediaType.VIDEO;
    transact(project, "PoruwaCut cut", () => tracks.map((ti) => editor.createRazorTrackAction(V, ti, tt(t), false, V)));
    return;
  }
  for (const ti of tracks) await emulatedRazor(project, seq, ti, t);
}

// Live applier for "Manual cutting": cut every camera track and enable only the chosen camera
function makeStackedApplier(project, target, trackOfCam, rangeT0, rangeT1) {
  const native = hasNativeRazor(target);
  log(native ? "Razor: Premiere razor API" : "Razor: clone + trim (Premiere 26 / 27.0)", "mute");
  const done = new Set();
  let enabledUntil = rangeT0;
  async function cutAt(times) {
    let n = 0;
    for (const t of times) {
      const key = t.toFixed(3);
      if (done.has(key) || t <= 0.001) continue;
      await razorTracks(project, target, trackOfCam, t, native);
      done.add(key); n++;
    }
    return n;
  }
  async function enablePieces(segments, until) {
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
    if (acts.length) transact(project, "PoruwaCut angles", () => acts);
    enabledUntil = processed;
  }
  return {
    async apply(segments, until, final) {
      const cuts = [rangeT0];
      for (let i = 1; i < segments.length; i++) cuts.push(segments[i].start);
      if (final) cuts.push(rangeT1);
      await cutAt(cuts);
      await enablePieces(segments, until);
    },
    count: () => Math.max(0, done.size - 1),
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
    d.title = `${fmt(s.start - t0)} – ${fmt(s.end - t0)} · Cam ${s.cam + 1}`;
    strip.appendChild(d);
  }
  const rest = document.createElement("div");
  rest.style.flex = "1"; rest.style.height = "16px"; rest.style.backgroundColor = "#1b1b1b";
  strip.appendChild(rest);
  $("striplbl").textContent = `Cuts ${Math.max(0, segments.length - 1)} · ${fmt((until || t0) - t0)} / ${fmt(total)}`;
}
function renderLegend(cams) {
  $("legend").innerHTML = "";
  cams.forEach((c, i) => {
    const sw = document.createElement("span");
    sw.textContent = "■ Cam " + (i + 1);
    sw.style.color = CAM_COLORS[i % CAM_COLORS.length];
    sw.style.marginRight = "10px";
    $("legend").appendChild(sw);
  });
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
  if (!(await ping())) {
    log("Engine එකට සම්බන්ධ වෙන්න බැහැ. Start menu › PoruwaCut › PoruwaCut Engine run කරලා ආයෙ උත්සාහ කරන්න.", "bad");
    log("විස්තර: " + lastPingErr, "mute");
    return;
  }
  const project = await ppro.Project.getActiveProject();
  const kind = $("etype").value;
  const range = $("range").value === "inout" ? [sd.inT, sd.outT] : [0, sd.endT];
  const event = $("event").value || "auto";
  const opt = $("event").selectedOptions && $("event").selectedOptions[0];
  const evName = opt ? opt.textContent.split("  ·  ")[0] : event;
  showProgress("prepare", 0, null);
  renderLegend(sd.cams);
  renderStrip([], range[0], range[1], range[0]);

  // 1) target sequence
  let target = null, nest = null, applier = null;
  if (kind === "stacked") {
    target = await cloneSequence(project, sd.seq, await uniqueName(project, `${sd.name} - PoruwaCut ${evName}`));
    await project.openSequence(target);
    applier = makeStackedApplier(project, target, sd.cams.map((c) => c.track), range[0], range[1]);
    log(`"${target.name}" හැදුවා. Cuts live වැටෙනවා…`, "ok");
  } else {
    nest = await cloneSequence(project, sd.seq, await uniqueName(project, `${sd.name} - Multicam Source`));
    const nestItem = ppro.ClipProjectItem.cast(await nest.getProjectItem());
    const eName = await uniqueName(project, `${sd.name} - PoruwaCut Multicam ${evName}`);
    const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
    target = await project.createSequenceFromMedia(eName, [nestItem]);
    if (!target) target = await findNewSequence(project, before);
    if (!target) throw new Error("multicam edit sequence හදන්න බැරි වුණා");
    log(`Nest: "${nest.name}" · Edit: "${target.name}". Cuts පහළ strip එකේ live පේනවා, ඉවර වුණාම timeline එකට.`, "ok");
  }

  // 2) engine job
  const body = {
    project: { name: sd.name, fps: sd.fps, master_cam: 0, range,
               cameras: sd.cams.map((c) => ({ name: c.name, clips: c.clips })), audio_tracks: sd.audio },
    event,
    options: { min_shot: numOrNull("min_shot"), max_shot: numOrNull("max_shot"), layout: "stacked" },
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
        catch (e) { log("Timeline cut: " + (e && e.message || e), "bad"); applier = null; log("Live cuts නවත්වනවා. ඉවර වුණාම XML එකක් import කරනවා.", "warn"); }
      }
    }
    if (j.status === "error") return;
    if (j.status === "done") { await finish(project, kind, target, nest, sd, applier, j.result); return; }
  }
}

async function addDeadMarkers(project, seq, result) {
  if (!result.dead || !result.dead.length) return;
  try {
    const markers = await ppro.Markers.getMarkers(seq);
    transact(project, "PoruwaCut markers", () => result.dead.map((d) =>
      markers.createAddMarkerAction("CHECK – කැමරා සියල්ල දුර්වලයි", "Comment", tt(d.start), tt(Math.max(0.1, d.end - d.start)), "PoruwaCut")));
  } catch (e) { log("Markers: " + e, "warn"); }
}

async function finish(project, kind, target, nest, sd, applier, result) {
  showProgress("finish", 0.99, null);
  renderStrip(result.segments, result.range[0], result.range[1], result.range[1]);
  const s = result.stats;
  if (kind === "stacked") {
    if (applier) {
      await applier.apply(result.segments, result.range[1], true);
      await addDeadMarkers(project, target, result);
    } else if (result.xml) {
      await project.importFiles([result.xml], true, null, false);
      log("XML sequence එක import කළා: " + result.xml, "ok");
    }
    showProgress("done", 1, null);
    log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s`, "ok");
    log("Angle එකක් මාරු කරන්න: කෑල්ල select කරලා Shift+E (enable/disable).", "ok");
    return;
  }
  // multicam: save, the engine cuts the nest inside a project copy, open that copy
  await addDeadMarkers(project, target, result);
  log("Multicam cuts project එකට ලියනවා…");
  const saved = await project.save();
  if (!saved) log("Project save වුණේ නැද්ද කියලා සැකයි – දිගටම යනවා", "warn");
  const r = await call("POST", "/api/prproj/multicam", {
    project_path: project.path, edit_seq: target.name, nest_seq: nest.name,
    segments: result.segments, cam_tracks: sd.cams.map((c) => c.track),
  }, 120000);
  if (r.error) { log("Multicam cuts අසාර්ථකයි: " + r.error, "bad"); return; }
  log(`Multicam pieces ${r.report.multicam} හැදුවා → ${r.path}`, "ok");
  const opened = await ppro.Project.open(r.path);
  try {
    const seqs = await opened.getSequences();
    const e = seqs.find((x) => x.name === target.name);
    if (e) await opened.openSequence(e);
  } catch (e) {}
  showProgress("done", 1, null);
  log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s · angles මාරු කරන්න Program Monitor › Multi-Camera (Shift+0).`, "ok");
  log("ඔබේ original project එකට nest + edit sequence විතරක් එකතු වුණා. Multicam edit එක තියෙන්නේ අලුතින් open වුණු PoruwaCut copy එකේ.", "mute");
}

// ---------- razor test (Manual cutting on Premiere 26 / 27.0) ----------
async function razorTest() {
  if (busy) return;
  busy = true;
  try {
    const project = await ppro.Project.getActiveProject();
    const seq = project && (await project.getActiveSequence());
    if (!seq) { log("Test එකට sequence එකක් open කරන්න.", "bad"); return; }
    const test = await cloneSequence(project, seq, await uniqueName(project, `${seq.name} - PoruwaCut Razor Test`));
    await project.openSequence(test);
    const native = hasNativeRazor(test);
    const v1 = await test.getVideoTrack(0);
    const before = await itemsOf(v1);
    if (!before.length) { log("V1 එකේ clip එකක් නැහැ.", "bad"); return; }
    const aCount = async () => { let n = 0; const c = await test.getAudioTrackCount(); for (let i = 0; i < c; i++) n += (await itemsOf(await test.getAudioTrack(i))).length; return n; };
    const audioBefore = await aCount();
    const { a, b } = before[0];
    const inp0 = (await before[0].it.getInPoint()).seconds;
    let fps = 25;
    try { const tb = parseFloat(await test.getTimebase()); if (tb > 0) fps = TICKS / tb; } catch (e) {}
    const pts = [a + (b - a) * 0.25, a + (b - a) * 0.5, a + (b - a) * 0.8].map((t) => Math.round(t * fps) / fps);
    log(`Razor test (${native ? "Premiere razor" : "clone + trim"}): V1 clip ${fmt(a)}–${fmt(b)} · cuts ${pts.map((t) => fmt(t)).join(", ")}`);
    for (const t of pts) await razorTracks(project, test, [0], t, native);
    const after = await itemsOf(v1);
    let ok = after.length === before.length + 3;
    for (const p of after.slice(0, 4)) {
      const ip = (await p.it.getInPoint()).seconds;
      const expect = inp0 + (p.a - a);
      const good = Math.abs(ip - expect) < 0.05;
      ok = ok && good;
      log(`  ${fmt(p.a)}–${fmt(p.b)} · source in ${ip.toFixed(2)}s ${good ? "✔" : "✘ (expected " + expect.toFixed(2) + ")"}`, good ? "ok" : "bad");
    }
    const audioAfter = await aCount();
    if (audioAfter !== audioBefore) { ok = false; log(`  Audio clips ${audioBefore} → ${audioAfter}: audio වලටත් බලපෑවා ✘`, "bad"); }
    else log(`  Audio clips වෙනස් වුණේ නැහැ ✔`, "ok");
    log(ok ? "✔ Razor test සාර්ථකයි. Manual cutting live වැඩ කරයි." : "✘ Razor test අසාර්ථකයි. මේ log එකේ screenshot එකක් එවන්න.", ok ? "ok" : "bad");
    log(`Test sequence: "${test.name}" (බලලා delete කරන්න).`, "mute");
  } catch (e) { log("Razor test error: " + (e && e.message || e), "bad"); }
  finally { busy = false; }
}

// ---------- wiring ----------
const HINTS = {
  multicam: "කැමරා nest කරලා multicam angle cuts. Cuts panel strip එකේ live පේනවා, ඉවර වුණාම project copy එකක් open වෙනවා.",
  stacked: "කැමරා track වෙන වෙනම. Cuts timeline එකේ live වැටෙනවා, angle මාරු කරන්න Shift+E.",
};
$("etype").addEventListener("change", () => { $("etypehint").textContent = HINTS[$("etype").value]; });
$("etypehint").textContent = HINTS.multicam;
$("updbtn").addEventListener("click", () => installUpdate());
$("chk").addEventListener("click", () => checkUpdate(true));
$("read").addEventListener("click", () => readSequence().catch((e) => log("Error: " + e, "bad")));
$("rtest").addEventListener("click", () => razorTest());
$("go").addEventListener("click", () => autoEdit());
$("ver").textContent = "PoruwaCut v" + PANEL_VERSION;
ping().then((ok) => { if (ok) checkUpdate(false); });
setInterval(ping, 15000);
