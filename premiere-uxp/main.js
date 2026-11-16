/* PoruwaCut – Premiere Pro UXP panel.
 * Reads the active (synced) multicam sequence, sends it to the local PoruwaCut engine,
 * then imports the resulting edit (FCP XML) back into the project and opens it.
 */
const ppro = require("premierepro");
const ENGINE = "http://127.0.0.1:7788";
const PANEL_VERSION = "__PORUWACUT_VERSION__"; // stamped by build/make_ccx.py
const $ = (id) => document.getElementById(id);

let seqData = null; // what we read from Premiere

function log(msg, cls) {
  const el = $("log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

async function ping() {
  try {
    const p = await (await fetch(ENGINE + "/api/ping")).json();
    $("eng").textContent = `● v${p.version} · GPU ${p.nvdec ? "ON" : "OFF"} · faces ${p.face_detector}`;
    $("eng").className = "ok";
    if (!updateChecked) { updateChecked = true; checkUpdate(); }
    return true;
  } catch (e) {
    $("eng").textContent = "● engine offline (start_engine.bat run කරන්න)";
    $("eng").className = "bad";
    return false;
  }
}

// ---------- updates ----------
let updateChecked = false;

async function checkUpdate() {
  try {
    const u = await (await fetch(ENGINE + "/api/update")).json();
    if (u.available) {
      $("upd").style.display = "block";
      $("updtxt").textContent = `අලුත් version එකක්: v${u.latest} (දැන් v${u.current})`;
      $("updnotes").textContent = (u.notes || "").split("\n").slice(0, 4).join("\n");
    }
  } catch (e) {}
}

async function installUpdate() {
  $("updbtn").disabled = true;
  try {
    const r = await (await fetch(ENGINE + "/api/update/install", { method: "POST" })).json();
    if (r.status === "error") { log(r.message, "bad"); $("updbtn").disabled = false; return; }
  } catch (e) { log("Update පටන් ගන්න බැරි වුණා: " + e, "bad"); $("updbtn").disabled = false; return; }
  const poll = async () => {
    let s;
    try { s = await (await fetch(ENGINE + "/api/update/status")).json(); }
    catch (e) {
      // engine exited to let the installer run
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

async function readItem(item) {
  const start = (await item.getStartTime()).seconds;
  const end = (await item.getEndTime()).seconds;
  const srcIn = (await item.getInPoint()).seconds;
  let speed = 1;
  try { speed = await item.getSpeed(); } catch (e) {}
  const pi = await item.getProjectItem();
  const clip = ppro.ClipProjectItem.cast(pi);
  let path = "";
  let why = "";
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
  if (!seq) { log("Active sequence එකක් නැහැ.", "bad"); return; }

  // frame rate: ticks per frame -> fps
  let fps = 25;
  try {
    const tb = parseFloat(await seq.getTimebase());
    if (tb > 0) fps = 254016000000 / tb;
  } catch (e) {}
  const endT = (await seq.getEndTime()).seconds;
  let inT = 0, outT = endT;
  try {
    inT = (await seq.getInPoint()).seconds;
    outT = (await seq.getOutPoint()).seconds;
    if (!(outT > inT)) { inT = 0; outT = endT; }
  } catch (e) {}

  const cams = [];
  const warnings = [];
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

  seqData = { name: seq.name, fps, inT, outT, endT, cams, audio };
  warnings.forEach((w) => log(w, "bad"));
  if (warnings.some((w) => w.includes("multicam"))) {
    log("Tip: Project panel එකේ multicam source sequence එක right-click › Open in Timeline කරලා ඒකේ run කරන්න.", "bad");
  }
  $("seqinfo").textContent = `${seq.name} · ${fps.toFixed(2)} fps · In ${fmt(inT)} → Out ${fmt(outT)} · audio tracks ${audio.length}`;

  if (cams.length < 2) { log("කැමරා tracks 2ක් වත් හමු නොවුණි (එක් කැමරාවකට එක video track එකක් ඕන).", "bad"); return; }
  const sel = document.createElement("select");
  sel.id = "master";
  cams.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "trk";
    row.innerHTML = `<span class="nm">Cam ${i + 1} · ${c.name}</span><span class="c">${c.clips.length} clips</span>`;
    $("tracks").appendChild(row);
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = `Cam ${i + 1} · ${c.name}`;
    sel.appendChild(o);
  });
  const r = document.createElement("div");
  r.className = "row";
  r.innerHTML = `<label>DJI audio කැමරාව</label>`;
  r.appendChild(sel);
  $("tracks").appendChild(r);
  log(`කැමරා ${cams.length}ක් කියෙව්වා ✔`, "ok");
}

const fmt = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = Math.floor(s % 60);
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0");
};
const numOrNull = (id) => { const v = $(id).value; return v === "" ? null : parseFloat(v); };

async function autoEdit() {
  if (!seqData) await readSequence();
  if (!seqData || seqData.cams.length < 2) return;
  if (!(await ping())) { log("Engine එක run වෙන්නේ නැහැ. start_engine.bat double-click කරන්න.", "bad"); return; }

  const master = parseInt(($("master") && $("master").value) || "0", 10);
  const useInOut = $("range").value === "inout";
  const range = useInOut ? [seqData.inT, seqData.outT] : [0, seqData.endT];
  const project = {
    name: seqData.name,
    fps: seqData.fps,
    master_cam: master,
    range,
    cameras: seqData.cams.map((c) => ({ name: c.name, clips: c.clips })),
    audio_tracks: seqData.audio,
  };
  const body = {
    project,
    mode: $("mode").value,
    options: { min_shot: numOrNull("min_shot"), max_shot: numOrNull("max_shot"),
               layout: $("layout").value, remove_dead: !!$("dead").value },
  };
  $("go").disabled = true;
  log(`Auto edit පටන් ගන්නවා: ${body.mode}, ${fmt(range[0])} → ${fmt(range[1])}`);
  let job;
  try {
    job = (await (await fetch(ENGINE + "/api/edit", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json()).job;
  } catch (e) { log("Engine එකට යවන්න බැරි වුණා: " + e, "bad"); $("go").disabled = false; return; }

  let shown = 0;
  const poll = async () => {
    let j;
    try { j = await (await fetch(ENGINE + "/api/job/" + job)).json(); }
    catch (e) { setTimeout(poll, 1500); return; }
    $("bar").firstElementChild.style.width = Math.round(j.progress * 100) + "%";
    for (; shown < j.log.length; shown++) log(j.log[shown], j.log[shown].startsWith("ERROR") ? "bad" : "");
    if (j.status === "done") { await importResult(j.result); $("go").disabled = false; }
    else if (j.status === "error") { $("go").disabled = false; }
    else setTimeout(poll, 1000);
  };
  poll();
}

async function importResult(result) {
  const project = await ppro.Project.getActiveProject();
  const before = new Set((await project.getSequences()).map((s) => s.guid.toString()));
  const ok = await project.importFiles([result.xml], true, null, false);
  if (!ok) { log("XML import අසාර්ථකයි. File › Import එකෙන් අතින් import කරන්න: " + result.xml, "bad"); return; }
  // importFiles may resolve before the sequence exists – wait for it
  for (let i = 0; i < 40; i++) {
    const seqs = await project.getSequences();
    const added = seqs.find((s) => !before.has(s.guid.toString()));
    if (added) {
      await project.openSequence(added);
      const s = result.stats;
      log(`✔ "${added.name}" හැදුවා · cuts ${s.cuts} · සාමාන්‍ය shot ${s.avg_shot}s`, "ok");
      log("Angle එකක් මාරු කරන්න: ඒ කෑල්ල select කරලා Shift+E (Stacked layout).", "ok");
      return;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  log("Import වුණා, ඒත් අලුත් sequence එක හොයාගන්න බැරි වුණා – Project panel එක බලන්න.", "bad");
}

$("updbtn").addEventListener("click", () => installUpdate());
$("read").addEventListener("click", () => readSequence().catch((e) => log("Error: " + e, "bad")));
$("go").addEventListener("click", () => autoEdit().catch((e) => { log("Error: " + e, "bad"); $("go").disabled = false; }));
ping();
setInterval(ping, 15000);
