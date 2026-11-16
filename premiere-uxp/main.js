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
  try {
    const r = await call("POST", "/api/update/install", {});
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
    if (s.status === "downloading") $("updtxt").textContent = `Download වෙනවා... ${Math.round(s.progress * 100)}%`;
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

// Live applier: razors new cut times on the target tracks, and (stacked) enables the chosen camera
function makeApplier(project, target, kind, trackOfCam, rangeT0, rangeT1) {
  const editor = ppro.SequenceEditor.getEditor(target);
  const V = ppro.Constants.MediaType.VIDEO;
  const razorTracks = kind === "multicam" ? [0] : trackOfCam.slice();
  const done = new Set();
  let enabledUntil = rangeT0;

  function razorAt(times) {
    const todo = times.filter((t) => !done.has(t.toFixed(3)));
    if (!todo.length) return 0;
    transact(project, "PoruwaCut cuts", () => {
      const acts = [];
      for (const t of todo) for (const ti of razorTracks) acts.push(editor.createRazorTrackAction(V, ti, tt(t), false, V));
      return acts;
    });
    todo.forEach((t) => done.add(t.toFixed(3)));
    return todo.length;
  }

  async function enablePieces(segments, until) {
    if (kind !== "stacked") return;
    const camAt = (t) => { for (const s of segments) if (t >= s.start - 1e-3 && t < s.end + 1e-3) return s.cam; return null; };
    const acts = [];
    let processed = enabledUntil;
    for (let c = 0; c < trackOfCam.length; c++) {
      const track = await target.getVideoTrack(trackOfCam[c]);
      const items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
      for (const it of items) {
        const a = (await it.getStartTime()).seconds, b = (await it.getEndTime()).seconds;
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
    enabledUntil = processed; // only pieces that were fully inside the committed range
  }

  return {
    async apply(segments, until, final) {
      const cuts = [];
      if (!done.size) cuts.push(rangeT0);
      for (let i = 1; i < segments.length; i++) cuts.push(segments[i].start);
      if (final) cuts.push(rangeT1);
      const n = razorAt(cuts.filter((t) => t > 0.001));
      await enablePieces(segments, until);
      return n;
    },
    count: () => Math.max(0, done.size - 1),
  };
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
  const evName = $("event").selectedOptions && $("event").selectedOptions[0] ? $("event").selectedOptions[0].textContent.split("  ·  ")[0] : event;
  showProgress("prepare", 0, null);

  // 1) target sequence
  let target, nest = null;
  if (kind === "stacked") {
    target = await cloneSequence(project, sd.seq, await uniqueName(project, `${sd.name} - PoruwaCut ${evName}`));
    await project.openSequence(target);
    log(`"${target.name}" හැදුවා. Cuts live වැටෙනවා…`, "ok");
  } else {
    nest = await cloneSequence(project, sd.seq, await uniqueName(project, `${sd.name} - Multicam Source`));
    const nestItem = ppro.ClipProjectItem.cast(await nest.getProjectItem());
    const eName = await uniqueName(project, `${sd.name} - PoruwaCut Multicam ${evName}`);
    const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
    target = await project.createSequenceFromMedia(eName, [nestItem]);
    if (!target) target = await findNewSequence(project, before);
    if (!target) throw new Error("multicam edit sequence හදන්න බැරි වුණා");
    await project.openSequence(target);
    log(`Nest: "${nest.name}" · Edit: "${target.name}". Cuts live වැටෙනවා…`, "ok");
  }
  const trackOfCam = sd.cams.map((c) => c.track);
  const applier = makeApplier(project, target, kind, trackOfCam, range[0], range[1]);

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
      await applier.apply(j.segments, j.committed_until || range[0], false);
      $("cutsline").textContent = `Timeline එකේ cuts: ${applier.count()}  ·  ${fmt((j.committed_until || range[0]) - range[0])} / ${fmt(range[1] - range[0])}`;
    }
    if (j.status === "error") return;
    if (j.status === "done") { await finish(project, kind, target, nest, sd, applier, j.result); return; }
  }
}

async function finish(project, kind, target, nest, sd, applier, result) {
  showProgress("finish", 0.99, null);
  await applier.apply(result.segments, result.range[1], true);
  // markers on stretches where every camera was weak
  try {
    const markers = await ppro.Markers.getMarkers(target);
    if (result.dead && result.dead.length) {
      transact(project, "PoruwaCut markers", () => result.dead.map((d) =>
        markers.createAddMarkerAction("CHECK – කැමරා සියල්ල දුර්වලයි", "Comment", tt(d.start), tt(Math.max(0.1, d.end - d.start)), "PoruwaCut")));
    }
  } catch (e) { log("Markers: " + e, "warn"); }
  const s = result.stats;
  if (kind === "stacked") {
    showProgress("done", 1, null);
    log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s`, "ok");
    log("Angle එකක් මාරු කරන්න: කෑල්ල select කරලා Shift+E (enable/disable).", "ok");
    return;
  }
  // multicam: save, let the engine turn every piece into a multicam clip, open the copy
  log("Multicam angles සෙට් කරනවා (project copy එකක)…");
  const saved = await project.save();
  if (!saved) log("Project save වුණේ නැද්ද කියලා සැකයි – දිගටම යනවා", "warn");
  const r = await call("POST", "/api/prproj/multicam", {
    project_path: project.path, edit_seq: target.name, nest_seq: nest.name,
    segments: result.segments, cam_tracks: sd.cams.map((c) => c.track),
  }, 60000);
  if (r.error) { log("Multicam patch අසාර්ථකයි: " + r.error, "bad"); return; }
  log(`Multicam pieces ${r.report.set}/${r.report.pieces} සෙට් කළා → ${r.path}`, "ok");
  const opened = await ppro.Project.open(r.path);
  try {
    const seqs = await opened.getSequences();
    const e = seqs.find((x) => x.name === target.name);
    if (e) await opened.openSequence(e);
  } catch (e) {}
  showProgress("done", 1, null);
  log(`✔ ඉවරයි · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s · Program Monitor එකේ Multi-Camera view (Shift+0) එකෙන් angles මාරු කරන්න පුළුවන්.`, "ok");
  log("ඔබේ original project එක වෙනස් වුණේ නැහැ (nest + edit sequence විතරක් එකතු වුණා). PoruwaCut copy එක අලුත් tab එකක open වුණා.", "mute");
}

// ---------- wiring ----------
const HINTS = {
  multicam: "කැමරා nest කරලා multicam enable කරලා angle cuts. ඉවර වුණාම project copy එකක් open වෙනවා.",
  stacked: "කැමරා track වෙන වෙනම. Cuts live වැටෙනවා, angle මාරු කරන්න Shift+E.",
};
$("etype").addEventListener("change", () => { $("etypehint").textContent = HINTS[$("etype").value]; });
$("etypehint").textContent = HINTS.multicam;
$("updbtn").addEventListener("click", () => installUpdate());
$("chk").addEventListener("click", () => checkUpdate(true));
$("read").addEventListener("click", () => readSequence().catch((e) => log("Error: " + e, "bad")));
$("go").addEventListener("click", () => autoEdit());
$("ver").textContent = "PoruwaCut v" + PANEL_VERSION;
ping().then((ok) => { if (ok) checkUpdate(false); });
setInterval(ping, 15000);
