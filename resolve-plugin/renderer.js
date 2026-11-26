// Endora Highlights panel: Resolve data comes through window.resolveApi (main process),
// the analysis runs in the AutoWed engine (http://127.0.0.1:7788).
const ENGINE = "http://127.0.0.1:7788";
const $ = (id) => document.getElementById(id);
let SNAP = null, JOB = null, RES = null, FILTER = "all";
const EXCL = new Set();
const TAG_NAMES = { all: "ඔක්කොම", smile: "සිනහ", hug: "බදාගැනීම", couple: "Couple", slowmo: "Slow-mo", detail: "Details",
  drone: "Drone", glide: "Smooth move", wide: "Wide", close: "Close", applause: "අත්පොළසන්", chant: "ගාථා", drums: "බෙර", speech: "කතා" };
const SEC_COL = { low: "#3a5a9a", mid: "#2e8a8a", high: "#c47a2c", peak: "#c43c3c" };

async function api(method, path, body) {
  const r = await fetch(ENGINE + path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function log(msg) { const l = $("log"); l.textContent += msg + "\n"; l.scrollTop = l.scrollHeight; }
function fmt(t) { t = Math.max(0, t); const m = Math.floor(t / 60), s = Math.floor(t % 60); return `${m}:${String(s).padStart(2, "0")}`; }

async function status() {
  const st = $("st");
  try {
    const p = await api("GET", "/api/ping");
    let lic = "";
    try { const l = await api("GET", "/api/license"); lic = l.ok ? "" : " · licence නැහැ"; } catch (e) {}
    st.textContent = `Engine v${p.version}${p.nvdec ? " · GPU" : ""}${lic}`;
    st.className = lic ? "bad" : "ok";
  } catch (e) {
    st.textContent = "AutoWed Engine එක run වෙන්නේ නැහැ";
    st.className = "bad";
  }
}

$("read").onclick = readProject;

async function readProject() {
  $("snapInfo").textContent = "කියවනවා…";
  const r = await window.resolveApi.snapshot();
  if (!r.ok) { $("snapInfo").textContent = "✖ " + r.error; return false; }
  SNAP = r.data;
  const mins = SNAP.music.reduce((a, m) => a + m.dur, 0) / 60;
  $("snapInfo").textContent = `${SNAP.project}: video clips ${SNAP.clips.length}` +
    (SNAP.timeline ? ` · timeline "${SNAP.timeline.name}" ${SNAP.timeline.fps}fps · සින්දු ${SNAP.music.length} (${mins.toFixed(1)} min)` : " · timeline එකක් open නැහැ");
  $("go").disabled = !SNAP.clips.length;
  return true;
}

let WAITER = null;   // resolves when the running job ends (auto mode)
function runJob(path, body) {
  return new Promise(async (res) => {
    try { const r = await api("POST", path, body); JOB = r.job; WAITER = res; poll(); }
    catch (e) { log("✖ " + e.message); res(null); }
  });
}

$("go").onclick = async () => {
  if (!SNAP) return;
  $("go").disabled = true; $("stop").disabled = false; $("log").textContent = "";
  await runJob("/api/hl/analyze", { clips: SNAP.clips, music: SNAP.music, fps: SNAP.fps, thumbs: 150 });
};

async function buildHighlight() {
  if (!RES) { log("මුලින් analyze කරන්න."); return false; }
  if (!SNAP || !SNAP.music.length) { log("✖ Highlight timeline එකේ සින්දුවක් නැහැ."); return false; }
  $("build").disabled = true; $("buildInfo").textContent = "Plan එක හදනවා…";
  const { thumbs, ...analysis } = RES;
  const j = await runJob("/api/hl/plan", { analysis, music: SNAP.music, exclude_groups: [...EXCL], speech: $("optSpeech").checked });
  if (!j || j.status !== "done") { $("build").disabled = false; $("buildInfo").textContent = "✖ plan එක හදන්න බැරි වුණා"; return false; }
  $("buildInfo").textContent = "Resolve එකේ timeline එක හදනවා…";
  const r = await window.resolveApi.build(j.result, { slowmo: $("optSlow").checked });
  $("build").disabled = false;
  if (!r.ok) { $("buildInfo").textContent = "✖ " + r.error; log("✖ " + r.error); return false; }
  $("buildInfo").textContent = `✔ "${r.data.timeline}" · shots ${r.data.placed}/${r.data.shots} · slow-mo ${r.data.slowmo}`;
  for (const l of r.data.log) log("⚠ " + l);
  log(`✔ Highlight timeline එක හැදුවා: ${r.data.timeline}. බලලා කැමති විදිහට හදන්න.`);
  return true;
}
$("build").onclick = buildHighlight;

$("auto").onclick = async () => {
  $("auto").disabled = true; $("log").textContent = "";
  try {
    if (!(await readProject())) return;
    if (!SNAP.music.length) { log("✖ Highlight timeline එක open කරලා සින්දුව audio track එකට දාන්න."); return; }
    $("go").disabled = true; $("stop").disabled = false;
    const j = await runJob("/api/hl/analyze", { clips: SNAP.clips, music: SNAP.music, fps: SNAP.fps, thumbs: 150 });
    if (!j || j.status !== "done") return;
    await buildHighlight();
  } finally { $("auto").disabled = false; }
};

$("learn").onclick = async () => {
  $("learnInfo").textContent = "කියවනවා…";
  const r = await window.resolveApi.learnSnapshot();
  if (!r.ok) { $("learnInfo").textContent = "✖ " + r.error; return; }
  try {
    const j = await api("POST", "/api/hl/learn", { items: r.data.items, music: r.data.music });
    const st = j.style;
    $("learnInfo").textContent = `✔ "${r.data.name}" · low ${st.beats.low} / mid ${st.beats.mid} / high ${st.beats.high} / peak ${st.beats.peak} beats · slow-mo ${Math.round(st.slowmo * 100)}% · highlights ${st.n}`;
  } catch (e) { $("learnInfo").textContent = "✖ " + e.message; }
};
$("stop").onclick = async () => { if (JOB) try { await api("POST", `/api/job/${JOB}/cancel`); } catch (e) {} };

let seen = 0;
async function poll() {
  if (!JOB) return;
  let j;
  try { j = await api("GET", "/api/job/" + JOB); } catch (e) { setTimeout(poll, 2000); return; }
  for (const l of j.log.slice(seen)) log(l);
  seen = j.log.length;
  $("bar").firstElementChild.style.width = Math.round(100 * (j.progress || 0)) + "%";
  const eta = j.eta ? ` · ඉතුරු ~${Math.ceil(j.eta / 60)} min` : "";
  $("stage").textContent = `${j.stage || ""} ${Math.round(100 * (j.progress || 0))}%${eta}`;
  if (j.status === "running" || j.status === "queued") { setTimeout(poll, 1500); return; }
  $("go").disabled = false; $("stop").disabled = true; JOB = null; seen = 0;
  if (j.status === "done" && j.kind === "highlights") { RES = j.result; show(); }
  else if (j.status !== "done") log("✖ " + j.status);
  const w = WAITER; WAITER = null; if (w) w(j);
}

function show() {
  // song
  const s = RES.song;
  if (s && s.sections && s.sections.length) {
    $("songCard").style.display = "";
    $("songInfo").textContent = `~${Math.round(s.bpm)} BPM · ${fmt(s.end)} · කොටස් ${s.sections.length} · hits ${s.hits.length} · කතාවට ඉඩ තියෙන bars ${s.quiet.length}`;
    const box = $("song"); box.innerHTML = "";
    for (const x of s.sections) {
      const d = document.createElement("div");
      d.style.flex = String(x.end - x.start); d.style.background = SEC_COL[x.energy] || "#555";
      d.textContent = x.energy; d.title = `${fmt(x.start)} – ${fmt(x.end)} · ${x.energy}`;
      box.appendChild(d);
    }
  } else $("songCard").style.display = "none";
  // groups
  $("grpCard").style.display = "";
  const g = $("groups"); g.innerHTML = "";
  for (const gr of RES.groups) {
    if (!EXCL.size && /reception|after ?party|මංගල සභා/i.test(gr.label)) EXCL.add(gr.id);
    const row = document.createElement("label"); row.className = "grp";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !EXCL.has(gr.id);
    cb.onchange = () => { if (cb.checked) EXCL.delete(gr.id); else EXCL.add(gr.id); list(); };
    const t = document.createElement("span"); t.textContent = gr.label;
    const k = document.createElement("span"); k.className = "k"; k.textContent = `${gr.kind_name} · ${gr.minutes} min · clips ${gr.clips.length}`;
    row.append(cb, t, k); g.appendChild(row);
  }
  // chips
  const counts = {};
  for (const m of RES.moments) for (const t of m.tags) counts[t] = (counts[t] || 0) + 1;
  const c = $("chips"); c.innerHTML = "";
  for (const key of ["all", ...Object.keys(TAG_NAMES).filter((k) => counts[k])]) {
    const d = document.createElement("div");
    d.className = "chip" + (FILTER === key ? " on" : "");
    d.textContent = `${TAG_NAMES[key] || key}${key === "all" ? "" : " " + counts[key]}`;
    d.onclick = () => { FILTER = key; show(); };
    c.appendChild(d);
  }
  $("resCard").style.display = "";
  $("buildCard").style.display = "";
  list();
}

function picked() {
  return RES.moments.filter((m) => !EXCL.has(m.group) && (FILTER === "all" || m.tags.includes(FILTER)));
}
function list() {
  const box = $("list"); box.innerHTML = "";
  for (const m of picked().slice(0, 300)) {
    const d = document.createElement("div"); d.className = "m";
    if (RES.thumbs[m.id]) { const im = document.createElement("img"); im.src = RES.thumbs[m.id]; d.appendChild(im); }
    const t = document.createElement("div"); t.className = "t";
    t.innerHTML = `<span class="sc">${m.score.toFixed(2)}</span><div class="n"></div><div class="mute"></div><div class="tg"></div>`;
    t.querySelector(".n").textContent = m.name;
    t.querySelector(".mute").textContent = `${fmt(m.start)} – ${fmt(m.end)}`;
    for (const tag of m.tags) { const s = document.createElement("span"); s.className = "tag"; s.textContent = TAG_NAMES[tag] || tag; t.querySelector(".tg").appendChild(s); }
    d.appendChild(t); box.appendChild(d);
  }
}

$("mark").onclick = async () => {
  const n = Math.max(1, parseInt($("topN").value, 10) || 60);
  const ms = picked().slice(0, n);
  $("mark").disabled = true;
  const r = await window.resolveApi.markMoments(ms);
  $("mark").disabled = false;
  log(r.ok ? `✔ Markers ${r.data.added}ක් clips වලට දැම්මා${r.data.failed ? ` (${r.data.failed}ක් බැරි වුණා)` : ""}. Media pool එකේ clip එකක් open කළාම පේනවා.` : "✖ " + r.error);
};
$("clear").onclick = async () => {
  const r = await window.resolveApi.clearMarkers();
  log(r.ok ? `Endora markers ${r.data.cleared}ක් අයින් කළා.` : "✖ " + r.error);
};
$("markSong").onclick = async () => {
  const r = await window.resolveApi.markSong(RES.song);
  log(r.ok ? `✔ සින්දුවේ markers ${r.data.added}ක් timeline එකට දැම්මා.` : "✖ " + r.error);
};

status(); setInterval(status, 15000);
