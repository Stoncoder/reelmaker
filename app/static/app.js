"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
};
const S = { st: null, day: "", folder: "", p: null, piece: 0, kf: 0, tab: "cut", poll: null, playList: null };

function fmt(t) {
  if (t == null || isNaN(t)) return "";
  t = Math.max(0, t);
  const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const ss = s.toFixed(1).padStart(4, "0");
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function parseT(str) {
  const parts = String(str).trim().split(":").map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((a, b) => a * 60 + b, 0);
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const m = data.detail || r.statusText; toast(m, 6000); throw new Error(m); }
  return data;
}
const enc = encodeURIComponent;
const reelUrl = (x = "") => `/api/reel/${enc(S.day)}/${enc(S.folder)}${x}`;
const mediaUrl = (x) => `/media/${enc(S.day)}/${enc(S.folder)}/${x}`;

/* ------------------------------------------------------------------ home */
async function loadState() {
  S.st = await api("GET", "/api/state");
  const ff = S.st.ffmpeg || {};
  if (ff.ok === false && !$("#ffmpegBar")) {   // captions can't be burned in with this ffmpeg: say how to fix it
    const fix = S.st.platform === "windows" ? "double-click Setup.bat again (it installs the full FFmpeg build)"
      : "double-click Setup.command again (it installs ffmpeg-full)";
    document.body.prepend(h("div", { id: "ffmpegBar", class: "updBar restart" },
      h("span", {}, ff.found ? `⚠ The FFmpeg on this computer (${ff.path}) can't burn in captions, so exports will fail. To fix it, ${fix}, then restart Reelmaker.`
        : `⚠ FFmpeg wasn't found, so Reelmaker can't make videos. To fix it, ${fix}, then restart Reelmaker.`)));
  }
  const sel = $("#daySel"); sel.innerHTML = "";
  if (!S.st.days.length) sel.append(h("option", { value: "" }, "— create a day folder —"));
  for (const d of S.st.days) sel.append(h("option", { value: d }, d));
  S.day = S.day && S.st.days.includes(S.day) ? S.day : (S.st.last_day || S.st.days[S.st.days.length - 1] || "");
  sel.value = S.day;
  if (!$("#creatorIn").value) $("#creatorIn").value = S.st.creator || "";
  // only pick a saved length the menu actually offers (an unknown value would leave it blank and send "Auto")
  $("#autoExport").checked = S.st.auto_export !== false;
  const ts = $("#targetSel"), tv = String(S.st.target || 0);
  ts.value = [...ts.options].some((o) => o.value === tv) ? tv : "0";
  drawPlan();
}
async function loadReels() {
  $("#dayName").textContent = S.day || "—";
  const real = $("#reelList"), y = window.scrollY;
  if (!S.day) { real.innerHTML = ""; real.append(h("p", { class: "hint" }, "Create a day folder first (top right).")); return; }
  const reels = await api("GET", `/api/days/${enc(S.day)}/reels`);
  // build off-screen, then swap in one go, so the page never collapses and jumps to the top
  const list = document.createDocumentFragment();
  S.selReels = new Set([...(S.selReels || [])].filter((f) => reels.some((r) => r.folder === f)));
  const bar = h("div", { class: "selbar" + (S.selReels.size ? "" : " hidden") },
    h("span", {}, `${S.selReels.size} selected`),
    h("button", { class: "small danger", onclick: async () => {
      const n = S.selReels.size;
      if (!confirm(`Delete ${n} reel${n > 1 ? "s" : ""}? Their folders (drafts, final exports, footage) go to the Trash.`)) return;
      for (const f of S.selReels) await api("DELETE", `/api/reel/${enc(S.day)}/${enc(f)}`).catch(() => {});
      S.selReels.clear(); toast("Moved to the Trash."); loadReels();
    } }, "Delete selected"),
    h("button", { class: "small", onclick: async () => {
      const r = await api("POST", `/api/days/${enc(S.day)}/export-all`, { folders: [...S.selReels] }); window.usageKick && window.usageKick();
      toast(exportMsg(r), 6000); S.selReels.clear(); loadReels();
    } }, "Export selected"),
    h("button", { class: "ghost small", onclick: () => setAll(true) }, "Select all"),
    h("button", { class: "ghost small", onclick: () => setAll(false) }, "Clear"));
  function setAll(on) {
    S.selReels = new Set(on ? reels.map((r) => r.folder) : []);
    $$(".reelCard .pick").forEach((c) => { c.checked = on; c.closest(".reelCard").classList.toggle("sel", on); });
    bar.classList.toggle("hidden", !on); bar.firstChild.textContent = `${S.selReels.size} selected`;
  }
  list.append(bar);
  if (!reels.length) list.append(h("p", { class: "hint" }, "No reels yet in this folder."));
  for (const r of reels.reverse()) {
    const st = r.state === "ready" ? "" : r.state;
    list.append(h("div", { class: "reelCard" + (S.selReels.has(r.folder) ? " sel" : ""), onclick: () => (location.hash = `#/${enc(S.day)}/${enc(r.folder)}`) },
      h("input", { type: "checkbox", class: "pick", title: "Select", checked: S.selReels.has(r.folder),
        onclick: (e) => {
          e.stopPropagation();
          e.target.checked ? S.selReels.add(r.folder) : S.selReels.delete(r.folder);
          e.target.closest(".reelCard").classList.toggle("sel", e.target.checked);
          const sb = $(".selbar"); sb.classList.toggle("hidden", !S.selReels.size);
          sb.firstChild.textContent = `${S.selReels.size} selected`;
        } }),
      h("button", { class: "ghost small del", title: "Delete this reel", onclick: async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete “${r.name || r.folder}”? Its folder (drafts, final export, footage) goes to the Trash.`)) return;
        await api("DELETE", `/api/reel/${enc(S.day)}/${enc(r.folder)}`); toast("Moved to the Trash."); loadReels();
      } }, "🗑"),
      h("b", {}, r.name || r.folder),
      h("div", { class: "hint" }, r.folder),
      h("div", { class: "hint" }, (r.source || "").slice(0, 60)),
      h("div", {}, ...r.ranges.map((x) => h("span", { class: "tag" }, x))),
      h("div", { style: "margin-top:6px" },
        r.final ? h("span", { class: "tag final" }, "FINAL exported") : null,
        r.drafts ? h("span", { class: "tag" }, `${r.drafts} draft${r.drafts > 1 ? "s" : ""}`) : null,
        st ? h("span", { class: "tag" + (st === "error" ? " err" : "") }, st + (st !== "error" && r.progress != null ? ` ${Math.floor(r.progress * 100)}%` : "")) : null,
        r.rendering ? h("span", { class: "tag" }, (r.render_phase || "").startsWith("Waiting")
          ? (r.rendering === "final" ? "waiting to export" : "waiting to render")
          : `${r.rendering === "final" ? "exporting" : "rendering preview"} ${Math.floor((r.render_progress || 0) * 100)}%`) : null),
      st && st !== "error" ? h("div", { class: "bar small" }, h("i", { style: `width:${((r.progress || 0) * 100).toFixed(1)}%` })) : null));
  }
  real.replaceChildren(list);
  window.scrollTo(0, y);
  const toExport = reels.filter((r) => r.state === "ready" && !r.final && !r.rendering).length;
  const stopped = reels.filter((r) => r.state === "error").length;
  const eb = $("#exportAllBtn"), rb = $("#retryAllBtn");
  S.readyReels = reels.filter((r) => r.state === "ready").map((r) => r.folder);
  $("#rehookBtn").classList.toggle("hidden", !S.readyReels.length || !!S.rehooking);
  eb.classList.toggle("hidden", !toExport); eb.textContent = `Export all ready (${toExport})`;
  rb.classList.toggle("hidden", !stopped); rb.textContent = `Retry stopped (${stopped})`;
  // keep the cards live while anything is being made
  clearTimeout(S.homePoll);
  if (reels.some((r) => (r.state !== "ready" && r.state !== "error") || r.rendering))
    S.homePoll = setTimeout(() => { if (!S.folder && document.visibilityState === "visible") loadReels(); }, 2000);
}
function exportMsg(r) {
  const n = r.queued.length;
  const why = r.skipped.length ? ` Skipped ${r.skipped.length}: ` + r.skipped.map((x) => `${x.folder} (${x.why})`).join(", ") : "";
  return (n ? `Exporting ${n} reel${n > 1 ? "s" : ""}, one after another — watch the cards.` : "Nothing to export.") + why;
}
$("#exportAllBtn").onclick = async () => {
  const r = await api("POST", `/api/days/${enc(S.day)}/export-all`, {}); toast(exportMsg(r), 6000); loadReels(); window.usageKick && window.usageKick();
};
$("#rehookBtn").onclick = async () => {
  const list = [...(S.readyReels || [])], day = S.day;
  if (!list.length || !confirm(`Write new hooks for all ${list.length} reels in ${day}? ${S.hw && S.hw.mode === "groq" && S.hw.has_key ? "A few seconds per reel (Groq)." : "About 25 s per reel (the AI runs on your Mac)."} Reels where you typed your own hook keep it.`)) return;
  S.rehooking = true; $("#rehookBtn").classList.add("hidden");
  let applied = 0, failed = 0;
  for (let i = 0; i < list.length; i++) {
    toast(`Writing hooks ${i + 1}/${list.length}: ${list[i]}…`, 60000);
    const r = await api("POST", `/api/reel/${enc(day)}/${enc(list[i])}/hooks`, { idea: "", apply_best: true }).catch(() => null);
    if (!r) failed++; else if (r.applied) applied++;
  }
  S.rehooking = false;
  const exp = confirm(`New hooks written for ${list.length - failed} reel${list.length - failed === 1 ? "" : "s"} (the best one is now in ${applied} of them${failed ? `; ${failed} failed` : ""}).\n\nExport them all again so the MP4s get the new hooks?`);
  if (exp) { const q = await api("POST", `/api/days/${enc(day)}/export-all`, { folders: list }); toast(exportMsg(q), 6000); window.usageKick && window.usageKick(); }
  else toast("Done. Open a reel's Hook tab to pick a different one.", 5000);
  loadReels();
};
$("#retryAllBtn").onclick = async () => {
  const r = await api("POST", `/api/days/${enc(S.day)}/retry-stopped`); window.usageKick && window.usageKick();
  toast(`Restarted ${r.restarted.length} reel${r.restarted.length === 1 ? "" : "s"}. They run one at a time.`, 5000); loadReels();
};
$("#cleanBtn").onclick = async () => {
  const pick = await api("POST", "/api/pick-file"); if (!pick.path) return;
  const box = $("#cleanResult"), b = $("#cleanBtn");
  box.innerHTML = ""; box.append(h("p", { class: "hint" }, "Cleaning " + pick.path.split("/").pop() + "…"));
  b.disabled = true;
  try {
    const r = await api("POST", "/api/clean-file", { path: pick.path });
    box.innerHTML = "";
    box.append(h("div", { class: "check" }, h("span", { class: r.ok ? "ok" : "bad" }, r.ok ? "✓" : "✗"),
      h("span", {}, h("b", {}, r.ok ? "Clean — no metadata left" : "Some metadata could not be removed"), ` (checked with ${r.read_by})`)));
    if (r.removed.length) box.append(h("div", { class: "hint" }, "Removed: " + r.removed.map(([k, v]) => `${k.replace(/^.*:/, "")}${typeof v === "string" && v.length < 40 ? " (" + v + ")" : ""}`).join(", ")));
    if (!r.ok) box.append(h("div", { class: "err" }, "Still there: " + Object.keys(r.leftover).join(", ")));
    box.append(h("div", { class: "row" }, h("span", { class: "hint" }, r.output.split("/").pop()),
      h("button", { class: "ghost small", onclick: () => api("POST", "/api/reveal-path", { path: r.output }) }, "Show in Finder")));
  } catch (e) { box.innerHTML = ""; } finally { b.disabled = false; }
};
$("#daySel").onchange =async (e) => { S.day = e.target.value; await api("POST", "/api/last-day", { day: S.day }); loadReels(); drawPlan(); };
$("#newDayBtn").onclick = async () => {
  let n = 1;
  for (const d of S.st.days) { const m = d.match(/^GTA 6 Day (\d+)$/); if (m) n = Math.max(n, +m[1] + 1); }
  const name = prompt("New day folder name:", `GTA 6 Day ${n}`);
  if (!name) return;
  const r = await api("POST", "/api/days", { name });
  S.day = r.day; await loadState(); loadReels(); drawPlan();
};
$$("input[name=srcType]").forEach((r) => (r.onchange = () => {
  const yt = $("input[name=srcType]:checked").value === "youtube";
  $("#ytRow").classList.toggle("hidden", !yt); $("#fileRow").classList.toggle("hidden", yt); drawPlan();
}));
$("#pickBtn").onclick = async () => { const r = await api("POST", "/api/pick-file"); if (r.path) $("#pathIn").value = r.path; };

let moments = [];
async function parseMoments() {
  const text = $("#scenIn").value;
  moments = text.trim() ? await api("POST", "/api/parse-moments", { text }).catch(() => []) : [];
  const box = $("#momentPreview"); box.innerHTML = "";
  moments.forEach((m, i) => box.append(h("div", { class: "mrow" + (m.error ? " bad" : "") },
    h("b", {}, `Reel ${i + 1}`),
    m.error ? h("span", { class: "err" }, m.error)
      : h("span", {}, m.ranges.length ? m.ranges.map((r) => r.label).join(" + ") : "app finds the moment", m.scenario ? h("span", { class: "hint" }, " · " + m.scenario) : null))));
  drawPlan();
}
const autoMode = () => $("input[name=clipMode]:checked").value === "auto";
function hms(t) {  // "5:08:45" / "8:45" / "125" -> seconds, "" -> null, junk -> NaN
  t = (t || "").trim(); if (!t) return null;
  if (!/^\d+(:\d{1,2}){0,2}(\.\d+)?$/.test(t)) return NaN;
  return t.split(":").reduce((a, x) => a * 60 + +x, 0);
}
function searchWindow() {
  const a = hms($("#winA").value), b = hms($("#winB").value);
  if (a === null && b === null) return { win: null };
  if (a === null || b === null || isNaN(a) || isNaN(b)) return { err: "Fill both window times, like 5:00:00 and 5:30:00." };
  if (b - a < 60) return { err: "The search window must end at least 1 minute after it starts." };
  return { win: { start: a, end: b } };
}
function drawPlan() {
  const auto = autoMode();
  $("#autoRow").classList.toggle("hidden", !auto); $("#mineRow").classList.toggle("hidden", auto);
  const ms = auto ? [] : moments;
  const out = [], timed = ms.filter((m) => m.ranges.length).length, untimed = ms.filter((m) => !m.ranges.length && !m.error).length;
  if (!S.day) out.push("⚠ First create a day folder: “+ New day” at the top.");
  const sw = searchWindow();
  if (!ms.length) {
    const n = +$("#countIn").value || 1;
    if (!auto) out.push("Type at least one moment, or switch to ✨ Auto-find.");
    else out.push(`The app will find the ${n} best moment${n > 1 ? "s" : ""} by itself` + (sw.win ? ` between ${$("#winA").value.trim()} and ${$("#winB").value.trim()}.` : "."));
  } else {
    out.push(`${moments.length} reel${moments.length > 1 ? "s" : ""}: ` + [timed ? `${timed} from your timestamps` : "", untimed ? `${untimed} found from your description` : ""].filter(Boolean).join(", ") + ".");
  }
  if (auto && sw.err) out.push("⚠ " + sw.err);
  else if (auto && sw.win) out.push(`Only that ${Math.round((sw.win.end - sw.win.start) / 60)} min is scanned (about 1 min of waiting per 5 min of video).`);
  else if (auto || untimed) out.push("Finding moments scans the whole video: it's transcribed once (about 4 min for 20 min of video)" +
    ($("input[name=srcType]:checked").value === "youtube" ? " from a small 360p copy." : ".") + " Set a search window to make it faster.");
  else if (ms.length) out.push("Only your ranges are fetched — quick.");
  out.push($("#autoExport").checked ? "Each reel's final MP4 is exported automatically when it's ready — nothing to click."
    : "Automatic export is off: you'll export each reel yourself (or use “Export all” below).");
  $("#planSummary").textContent = out.join("\n");
  const bad = ms.some((m) => m.error), empty = !auto && !ms.length, werr = auto && sw.err;
  $("#createBtn").disabled = !S.day || bad || empty || !!werr;
  $("#createBtn").textContent = bad ? "Fix the highlighted line first" : werr ? "Fix the search window first" : "Make my reels";
}
let parseTimer;
$("#scenIn").oninput = () => { clearTimeout(parseTimer); parseTimer = setTimeout(parseMoments, 300); };
["#countIn", "#targetSel", "#winA", "#winB", "#autoExport"].forEach((id) => $(id).addEventListener("input", drawPlan));
$$("input[name=clipMode]").forEach((r) => (r.onchange = drawPlan));
$("#createBtn").onclick = async () => {
  if (!S.day) return toast("Create or choose a day folder first.");
  const auto = autoMode(), sw = searchWindow();
  if (auto && sw.err) return toast(sw.err);
  if (!auto) { await parseMoments(); if (moments.some((m) => m.error) || !moments.length) return; }
  const yt = $("input[name=srcType]:checked").value === "youtube";
  const source = yt ? { type: "youtube", url: $("#urlIn").value } : { type: "file", path: $("#pathIn").value };
  const body = { source, moments: auto ? [] : moments, window: auto ? sw.win : null, creator: $("#creatorIn").value, target: +$("#targetSel").value, count: +$("#countIn").value, auto_export: $("#autoExport").checked };
  $("#createBtn").disabled = true;
  try {
    const r = await api("POST", `/api/days/${enc(S.day)}/reels`, body); window.usageKick && window.usageKick();
    $("#scenIn").value = ""; moments = []; $("#momentPreview").innerHTML = "";
    location.hash = `#/${enc(S.day)}/${enc(r.created[0])}`;
    if (r.created.length > 1) setTimeout(() => toast(`${r.created.length} reels are being made — find them all on the home page.`, 6000), 800);
  } finally { $("#createBtn").disabled = false; }
};

/* ------------------------------------------------------------------ routing */
async function route() {
  const m = location.hash.match(/^#\/([^/]+)\/([^/]+)$/);
  clearTimeout(S.poll);
  await flushSave();   // an edit made just before leaving still lands in the reel it was made in
  if (!S.st) await loadState();
  if (m) {
    S.day = decodeURIComponent(m[1]); S.folder = decodeURIComponent(m[2]);
    $("#home").classList.add("hidden"); $("#editor").classList.remove("hidden");
    $("#daySel").value = S.day;
    stopAll(); window.scrollTo(0, 0);
    await openReel();
    drawReelNav();
  } else {
    S.folder = ""; S.p = null; stopAll();
    $("#editor").classList.add("hidden"); $("#home").classList.remove("hidden");
    await loadState(); loadReels();
  }
}
window.addEventListener("hashchange", route);
$("#homeLink").onclick = (e) => { e.preventDefault(); location.hash = ""; };

/* ------------------------------------------------------------------ usage bar (header) */
// While anything runs (download, transcription, preview, export) the header shows what it is with its progress,
// plus CPU / GPU / RAM and the download speed. Idle: only a cheap "anything running?" check every 3 s.
(function usageBar() {
  const bar = $("#usageBar");
  let busy = false, timer = null;
  const pctTxt = (v) => (v == null ? "…" : `${Math.round(v)}%`);
  const meter = (barId, numId, v) => {
    const i = $(barId); i.style.width = `${Math.min(100, v || 0)}%`; i.classList.toggle("hot", (v || 0) >= 85);
    $(numId).textContent = pctTxt(v);
  };
  const tick = async () => {
    clearTimeout(timer);
    if (document.visibilityState !== "visible") { timer = setTimeout(tick, 3000); return; }
    const r = await fetch(`/api/system${busy ? "" : "?lite=1"}`).then((x) => x.json()).catch(() => null);
    const act = (r && r.active) || [];
    busy = act.length > 0;
    bar.classList.toggle("hidden", !busy);
    if (busy) {
      const a = act[0];
      $("#ubLabel").textContent = `${a.label} · ${a.reel.replace(/^reel-(\d+).*/, "reel $1")}`;
      $("#ubTaskBar").style.width = `${((a.progress || 0) * 100).toFixed(1)}%`;
      $("#ubPct").textContent = a.waiting ? "" : `${Math.floor((a.progress || 0) * 100)}%`;
      $("#ubTask").href = `#/${enc(a.day)}/${enc(a.reel)}`;
      $("#ubTask").title = act.map((x) => `${x.label} — ${x.reel} ${x.waiting ? "(waiting)" : Math.floor((x.progress || 0) * 100) + "%"}`).join("\n");
      $("#ubMore").textContent = act.length > 1 ? `+${act.length - 1} more` : "";
      if (r.cpu === undefined) { $("#ubCpu").textContent = $("#ubGpu").textContent = $("#ubMem").textContent = "…"; }
      else {
        meter("#ubCpuBar", "#ubCpu", r.cpu); meter("#ubGpuBar", "#ubGpu", r.gpu);
        $("#ubMem").textContent = r.mem_used && r.mem_total ? `${(r.mem_used / 1e9).toFixed(1)}/${(r.mem_total / 1e9).toFixed(0)} GB` : "–";
        // download speed: the app's own measure while it downloads, else the Mac's network if it's busy
        const dl = act.find((x) => x.download_speed != null);
        const sp = dl ? dl.download_speed : (r.net_down > 100e3 ? r.net_down : null);
        $("#ubNetWrap").classList.toggle("hidden", sp == null);
        if (sp != null) $("#ubNet").textContent = fmtBytes(sp) + "/s";
      }
    }
    clearTimeout(timer); timer = setTimeout(tick, busy ? 1000 : 3000);   // exactly one loop: each tick schedules the next
  };
  tick();
  // coming back to the tab, or starting a render/export here: check right away instead of waiting up to 3 s
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tick(); });
  window.usageKick = () => setTimeout(tick, 300);
})();

/* ------------------------------------------------------------------ out-of-date tab */
// An open tab keeps running the page it loaded. If Reelmaker is updated meanwhile, say so (once) and offer a reload.
const API_VERSION = 5;   // must match server.py API_VERSION
function restartBar(show) {
  let b = $("#restartBar");
  if (!show) { if (b) b.remove(); return; }
  if (b) return;
  document.body.prepend(h("div", { id: "restartBar", class: "updBar restart" },
    h("span", {}, "⚠ Reelmaker's server is running an older version, so some buttons won't work. Restart it: press Ctrl+C in the Terminal window where it runs, then start ./reelmaker again (or double-click Reelmaker.command)."),
    h("button", { class: "small", onclick: () => restartReelmaker() }, "Restart now"),
    h("button", { class: "ghost small", onclick: () => location.reload() }, "I restarted it — reload")));
}
/* restart Reelmaker from the page (works when it was started by the launcher / “Start Reelmaker”) */
async function restartReelmaker() {
  await flushSave();
  const post = (force) => fetch("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force }) });
  let r = await post(false).catch(() => null);
  if (!r) return toast("Reelmaker isn't answering. Double-click “Start Reelmaker” on your Desktop.", 9000);
  if (r.status === 404) return toast("This Reelmaker is too old to restart itself: press Ctrl+C in its Terminal window, then double-click “Start Reelmaker” on your Desktop. After that, ⟳ Restart works.", 12000);
  if (r.status === 409) {
    const d = (await r.json().catch(() => ({}))).detail;
    if (d && d.busy) {
      if (!confirm(`Reelmaker is still working on: ${d.busy.join(", ")}.\n\nRestarting now stops that work (use “Retry stopped” / Export again afterwards). Restart anyway?`)) return;
      r = await post(true).catch(() => null);
      if (!r || !r.ok) return toast("Couldn't restart — try again.", 6000);
    } else return toast(typeof d === "string" ? d : "Reelmaker can't restart itself right now.", 12000);
  } else if (!r.ok) return toast("Couldn't restart — try again.", 6000);
  const ov = h("div", { id: "restartOverlay" }, h("div", {}, "↻ Restarting Reelmaker…", h("span", { class: "hint" }, "This page reloads by itself in a few seconds.")));
  document.body.append(ov);
  await waitForRestart(ov);
}
/* wait for the server to go down and come back on the new code, then reload the page */
async function waitForRestart(ov) {
  const t0 = Date.now(); let down = false;
  while (Date.now() - t0 < 60000) {
    await new Promise((res) => setTimeout(res, 500));
    const v = await fetch("/api/ui-version", { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null);
    if (!v) { down = true; continue; }
    if (down && !v.restart_needed) { location.reload(); return; }
  }
  ov.firstChild.replaceChildren("Restart is taking longer than usual.", h("span", { class: "hint" }, "Check the Terminal window where Reelmaker runs, or double-click “Start Reelmaker” on your Desktop."));
}
$("#restartBtn").onclick = () => restartReelmaker();

/* ------------------------------------------------------------------ updates from GitHub */
const UPD = { info: null };
function showUpdateInfo(u) {
  UPD.info = u;
  const btn = $("#updateBtn");
  btn.classList.toggle("hidden", !(u && u.update_available));
  if (u && u.update_available) btn.title = u.can_apply ? `Update to ${u.latest_short}` + (u.whats_new.length ? ":\n• " + u.whats_new.slice(0, 6).join("\n• ") : "") : (u.reason || "");
  const date = u && u.latest_date && u.current === u.latest ? " (" + new Date(u.latest_date).toLocaleDateString() + ")" : "";
  $("#versionText").textContent = u ? `Reelmaker ${u.current_short}${date}` + (u.update_available ? ` — update available (${u.latest_short})` : u.error ? "" : " — up to date") : "Reelmaker";
}
async function checkUpdates(force) {
  const u = await fetch("/api/update" + (force ? "?force=1" : ""), { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (u) showUpdateInfo(u);
  return u;
}
async function runUpdate() {
  let u = UPD.info;
  if (!u || !u.update_available) return;
  if (!u.can_apply) return alert(u.reason || "This copy of Reelmaker can't update itself.");
  const list = u.whats_new.length ? "\n\nWhat's new:\n• " + u.whats_new.slice(0, 12).join("\n• ") : "";
  if (!confirm(`Update Reelmaker from ${u.current_short} to ${u.latest_short}?${list}\n\nYour reels, edits and settings are kept. ${u.can_restart ? "Reelmaker restarts by itself when it's done" : "Restart Reelmaker when it's done"} (usually under a minute; longer if new tools need downloading).`)) return;
  await flushSave();
  const post = (force) => fetch("/api/update/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force }) });
  let r = await post(false).catch(() => null);
  if (r && r.status === 409) {
    const d = (await r.json().catch(() => ({}))).detail;
    if (d && d.busy) {
      if (!confirm(`Reelmaker is still working on: ${d.busy.join(", ")}.\n\nUpdating restarts Reelmaker, which stops that work (use “Retry stopped” / Export again afterwards). Update anyway?`)) return;
      r = await post(true).catch(() => null);
    } else return toast(typeof d === "string" ? d : "An update is already running.", 8000);
  }
  if (!r || !r.ok) return toast("Couldn't start the update — try again.", 6000);
  const logEl = h("pre", { class: "updLog" });
  const title = h("b", {}, "⬆ Updating Reelmaker…");
  const hint = h("span", { class: "hint" }, "Don't close Reelmaker. This page reloads by itself when it's done.");
  const box = h("div", { class: "updBox" }, title, hint, logEl);
  const ov = h("div", { id: "restartOverlay" }, box);
  document.body.append(ov);
  while (true) {
    await new Promise((res) => setTimeout(res, 800));
    const st = await fetch("/api/update/status", { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null);
    if (!st) { title.textContent = "↻ Restarting Reelmaker…"; await waitForRestart(ov); return; }   // server went down: it's restarting
    logEl.textContent = st.log.join("\n"); logEl.scrollTop = logEl.scrollHeight;
    if (st.error) {
      title.textContent = "Update failed";
      hint.textContent = st.error;
      box.append(h("button", { class: "small", onclick: () => ov.remove() }, "Close"));
      return;
    }
    if (st.restarting) { title.textContent = "↻ Restarting Reelmaker…"; hint.textContent = "Almost done — this page reloads by itself."; await waitForRestart(ov); return; }
    if (st.done && st.result && st.result.up_to_date) {
      title.textContent = "✓ Already up to date"; hint.textContent = "";
      box.append(h("button", { class: "small", onclick: () => { ov.remove(); checkUpdates(true); } }, "Close"));
      return;
    }
    if (st.done) {
      title.textContent = "✓ Update installed";
      hint.textContent = "Restart Reelmaker to use the new version: close its Terminal window and double-click “Start Reelmaker” again.";
      box.append(h("button", { class: "small", onclick: () => restartReelmaker() }, "Try restarting now"), h("button", { class: "ghost small", onclick: () => ov.remove() }, "Later"));
      return;
    }
  }
}
$("#updateBtn").onclick = () => runUpdate();
$("#checkUpdBtn").onclick = async (e) => {
  e.preventDefault();
  $("#versionText").textContent = "Checking for updates…";
  const u = await checkUpdates(true);
  if (!u) { $("#versionText").textContent = "Reelmaker"; return toast("Couldn't check for updates (restart Reelmaker if it's an old version).", 6000); }
  if (u.error) toast(u.error, 7000);
  else if (u.update_available) runUpdate();
  else toast(`Reelmaker is up to date (${u.current_short}).`);
};
checkUpdates(false); setInterval(() => checkUpdates(false), 3 * 3600e3);

(function watchForUpdate() {
  let mine = null;
  const check = async () => {
    const resp = await fetch("/api/ui-version").catch(() => null);
    if (!resp) return;                                  // server not reachable right now (restarting?)
    if (resp.status === 404) { restartBar(true); return; }   // a server too old to even know this check
    const r = await resp.json().catch(() => null);
    if (!r) return;
    restartBar(r.api !== API_VERSION || r.restart_needed);
    if (mine == null) { mine = r.build; return; }
    if (r.build !== mine && !$("#updBar")) {
      document.body.prepend(h("div", { id: "updBar", class: "updBar" },
        h("span", {}, "Reelmaker was updated. Reload this page to use the new version (your edits are saved)."),
        h("button", { class: "small", onclick: async () => { await flushSave(); location.reload(); } }, "Reload now")));
    }
  };
  check(); setInterval(check, 20000);
})();

/* ------------------------------------------------------------------ editor: previous / next reel */
const reelHash = (folder) => `#/${enc(S.day)}/${enc(folder)}`;
function reelTag(r) {
  if (r.final) return "✓ exported";
  if (r.state === "error") return "stopped";
  if (r.state !== "ready") return r.state + (r.progress != null ? ` ${Math.floor(r.progress * 100)}%` : "") + "…";
  return r.drafts ? "preview ready" : "ready";
}
async function drawReelNav() {
  const day = S.day, folder = S.folder;
  const reels = await api("GET", `/api/days/${enc(day)}/reels`).catch(() => []);
  if (S.day !== day || S.folder !== folder) return;   // you already moved on
  S.navReels = reels;   // reel-01, reel-02, … (oldest first)
  const i = reels.findIndex((r) => r.folder === folder);
  const pick = $("#reelPick");
  pick.innerHTML = "";
  reels.forEach((r, k) => pick.append(h("option", { value: r.folder, selected: k === i },
    `${String(k + 1).padStart(2, "0")} · ${r.name || r.folder} — ${reelTag(r)}`)));
  $("#reelPos").textContent = i >= 0 ? `Reel ${i + 1} of ${reels.length} · ${reels.filter((r) => r.final).length} exported` : "";
  const prev = reels[i - 1], next = reels[i + 1];
  $("#prevReel").disabled = !prev; $("#prevReel").title = prev ? `Previous: ${prev.name || prev.folder}` : "This is the first reel";
  $("#nextReel").disabled = !next; $("#nextReel").title = next ? `Next: ${next.name || next.folder}` : "This is the last reel";
  // after this reel is exported, point to the next one that still needs exporting
  const todo = reels.slice(i + 1).concat(reels.slice(0, i)).find((r) => !r.final && r.state === "ready");
  const nb = $("#nextTodo");
  nb.classList.toggle("hidden", !todo || !(S.p && S.p.final));
  if (todo) { nb.textContent = `Next to export: ${todo.name || todo.folder} →`; nb.onclick = () => (location.hash = reelHash(todo.folder)); }
}
function stepReel(d) {
  const reels = S.navReels || [], i = reels.findIndex((r) => r.folder === S.folder);
  const r = reels[i + d];
  if (r) location.hash = reelHash(r.folder);
}
$("#prevReel").onclick = () => stepReel(-1);
$("#nextReel").onclick = () => stepReel(1);
$("#reelPick").onchange = (e) => { if (e.target.value !== S.folder) location.hash = reelHash(e.target.value); };
// Alt+← / Alt+→ switch reels (not while typing in a box)
document.addEventListener("keydown", (e) => {
  if (!S.folder || !e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ""))) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); stepReel(-1); }
  if (e.key === "ArrowRight") { e.preventDefault(); stepReel(1); }
});

/* ------------------------------------------------------------------ editor: load / poll / save */
async function openReel() {
  // everything that belongs to the reel you were on is dropped here, so nothing of it shows in this one
  const token = S.openToken = (S.openToken || 0) + 1;
  S.lastDone = undefined; S.lastErr = undefined; S.expectDraft = false; S.selDrafts = new Set(); S.playList = null;
  const dv = $("#draftV"); dv.pause(); dv.removeAttribute("src"); dv.dataset.reel = ""; dv.dataset.newest = ""; dv.load();
  const ta = $("#ttsAudio"); if (ta) { ta.pause(); ta.removeAttribute("src"); ta.dataset.key = ""; ta.classList.add("hidden"); }
  $("#draftProg").textContent = ""; $("#finalProg").textContent = "";
  const p = await api("GET", reelUrl());
  if (token !== S.openToken) return;   // you clicked on to another reel meanwhile: that one wins
  S.p = p;
  S.renderSig = p.segments ? renderSig(p) : null;   // the newest preview is taken to match what you open
  S.piece = 0; S.kf = 0;
  $("#reelName").value = S.p.name || S.folder;
  drawStatus();
  if (S.p.state === "ready") { $("#edBody").classList.remove("hidden"); buildEditor(); }
  else $("#edBody").classList.add("hidden");
  schedulePoll();
}
const isBusy = (p) => p && ((p.state !== "ready" && p.state !== "error") || !!(p.job || {}).render);
function schedulePoll(ms = 1000) {
  clearTimeout(S.poll);
  if (S.folder && document.visibilityState === "visible") S.poll = setTimeout(poll, ms);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (S.folder) poll();
  else if (S.st) loadReels();
});
async function poll() {
  if (!S.folder) return;
  await pollOnce();
  if (isBusy(S.p)) schedulePoll();
}
async function pollOnce() {
  if (!S.p) return;
  const prev = S.p.state, mine = S.p, url = reelUrl();
  const p = await fetch(url).then((r) => r.json()).catch(() => null);
  if (!p || p.version == null) return;
  if (S.p !== mine || reelUrl() !== url) return;   // an answer about the reel you just left: ignore it
  Object.assign(S.p, { job: p.job, log: p.log, state: p.state, error: p.error, drafts: p.drafts, final: p.final });
  if (prev !== "ready" && p.state === "ready") { S.p = p; $("#edBody").classList.remove("hidden"); buildEditor(); }
  drawStatus(); drawRenderStatus();
}
/* progress formatting */
function fmtBytes(b) {
  if (b == null || isNaN(b)) return "";
  if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(b >= 1e8 ? 0 : 1) + " MB";
  if (b >= 1e3) return Math.round(b / 1e3) + " KB";
  return Math.round(b) + " B";
}
function fmtDur(s) {
  if (s == null || isNaN(s) || !isFinite(s)) return "";
  s = Math.max(0, Math.round(s));
  const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function taskLine(t) {
  // "12.4 MB of ~31 MB · 4.2 MB/s · about 0:18 left"  /  "2:10 of 8:05 · 3.1× realtime · about 1:40 left"
  const parts = [];
  if (t.unit === "busy") return `working on it · ${fmtDur(Date.now() / 1000 - t.at + (t.elapsed || 0))} so far`;
  const stale = t.at && Date.now() / 1000 - t.at > 10;
  if (t.unit === "bytes") {
    if (t.done > 0) parts.push(fmtBytes(t.done) + (t.total ? ` of ${t.approx ? "~" : ""}${fmtBytes(t.total)}` : ""));
    if (t.speed != null && !stale) parts.push(fmtBytes(t.speed) + "/s");
  } else {
    if (t.done > 0 || t.total) parts.push(fmtDur(t.done) + (t.total ? ` of ${fmtDur(t.total)}` : ""));
    if (t.speed != null && !stale && t.speed > 0) parts.push(`${t.speed >= 10 ? Math.round(t.speed) : t.speed.toFixed(1)}× realtime`);
  }
  if (stale) parts.push(`no new data for ${fmtDur(Date.now() / 1000 - t.at)} — still waiting`);
  else if (t.eta != null && t.frac < 1) parts.push(`about ${fmtDur(t.eta)} left`);
  if (!t.done && !stale) parts.push(t.unit === "bytes" ? "connecting…" : "starting…");
  return parts.join(" · ");
}
function drawStatus() {
  const p = S.p, j = p.job || {}, box = $("#statusBox");
  // keep the Details panel open (and scrolled) across the once-a-second redraw
  const oldDet = box.querySelector("details.more"), wasOpen = !!(oldDet && oldDet.open);
  const oldLog = box.querySelector(".log"), logTop = oldLog ? oldLog.scrollTop : 0;
  const logAtEnd = oldLog ? oldLog.scrollTop + oldLog.clientHeight >= oldLog.scrollHeight - 4 : true;
  box.innerHTML = "";
  if (p.state === "ready") { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const labels = { queued: "Waiting in line…", locating: "Finding the moment in the video…", fetching: "Getting the footage…", transcribing: "Listening to what's said…", analyzing: "Finding the webcam, reactions and the story…", error: "Something went wrong" };
  const cancelled = p.state === "error" && /^Cancelled/.test(p.error || "");
  box.append(h("b", {}, cancelled ? "Stopped" : (labels[p.state] || p.state)));
  const last = (p.log || []).slice(-1)[0];
  if (p.state !== "error") {
    const pct = Math.min(100, Math.max(0, (j.progress || 0) * 100));
    const el = j.started ? Date.now() / 1000 - j.started : null;
    box.append(h("div", { class: "progRow" }, h("span", {}, "Overall"),
      h("span", { class: "hint" }, `${Math.floor(pct)}%` + (el != null && p.state !== "queued" ? ` · ${fmtDur(el)} elapsed` : ""))),
      h("div", { class: "bar overall", role: "progressbar", "aria-valuenow": Math.floor(pct), "aria-valuemin": 0, "aria-valuemax": 100 },
        h("i", { style: `width:${pct.toFixed(1)}%` })));
    const t = j.task;
    if (t) {
      const tp = Math.min(100, (t.frac || 0) * 100);
      box.append(h("div", { class: "task" },
        h("div", { class: "progRow" }, h("span", {}, t.label), h("span", { class: "hint" }, t.known ? `${Math.floor(tp)}%` : "")),
        // the "busy" stripe is timed off the clock so it keeps sliding smoothly through the once-a-second redraw
        h("div", { class: "bar small" + (t.known ? "" : " indet") }, h("i", { style: t.known ? `width:${tp.toFixed(1)}%` : `animation-delay:-${Date.now() % 1400}ms` })),
        h("div", { class: "hint stats" }, taskLine(t))));
    }
    box.append(h("div", { class: "hint" }, last ? last.slice(9) : ""));
    box.append(h("button", { class: "ghost small", onclick: async () => { await api("POST", reelUrl("/cancel")); toast("Stopping…"); schedulePoll(500); } }, "Cancel"));
  }
  if (p.error) box.append(h("div", { class: "err" }, p.error.split("\n")[0]), h("button", { onclick: async () => { await api("POST", reelUrl("/retry")); S.p.state = "queued"; S.p.job = { progress: 0 }; drawStatus(); schedulePoll(500); } }, "Try again"));
  const log = h("div", { class: "log" }, (p.log || []).join("\n"));
  box.append(h("details", { class: "more", open: wasOpen }, h("summary", {}, "Details"), log));
  if (wasOpen) log.scrollTop = logAtEnd ? log.scrollHeight : logTop;
}
let saveTimer;
function voiceKey() {
  const k = S.p.hook || {};
  return JSON.stringify([k.voice_text, k.voice, k.speed, k.energy, k.voice_ref, k.voice_file]);
}
function changed(redraw = true) {
  // any edit makes the rendered preview out of date
  updateStale();
  // never let the little player replay a voiceover for old text
  const a = $("#ttsAudio");
  if (a && a.dataset.key && a.dataset.key !== voiceKey()) { a.pause(); a.removeAttribute("src"); a.load(); a.dataset.key = ""; a.classList.add("hidden"); }
  $("#saveState").textContent = "Saving…";
  clearTimeout(saveTimer);
  // remember which reel this edit belongs to, so switching reels can never save it into another one
  const p = S.p, url = reelUrl();
  S.pendingSave = async () => {
    S.pendingSave = null;
    const r = await api("PUT", url, { name: p.name, segments: p.segments, cold_open: p.cold_open, crops: p.crops, hook: p.hook, captions: p.captions, caption_style: p.caption_style });
    p.duration = r.duration;
    if (S.p === p) { drawDur(); $("#saveState").textContent = "Saved"; }
  };
  saveTimer = setTimeout(() => S.pendingSave && S.pendingSave(), 500);
  if (redraw) { drawTimeline(); drawDur(); }
}
async function flushSave() {  // save an edit that is still waiting on its short delay, right now
  clearTimeout(saveTimer);
  if (S.pendingSave) await S.pendingSave().catch(() => {});
}
function drawDur() {
  const d = cutItems().reduce((a, x) => a + x.end - x.start, 0);
  const b = $("#durBadge"); b.textContent = `Reel length ${d.toFixed(1)}s`;
  b.className = "badge " + (d > 90 ? "warn" : "");
}
$("#reelName").onchange = (e) => { S.p.name = e.target.value; changed(false); };
$("#delReel").onclick = async () => {
  if (!confirm("Delete this reel? Its folder (drafts, final export, footage) goes to the Trash.")) return;
  await api("DELETE", reelUrl()); toast("Moved to the Trash."); location.hash = "";
};
$("#openFolder").onclick = () => api("POST", reelUrl("/reveal"), { file: "" });

/* ------------------------------------------------------------------ editor: source video */
const V = $("#srcV");
const piece = (i = S.piece) => S.p.pieces[i];
const srcNow = () => piece().offset + V.currentTime;
function setPiece(i, t) {
  if (i !== S.piece || !V.src) {
    S.piece = i; $("#pieceSel").value = i;
    V.src = mediaUrl("source/" + piece(i).file);
    V.addEventListener("loadedmetadata", () => { if (t != null) seek(t); layoutBoxes(); }, { once: true });
  } else if (t != null) seek(t);
}
function seek(t) { V.currentTime = Math.max(0, Math.min(piece().length - 0.05, t - piece().offset)); }
function goto(pi, t) { setPiece(pi, t); }
$("#playBtn").onclick = () => (V.paused ? V.play() : V.pause());
$("#pieceSel").onchange = (e) => setPiece(+e.target.value, S.p.ranges[+e.target.value].start);
V.addEventListener("timeupdate", () => {
  $("#srcTime").textContent = fmt(srcNow());
  drawPlayhead();
  const L = S.playList;
  if (L && !V.paused) {
    const it = L.items[L.i];
    if (S.piece !== it.piece || srcNow() >= it.end - 0.03) {
      L.i++;
      if (L.i >= L.items.length) { V.pause(); S.playList = null; return; }
      const nx = L.items[L.i];
      if (nx.piece !== S.piece) { setPiece(nx.piece, nx.start); V.addEventListener("seeked", () => V.play(), { once: true }); }
      else seek(nx.start);
    }
  }
});
V.addEventListener("pause", () => { if (S.playList && !V.seeking) S.playList = null; });
function cutItems() {
  const out = [];
  const c = S.p.cold_open;
  if (c && c.enabled && c.end > c.start) out.push({ piece: c.piece, start: c.start, end: c.end });
  for (const s of S.p.segments) if (s.enabled && s.end - s.start >= 0.1) out.push(s);
  return out;
}
$("#playCut").onclick = () => {
  const items = cutItems(); if (!items.length) return;
  S.playList = { items, i: 0 };
  const go = () => V.play();
  if (items[0].piece !== S.piece) { setPiece(items[0].piece, items[0].start); V.addEventListener("seeked", go, { once: true }); }
  else { seek(items[0].start); go(); }
};

/* ------------------------------------------------------------------ editor: build */
function buildEditor() {
  const p = S.p;
  const ps = $("#pieceSel"); ps.innerHTML = "";
  p.pieces.forEach((pc, i) => ps.append(h("option", { value: i }, `Range ${i + 1}: ${fmt(p.ranges[i].start)}–${fmt(p.ranges[i].end)}`)));
  ps.classList.toggle("hidden", p.pieces.length < 2);
  V.removeAttribute("src"); setPiece(0, p.ranges[0].start);
  drawTimeline(); drawDur(); drawCut(); drawCrops(); drawHook(); drawCaps(); drawExport(); drawRenderStatus();
  showTab(S.tab);
}
$$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
function showTab(t) {
  S.tab = t;
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== t));
  const hideSrc = t === "hook" || t === "caps";
  if (hideSrc) { V.pause(); S.playList = null; }  // a hidden player must never keep playing
  $("#srcCol").classList.toggle("hidden", hideSrc);
  const crops = t === "crops";
  $("#boxGame").classList.toggle("hidden", !crops || S.p.crops.layout === "splitfit");
  $("#boxFace").classList.toggle("hidden", !crops || !isSplit());
  if (crops) layoutBoxes();
}

/* ------------------------------------------------------------------ timeline */
function drawTimeline() {
  const tl = $("#timeline"); tl.innerHTML = "";
  S.p.pieces.forEach((pc, i) => {
    const a = pc.offset, L = pc.length, pct = (t) => `${((t - a) / L) * 100}%`, w = (s, e) => `${((e - s) / L) * 100}%`;
    const r = S.p.ranges[i];
    const bar = h("div", { class: "tl", "data-piece": i, title: "Click to jump" },
      h("div", { class: "rng", style: `left:${pct(r.start)};width:${w(r.start, r.end)}` }),
      ...S.p.segments.filter((s) => s.piece === i).map((s) => h("div", { class: "seg" + (s.enabled ? "" : " off"), style: `left:${pct(s.start)};width:${w(s.start, s.end)}` })),
      ...(S.p.peaks || []).filter((k) => k.piece === i).map((k) => h("div", { class: "pk", style: `left:${pct(k.t)}`, title: `Loud moment ${fmt(k.t)}` })),
      S.p.cold_open && S.p.cold_open.enabled && S.p.cold_open.piece === i ? h("div", { class: "cold", style: `left:${pct(S.p.cold_open.start)};width:${w(S.p.cold_open.start, S.p.cold_open.end)}` }) : null,
      h("div", { class: "ph" }),
      h("span", { class: "lab" }, `${fmt(a)} – ${fmt(a + L)}`));
    bar.onclick = (e) => { const rc = bar.getBoundingClientRect(); goto(i, a + ((e.clientX - rc.left) / rc.width) * L); };
    tl.append(bar);
  });
  tl.append(h("div", { class: "hint" }, "Dashed box = your range (+10s fetched each side) · orange = kept segments · red = loud moments · purple = cold open"));
  drawPlayhead();
}
function drawPlayhead() {
  if (!S.p || !S.p.pieces) return;
  $$(".tl").forEach((bar) => {
    const i = +bar.dataset.piece, ph = $(".ph", bar);
    ph.style.display = i === S.piece ? "" : "none";
    if (i === S.piece) ph.style.left = `${(V.currentTime / piece(i).length) * 100}%`;
  });
}

/* ------------------------------------------------------------------ cut pane */
function timeInput(val, onset) {
  const inp = h("input", { type: "text", value: fmt(val), style: "width:96px;font-variant-numeric:tabular-nums" });
  inp.onchange = () => { const t = parseT(inp.value); if (t == null) { inp.value = fmt(val); return; } onset(t); };
  return inp;
}
function drawCut() {
  const p = S.p;
  // suggestions
  const sl = $("#suggList"); sl.innerHTML = "";
  if (!(p.suggestions || []).length) sl.append(h("p", { class: "hint" }, "No changes suggested — the ranges look clean."));
  const icon = { extend_start: "⇤ Add", extend_end: "Add ⇥", cut: "✂ Cut" };
  for (const s of p.suggestions || []) {
    sl.append(h("div", { class: "sugg" + (s.applied ? " applied" : "") },
      h("span", { class: "badge" + (s.kind === "cut" ? "" : " warn") }, `${icon[s.kind]} ${s.seconds}s`),
      h("span", { class: "why" }, s.reason, s.optional ? h("span", { class: "hint" }, " (optional)") : null),
      h("button", { class: "ghost small", onclick: () => goto(s.piece, s.kind === "extend_end" ? s.start - 2 : s.start - 1) }, "▶ Look"),
      h("button", { class: "small", onclick: async () => { S.p = await api("POST", reelUrl("/suggestion"), { id: s.id, undo: !!s.applied }); drawTimeline(); drawCut(); drawDur(); drawCaps(); } }, s.applied ? "Undo" : "Apply")));
  }
  // segments
  const sg = $("#segList"); sg.innerHTML = "";
  p.segments.forEach((s, i) => {
    const re = () => { changed(); drawCut(); drawCaps(); };
    sg.append(h("div", { class: "segRow" },
      h("input", { type: "checkbox", checked: s.enabled, title: "Include", onchange: (e) => { s.enabled = e.target.checked; re(); } }),
      p.pieces.length > 1 ? h("span", { class: "tag" }, `R${s.piece + 1}`) : null,
      timeInput(s.start, (t) => { s.start = Math.min(t, s.end - 0.1); re(); }),
      h("button", { class: "ghost small", title: "Set start to playhead", onclick: () => { if (S.piece !== s.piece) return toast("Jump to this segment's range first."); s.start = Math.min(srcNow(), s.end - 0.1); re(); } }, "⤓in"),
      "→",
      timeInput(s.end, (t) => { s.end = Math.max(t, s.start + 0.1); re(); }),
      h("button", { class: "ghost small", title: "Set end to playhead", onclick: () => { if (S.piece !== s.piece) return toast("Jump to this segment's range first."); s.end = Math.max(srcNow(), s.start + 0.1); re(); } }, "⤓out"),
      h("span", { class: "hint" }, `${(s.end - s.start).toFixed(1)}s`),
      h("button", { class: "ghost small", onclick: () => { goto(s.piece, s.start); V.play(); } }, "▶"),
      h("button", { class: "ghost small", disabled: p.segments.length < 2, onclick: () => { p.segments.splice(i, 1); re(); } }, "✕")));
  });
  // cold open
  const c = p.cold_open, cb = $("#coldBox"); cb.innerHTML = "";
  cb.append(h("div", { class: "row" },
    h("label", { class: "chk" }, h("input", { type: "checkbox", checked: c.enabled, onchange: (e) => { c.enabled = e.target.checked; changed(); } }), "Use cold open"),
    timeInput(c.start, (t) => { c.start = t; c.end = Math.max(c.end, t + 0.3); changed(); drawCut(); }), "→",
    timeInput(c.end, (t) => { c.end = Math.max(t, c.start + 0.3); changed(); drawCut(); }),
    h("button", { class: "ghost small", onclick: () => { c.piece = S.piece; const t = srcNow(); c.start = +(t - 0.8).toFixed(2); c.end = +(t + 0.8).toFixed(2); changed(); drawCut(); } }, "Center on playhead"),
    h("button", { class: "ghost small", onclick: () => { goto(c.piece, c.start); V.play(); } }, "▶")));
  if ((p.peaks || []).length) cb.append(h("div", { class: "row hint" }, "Loud moments: ", ...p.peaks.map((k) => h("button", { class: "ghost small", onclick: () => goto(k.piece, k.t - 1.5) }, fmt(k.t)))));
  const hk = p.hook || {};
  $("#storyNote").innerHTML = "";
  // (Element.append would print a null as the text "null", so leave the empty ones out)
  $("#storyNote").append(...[h("b", {}, "Story idea: "), hk.story || "—",
    hk.payoff_time ? h("div", {}, `Suggested payoff ≈ ${fmt(hk.payoff_time)}. Make sure the cut ends on it or just after the reaction.`) : null,
    hk.confidence === "low" ? h("div", { class: "err" }, "Low confidence — this is a draft cut. Watch the range and set the opening and payoff yourself.") : null].filter((x) => x != null));
}
$("#splitBtn").onclick = () => {
  const t = srcNow(); const i = S.p.segments.findIndex((s) => s.piece === S.piece && s.start + 0.1 < t && t < s.end - 0.1);
  if (i < 0) return toast("Move the playhead inside a kept segment first.");
  const s = S.p.segments[i];
  S.p.segments.splice(i + 1, 0, { ...s, id: Math.random().toString(16).slice(2, 10), start: +t.toFixed(2) });
  s.end = +t.toFixed(2); changed(); drawCut();
};
$("#resetCut").onclick = async () => { if (!confirm("Undo all cut changes and go back to your original ranges?")) return; S.p = await api("POST", reelUrl("/reset-cut")); drawTimeline(); drawCut(); drawDur(); drawCaps(); };

/* ------------------------------------------------------------------ crops pane */
const kfEdit = () => S.p.crops.keyframes[S.kf] || S.p.crops.keyframes[0];
// Same geometry as the renderer (render.py split_top / fit_game), so the boxes show exactly what gets exported.
const isSplit = (l = S.p.crops.layout) => l === "split" || l === "splitfit";
function splitTop(face, W = piece().width, H = piece().height) {
  const fw = face.w * W, fh = face.h * H;
  if (!(fw > 0 && fh > 0)) return 960;
  return Math.max(576, Math.min(960, Math.round(1080 * fh / fw / 2) * 2));
}
const gameAreaH = (k = kfEdit()) => (isSplit() ? 1920 - splitTop(k.face) : 960);
const aspect = (k = kfEdit()) => (1080 / gameAreaH(k)) * piece().height / piece().width; // normalized w/h of the game box
function fitGame(g, k = kfEdit()) {   // the game box in the exact shape of the space it fills, centred, same height
  const R = aspect(k), cx = g.x + g.w / 2, cy = g.y + g.h / 2;
  let h = g.h, w = h * R;
  if (w > 1) { w = 1; h = w / R; }
  if (h > 1) { h = 1; w = h * R; }
  return { x: Math.min(Math.max(0, cx - w / 2), 1 - w), y: Math.min(Math.max(0, cy - h / 2), 1 - h), w, h };
}
function layoutBoxes() {
  if (S.tab !== "crops" || !S.p) return;
  const k = kfEdit();
  for (const [id, key] of [["#boxFace", "face"], ["#boxGame", "game"]]) {
    const b = $(id), c = key === "game" ? fitGame(k.game, k) : k[key];
    Object.assign(b.style, { left: c.x * 100 + "%", top: c.y * 100 + "%", width: c.w * 100 + "%", height: c.h * 100 + "%" });
  }
}
function dragBox(id, key) {
  const box = $(id);
  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const resize = e.target.tagName === "I";
    const k = kfEdit();
    if (key === "game") k.game = fitGame(k.game, k);   // start from the box as it's really used
    const wrap = $("#vwrap").getBoundingClientRect(), c = k[key], st = { ...c }, x0 = e.clientX, y0 = e.clientY, R = aspect(k);
    box.setPointerCapture(e.pointerId);
    const mv = (ev) => {
      const dx = (ev.clientX - x0) / wrap.width, dy = (ev.clientY - y0) / wrap.height;
      if (resize && key === "face") {
        c.w = Math.min(Math.max(0.03, st.w + dx), 1 - st.x); c.h = Math.min(Math.max(0.03, st.h + dy), 1 - st.y);
      } else if (resize) {
        let w = Math.max(0.05, st.w + Math.max(dx, dy * R));
        w = Math.min(w, 1 - st.x, (1 - st.y) * R);
        c.w = w; c.h = w / R;
      } else {
        c.x = Math.min(Math.max(0, st.x + dx), 1 - c.w); c.y = Math.min(Math.max(0, st.y + dy), 1 - c.h);
      }
      layoutBoxes();
    };
    const up = () => {
      box.removeEventListener("pointermove", mv); box.removeEventListener("pointerup", up);
      k.game = fitGame(k.game, k);   // a new webcam shape changes the space for the game: keep its box in step
      S.p.crops.preset = null; S.p.crops.edited = true; layoutBoxes(); changed(false);
    };
    box.addEventListener("pointermove", mv); box.addEventListener("pointerup", up);
  });
}
dragBox("#boxFace", "face"); dragBox("#boxGame", "game");
window.addEventListener("resize", layoutBoxes);
function drawCrops() {
  const c = S.p.crops;
  $("#noCam").classList.toggle("hidden", isSplit(c.layout));
  $("#layoutSel").value = c.layout;
  const kl = $("#kfList"); kl.innerHTML = "";
  c.keyframes.forEach((k, i) => kl.append(h("div", { class: "kf" },
    h("label", { class: "chk" }, h("input", { type: "radio", name: "kf", checked: i === S.kf, onchange: () => { S.kf = i; layoutBoxes(); if (k.t != null) goto(S.piece, k.t + 0.5); } }),
      i === 0 ? "From the start" : "From "),
    i === 0 ? null : timeInput(k.t, (t) => { k.t = t; c.keyframes.sort((a, b) => (a.t ?? -1) - (b.t ?? -1)); changed(); drawCrops(); }),
    i === 0 ? null : h("button", { class: "ghost small", onclick: () => { c.keyframes.splice(i, 1); S.kf = 0; changed(); drawCrops(); layoutBoxes(); } }, "✕"))));
  const fh = $("#faceHints"); fh.innerHTML = "";
  for (const ch of S.p.face_changes || []) fh.append(h("div", { class: "row hint" }, `Facecam seems to move at ${fmt(ch.t)}.`,
    h("button", { class: "ghost small", onclick: () => addKf(ch.t) }, "Add crop change here")));
  const ps = $("#presetSel"); ps.innerHTML = "";
  const names = Object.keys(S.st.presets.presets || {});
  ps.append(h("option", { value: "" }, names.length ? "— choose a preset —" : "No presets yet"));
  names.forEach((n) => ps.append(h("option", { value: n, selected: c.preset === n }, n)));
  const ch = (S.p.source || {}).channel;
  $("#chanLbl").classList.toggle("hidden", !ch);
  if (ch) $("#chanLbl").lastChild.textContent = ` Use automatically for this channel (${ch})`;
}
function defaultCapY() {   // captions sit on the line between webcam and game (render.py caption_y)
  return isSplit() ? splitTop(S.p.crops.keyframes[0].face, S.p.pieces[0].width, S.p.pieces[0].height) : 1560;
}
function addKf(t) {
  const c = S.p.crops, cur = JSON.parse(JSON.stringify(kfEdit()));
  c.keyframes.push({ t: +t.toFixed(2), face: cur.face, game: cur.game });
  c.keyframes.sort((a, b) => (a.t ?? -1) - (b.t ?? -1));
  S.kf = c.keyframes.findIndex((k) => k.t === +t.toFixed(2));
  changed(); drawCrops(); layoutBoxes(); goto(S.piece, t + 0.5);
}
$("#markCam").onclick = () => {
  const k = kfEdit(); k.face = { x: 0.04, y: 0.35, w: 0.22, h: 0.25 };
  S.p.crops.layout = "split"; S.p.crops.edited = true; S.p.caption_style.y = null; changed(false); drawCrops(); showTab("crops");
  toast("Drag the blue WEBCAM box over the webcam, then resize it from its corner.", 6000);
};
$("#addKf").onclick = () => addKf(srcNow());
$("#applyAll").onclick = async () => {
  await flushSave();
  const r = await api("POST", reelUrl("/apply-layout-all"));
  if (!r.updated.length) return toast("No other reels from this video in this day folder.");
  drawReelNav();
  const n = r.updated.length;
  if (confirm(`Layout copied to ${n} other reel${n > 1 ? "s" : ""}.\n\nExport all ${n} again now with this layout? They run one after another while you keep working.`)) {
    const q = await api("POST", `/api/days/${enc(S.day)}/export-all`, { folders: r.updated.map((x) => x.folder) });
    toast(exportMsg(q), 6000);
  }
};
$("#layoutSel").onchange = (e) => {
  S.p.crops.layout = e.target.value; S.p.crops.edited = true; S.p.caption_style.y = null; changed(false); drawCrops(); showTab("crops"); drawCaps();
};
$("#applyPreset").onclick = () => {
  const n = $("#presetSel").value, pr = S.st.presets.presets[n]; if (!pr) return;
  S.p.crops.layout = pr.layout; S.p.crops.preset = n; S.p.crops.edited = true;
  S.p.crops.keyframes = [{ t: null, face: { ...pr.face }, game: { ...pr.game } }]; S.kf = 0;
  changed(false); drawCrops(); showTab("crops"); toast(`Applied “${n}”`);
};
$("#delPreset").onclick = async () => {
  const n = $("#presetSel").value; if (!n || !confirm(`Delete preset “${n}”?`)) return;
  S.st.presets = await api("DELETE", `/api/presets/${enc(n)}`); drawCrops();
};
$("#savePreset").onclick = async () => {
  const name = $("#presetName").value.trim(); if (!name) return toast("Name the preset first.");
  const k = kfEdit();
  S.st.presets = await api("POST", "/api/presets", { name, layout: S.p.crops.layout, face: k.face, game: k.game,
    channel: $("#chanDefault").checked ? (S.p.source || {}).channel : null });
  S.p.crops.preset = name; changed(false); drawCrops(); toast(`Saved preset “${name}”`);
};
// live 9:16 preview of the crops
(function preview() {
  const cv = $("#pv"), g = cv.getContext("2d");
  const loop = () => {
    requestAnimationFrame(loop);
    if (!S.p || S.tab !== "crops" || V.readyState < 2) return;
    const W = V.videoWidth, H = V.videoHeight, k = kfEdit();
    const d = (c, y, hh) => g.drawImage(V, c.x * W, c.y * H, c.w * W, c.h * H, 0, y, 270, hh);
    g.fillStyle = "#000"; g.fillRect(0, 0, 270, 480);
    if (isSplit()) {
      const f = k.face, ar = (f.w * W) / (f.h * H), top = splitTop(f, W, H) / 4, area = 480 - top;
      g.filter = "blur(6px) brightness(.8)"; d(f, 0, top); g.filter = "none";
      if (ar >= 270 / top) { const hh = 270 / ar; g.drawImage(V, f.x * W, f.y * H, f.w * W, f.h * H, 0, (top - hh) / 2, 270, hh); }
      else { const ww = top * ar; g.drawImage(V, f.x * W, f.y * H, f.w * W, f.h * H, (270 - ww) / 2, 0, ww, top); }
      if (S.p.crops.layout === "splitfit") {
        const hh = 270 * H / W;
        const sw = Math.min(W, H * 270 / area);   // blurred fill: the middle of the frame, full height
        g.filter = "blur(8px) brightness(.85)"; g.drawImage(V, (W - sw) / 2, 0, sw, H, 0, top, 270, area); g.filter = "none";
        g.drawImage(V, 0, 0, W, H, 0, top + (area - hh) / 2, 270, hh);
      } else d(fitGame(k.game, k), top, area);
    }
    else { g.filter = "blur(8px) brightness(.85)"; g.drawImage(V, (W - H * 9 / 16) / 2, 0, H * 9 / 16, H, 0, 0, 270, 480); g.filter = "none"; d(fitGame(k.game, k), 120, 240); }
    const y = (S.p.caption_style.y || defaultCapY()) / 4;
    g.strokeStyle = "rgba(255,212,0,.6)"; g.setLineDash([4, 4]); g.strokeRect(38, y - 14, 194, 28); g.setLineDash([]);
    g.fillStyle = "rgba(255,255,255,.18)"; g.fillRect(232, 250, 34, 160); g.fillRect(0, 418, 230, 62);
  };
  loop();
})();

/* ------------------------------------------------------------------ hook pane */
function drawHook() {
  const k = S.p.hook;
  $("#hookOn").checked = k.enabled;
  const opts = $("#hookOpts"); opts.innerHTML = "";
  [...new Set(k.options || [])].forEach((o) => opts.append(h("button", { class: "opt" + (o === k.screen ? " sel" : ""), title: "Use this hook (you can edit it below)",
    onclick: () => {
      k.screen = o; if (k.linked) k.voice_text = o; changed(false); drawHook();
      const ed = $("#hookScreen"); ed.focus(); ed.setSelectionRange(ed.value.length, ed.value.length);
    } }, o)));
  $("#hookScreen").value = k.screen || "";
  const vf = $("#voiceFileRow"); vf.innerHTML = ""; vf.classList.toggle("hidden", !k.voice_file);
  if (k.voice_file) vf.append("Using your voiceover: " + k.voice_file.split("/").pop() + " ", h("button", { class: "ghost small", onclick: () => { k.voice_file = null; changed(false); drawHook(); } }, "Use the AI voice instead"));
  $("#hookVoiceText").value = k.voice_text || "";
  $("#hookLinked").checked = !!k.linked; $("#hookVoiceText").disabled = !!k.linked;
  const vs = $("#voiceSel"); vs.innerHTML = "";
  for (const [id, label] of Object.entries(S.st.voices)) vs.append(h("option", { value: id, selected: id === k.voice }, label));
  $("#cbRow").classList.toggle("hidden", k.voice !== "cb" || !!k.voice_file);
  $("#refLbl").textContent = k.voice_ref ? "Matching: " + k.voice_ref.split("/").pop() : "Default narrator voice";
  $("#refClear").classList.toggle("hidden", !k.voice_ref);
  $("#energyIn").value = k.energy ?? 0.65; $("#energyVal").textContent = (+(k.energy ?? 0.65)).toFixed(2);
  $("#speedIn").value = k.speed; $("#speedVal").textContent = ` ${(+k.speed).toFixed(2)}×`;
  $("#duckIn").value = k.duck_db; $("#duckVal").textContent = `${k.duck_db} dB`;
  drawVoiceDefault();
  if (document.activeElement !== $("#hookIdea")) $("#hookIdea").value = k.idea || "";
  $("#hookConf").textContent = (k.source === "groq" ? `✍ written by Groq (${(k.writer_model || "").split("/").pop()}) · ` : "")
    + (k.writer_note && k.source !== "groq" ? "⚠ " + k.writer_note + " · " : "") + "Click a hook to use it, or edit it below";
  drawHookWriter();
}
$("#voiceFileBtn").onclick = async () => {
  const r = await api("POST", "/api/pick-audio"); if (!r.path) return;
  S.p.hook.voice_file = r.path; changed(false); drawHook();
};
$("#hookOn").onchange = (e) => { S.p.hook.enabled = e.target.checked; changed(false); };
$("#hookScreen").oninput = (e) => { const k = S.p.hook; k.screen = e.target.value; if (k.linked) { k.voice_text = k.screen; $("#hookVoiceText").value = k.screen; } changed(false); };
$("#hookVoiceText").oninput = (e) => { S.p.hook.voice_text = e.target.value; changed(false); };
$("#hookLinked").onchange = (e) => { const k = S.p.hook; k.linked = e.target.checked; if (k.linked) k.voice_text = k.screen; changed(false); drawHook(); };
$("#voiceSel").onchange = (e) => { S.p.hook.voice = e.target.value; changed(false); drawHook(); };

/* default voice tone */
const VD_KEYS = ["voice", "energy", "speed", "duck_db", "voice_ref"];
const voiceNow = () => Object.fromEntries(VD_KEYS.map((k) => [k, S.p.hook[k] ?? null]));
function voiceDesc(v) {
  const name = (S.st.voices || {})[v.voice] || v.voice;
  return [name + (v.voice_ref ? ` matching “${v.voice_ref.split("/").pop()}”` : ""),
    v.voice === "cb" ? `energy ${(+v.energy).toFixed(2)}` : null, `speed ${(+v.speed).toFixed(2)}×`, `game sound ${v.duck_db} dB`].filter(Boolean).join(" · ");
}
function drawVoiceDefault() {
  const d = S.st.voice_default; if (!d || !S.p) return;
  $("#vdDesc").textContent = (S.st.voice_default_saved ? "Yours: " : "Built-in: ") + voiceDesc(d);
  const same = VD_KEYS.every((k) => String(S.p.hook[k] ?? null) === String(d[k] ?? null));
  $("#vdMatch").classList.toggle("hidden", !same);
  $("#vdUse").disabled = same; $("#vdSave").disabled = same && S.st.voice_default_saved;
  $("#vdReset").classList.toggle("hidden", !S.st.voice_default_saved);
}
$("#vdSave").onclick = async () => {
  S.st.voice_default = await api("POST", "/api/voice-default", voiceNow()); S.st.voice_default_saved = true;
  drawVoiceDefault(); toast("Saved. Every new reel starts with this voice tone.");
};
$("#vdUse").onclick = () => {
  Object.assign(S.p.hook, Object.fromEntries(VD_KEYS.map((k) => [k, S.st.voice_default[k]])));
  changed(false); drawHook(); toast("This reel now uses your default voice — Hear it or Update preview.");
};
$("#vdReset").onclick = async () => {
  if (!confirm("Go back to the built-in default voice for new reels? (Existing reels keep their voice.)")) return;
  S.st.voice_default = await api("POST", "/api/voice-default", { reset: true }); S.st.voice_default_saved = false; drawVoiceDefault();
};
$("#vdAll").onclick = async () => {
  await flushSave();
  const r = await api("POST", `/api/days/${enc(S.day)}/apply-voice`, voiceNow());
  const others = r.updated.filter((x) => x.folder !== S.folder);
  if (!others.length) return toast("All reels in this day already use this voice.");
  if (confirm(`Voice tone copied to ${others.length} other reel${others.length > 1 ? "s" : ""}.\n\nExport them again now so their MP4s get the new voice? They run one after another.`)) {
    const q = await api("POST", `/api/days/${enc(S.day)}/export-all`, { folders: others.map((x) => x.folder) });
    toast(exportMsg(q), 6000); window.usageKick && window.usageKick();
  }
};
$("#refBtn").onclick = async () => {
  const r = await api("POST", "/api/pick-audio"); if (!r.path) return;
  S.p.hook.voice_ref = r.path; changed(false); drawHook();
  toast("New reels will use this voice sample too.");
};
$("#refClear").onclick = () => { S.p.hook.voice_ref = null; changed(false); drawHook(); };
$("#energyIn").oninput = (e) => { S.p.hook.energy = +e.target.value; $("#energyVal").textContent = (+e.target.value).toFixed(2); changed(false); drawVoiceDefault(); };
$("#speedIn").oninput = (e) => { S.p.hook.speed = +e.target.value; $("#speedVal").textContent = ` ${(+e.target.value).toFixed(2)}×`; changed(false); drawVoiceDefault(); };
$("#duckIn").oninput = (e) => { S.p.hook.duck_db = +e.target.value; $("#duckVal").textContent = `${e.target.value} dB`; changed(false); drawVoiceDefault(); };
$("#ttsBtn").onclick = async () => {
  const b = $("#ttsBtn"); b.disabled = true; b.textContent = "Generating…";
  try {
    const k = S.p.hook;
    const key = voiceKey(), a = $("#ttsAudio");
    if (k.voice_file) { a.src = "/api/local-audio?path=" + enc(k.voice_file); a.dataset.key = key; a.classList.remove("hidden"); a.play(); return; }
    if (k.voice === "cb") b.textContent = "Generating (first time takes ~30s)…";
    const r = await api("POST", reelUrl("/tts"), { text: k.voice_text, voice: k.voice, speed: k.speed, ref: k.voice_ref, energy: k.energy ?? 0.65 });
    if (voiceKey() !== key) return;  // the hook changed while generating; don't play the old one
    a.src = r.url; a.dataset.key = key; a.classList.remove("hidden"); a.play();
    if (r.duration > 3.2) toast(`Voiceover is ${r.duration}s — try a shorter line to keep it under ~3s.`, 6000);
  } finally { b.disabled = false; b.textContent = "🔊 Preview voice"; }
};
/* hook writer: local AI or the free online one (Groq). The key goes to the server once and never comes back. */
async function loadHookWriter(models = true) {
  S.hw = await api("GET", `/api/hook-ai${models ? "?models=1" : ""}`).catch(() => null);
  drawHookWriter();
}
function drawHookWriter() {
  const hw = S.hw; if (!hw) { if (!S.hwLoading) { S.hwLoading = true; loadHookWriter(); } return; }
  $("#hwMode").value = hw.mode;
  const groq = hw.mode === "groq";
  $("#hwGroq").classList.toggle("hidden", !groq);
  $("#hwKeyRow").classList.toggle("hidden", hw.has_key);
  $("#hwHasKey").classList.toggle("hidden", !hw.has_key);
  $("#hwRemove").classList.toggle("hidden", hw.key_source !== "keychain");
  const ms = $("#hwModel");
  if (hw.models && hw.models.length) {
    ms.innerHTML = ""; hw.models.forEach((m) => ms.append(h("option", { value: m, selected: m === hw.model }, m)));
  } else if (!ms.options.length && hw.model) ms.append(h("option", { value: hw.model }, hw.model));
  if (hw.secret_store) $("#hwHasKey .hint").textContent = `✓ Key saved in ${hw.secret_store}`;
  $("#hwStatus").textContent = hw.error ? "⚠ " + hw.error : groq ? (hw.has_key ? "Online writer on" : "Add your free key to switch it on") : "";
}
$("#hwMode").onchange = async (e) => {
  S.hw = await api("POST", "/api/hook-ai", { mode: e.target.value });
  if (S.hw.mode === "groq" && S.hw.has_key) await loadHookWriter(true); else drawHookWriter();
};
$("#hwSave").onclick = async () => {
  const inp = $("#hwKey"), key = inp.value.trim(); if (!key) return toast("Paste your Groq API key first.");
  const b = $("#hwSave"); b.disabled = true; b.textContent = "Checking…";
  try { S.hw = await api("POST", "/api/hook-ai/key", { key }); inp.value = ""; drawHookWriter(); toast(`Key works and is saved in ${S.hw.secret_store || "your keychain"}. New hooks are written by Groq.`); }
  catch (e) { /* api() already showed the reason */ }
  finally { b.disabled = false; b.textContent = "Save key"; inp.value = ""; }
};
$("#hwRemove").onclick = async () => {
  if (!confirm(`Remove the Groq key from ${S.hw.secret_store || "your keychain"} and go back to the local AI?`)) return;
  S.hw = await api("DELETE", "/api/hook-ai/key"); drawHookWriter();
};
$("#hwModel").onchange = async (e) => { S.hw = { ...S.hw, ...(await api("POST", "/api/hook-ai", { mode: "groq", model: e.target.value })), models: S.hw.models }; drawHookWriter(); };

$("#ideaBtn").onclick = async () => {
  const b = $("#ideaBtn"); b.disabled = true; b.textContent = "Writing…";
  try {
    const k = await api("POST", reelUrl("/hooks"), { idea: $("#hookIdea").value });
    Object.assign(S.p.hook, { options: k.options, idea: k.idea, source: k.source, writer_model: k.writer_model, writer_note: k.writer_note });
    S.p.hook.screen = k.options[0]; if (S.p.hook.linked) S.p.hook.voice_text = k.options[0];
    changed(false); drawHook();
  } finally { b.disabled = false; b.textContent = "✨ Write hooks"; }
};

/* ------------------------------------------------------------------ captions pane */
function inCut(c) { return S.p.segments.some((s) => s.enabled && s.piece === c.piece && c.start < s.end && c.end > s.start); }
const HL = ["#FFE600", "#39FF6A", "#FF5FA2", "#36D6FF", "#FF8A00"];
function swatches(el, key, def) {
  const box = $(el); box.innerHTML = "";
  HL.forEach((c) => box.append(h("button", { class: "sw" + ((S.p.caption_style[key] || def) === c ? " on" : ""), style: `background:${c}`, title: c,
    onclick: () => { S.p.caption_style[key] = c; changed(false); drawCaps(); } })));
}
function drawCaps() {
  const st = S.p.caption_style;
  swatches("#hlSwatches", "highlight", "#FFE600"); swatches("#hkSwatches", "hook_color", "#FF5FA2");
  $("#capSize").value = st.size || 80; $("#capCaps").checked = st.caps !== false;
  $("#capY").value = st.y || defaultCapY();
  const list = $("#capList"); list.innerHTML = "";
  if (!S.p.captions.length) list.append(h("p", { class: "hint" }, "No speech found, so no captions."));
  S.p.captions.forEach((c, i) => {
    const chips = h("div", { class: "chips" });
    const drawChips = () => {
      chips.innerHTML = "";
      c.text.split(/\s+/).filter(Boolean).forEach((w, wi) => chips.append(h("span", { class: "chip" + ((c.emph || []).includes(wi) ? " em" : ""),
        onclick: () => { c.emph = c.emph || []; const j = c.emph.indexOf(wi); j >= 0 ? c.emph.splice(j, 1) : c.emph.push(wi); changed(false); drawChips(); } }, w)));
    };
    drawChips();
    const txt = h("input", { type: "text", value: c.text });
    txt.oninput = () => {
      const oldW = c.text.split(/\s+/), newW = txt.value.split(/\s+/);
      const emWords = new Set((c.emph || []).map((k) => (oldW[k] || "").toLowerCase()));
      c.text = txt.value; c.emph = newW.map((w, k) => (emWords.has(w.toLowerCase()) ? k : -1)).filter((k) => k >= 0);
      changed(false); drawChips();
    };
    list.append(h("div", { class: "capRow" + (inCut(c) ? "" : " out") },
      h("span", { class: "t", title: "Jump here", onclick: () => { goto(c.piece, c.start - 0.3); V.play(); } }, fmt(c.start)),
      h("div", { class: "body" }, txt, chips,
        h("div", { class: "row tm hint" }, "from", timeInput(c.start, (t) => { c.start = t; changed(false); }), "to", timeInput(c.end, (t) => { c.end = Math.max(t, c.start + 0.1); changed(false); }),
          h("button", { class: "ghost small", onclick: () => { S.p.captions.splice(i + 1, 0, { id: Math.random().toString(16).slice(2, 10), piece: c.piece, start: c.end, end: c.end + 0.8, text: "…", emph: [] }); changed(false); drawCaps(); } }, "+ after"),
          h("button", { class: "ghost small", onclick: () => { S.p.captions.splice(i, 1); changed(false); drawCaps(); } }, "✕")))));
  });
}
$("#capSize").oninput = (e) => { S.p.caption_style.size = +e.target.value; changed(false); };
$("#capCaps").onchange = (e) => { S.p.caption_style.caps = e.target.checked; changed(false); };
$("#capY").oninput = (e) => { S.p.caption_style.y = +e.target.value; changed(false); };
$("#capYAuto").onclick = () => { S.p.caption_style.y = null; changed(false); drawCaps(); };
$("#rebuildCaps").onclick = async () => { if (!confirm("Replace all caption edits with a fresh set from the transcript?")) return; S.p.captions = await api("POST", reelUrl("/captions/rebuild")); drawCaps(); };

/* ------------------------------------------------------------------ live hook + captions over the reel player */
// Previews are rendered without hook/caption text; this layer draws them in sync with the video using the same
// rules as render.py build_ass (font, size, outline, highlight, position), so caption edits and moving them are
// instant. The final export still burns them in.
function renderSig(p) {   // everything that changes the preview VIDEO (caption text/style/position doesn't)
  const k = p.hook || {}, co = p.cold_open || {};
  return JSON.stringify([(p.segments || []).filter((s) => s.enabled !== false).map((s) => [s.piece, s.start, s.end]),
    co.enabled ? [co.piece, co.start, co.end] : 0, p.crops,
    [k.enabled, !!(k.screen || "").trim(), k.voice_text, k.voice, k.speed, k.energy, k.voice_ref, k.voice_file, k.duck_db]]);
}
function liveEntry() {   // the draft on screen, if it was rendered for live captions
  const v = $("#draftV");
  if (!S.p || v.dataset.reel !== `${S.day}/${S.folder}`) return null;
  const d = (S.p.drafts || []).find((x) => mediaUrl(x.file) + "?t=" + Math.round(x.time) === v.dataset.url);
  return d && d.live ? d : null;
}
function updateStale() {
  const st = $("#staleNote"); if (!st || !S.p) return;
  const has = (S.p.drafts || []).length;
  st.classList.toggle("hidden", !has || (!!liveEntry() && renderSig(S.p) === S.renderSig));
}
function splitTwo(text) {   // render.py _split_two
  const w = text.split(/\s+/).filter(Boolean);
  if (w.length < 3) return [text, ""];
  let best = 1e9, bi = 1;
  for (let i = 1; i < w.length; i++) { const d = Math.abs(w.slice(0, i).join(" ").length - w.slice(i).join(" ").length); if (d < best) { best = d; bi = i; } }
  return [w.slice(0, bi).join(" "), w.slice(bi).join(" ")];
}
const capYNow = () => S.p.caption_style.y || defaultCapY();
(function liveCaptions() {
  const wrap = $("#phoneWrap"), v = $("#draftV"), capEl = $("#liveCap"), hookEl = $("#liveHook"), guide = $("#capGuide");
  let lastKey = "";
  const frame = () => {
    requestAnimationFrame(frame);
    if (!S.p || !S.p.caption_style || $("#edBody").classList.contains("hidden")) return;
    const s = wrap.clientWidth / 1080, st = S.p.caption_style, y = capYNow();
    guide.style.top = y * s + "px";
    const e = liveEntry();
    $("#capGuideTxt").textContent = e ? "⇕ drag to move captions" : "⇕ drag to move · Update preview once to see captions live";
    let hookHtml = "", capHtml = "";
    if (e) {
      const t = v.currentTime, k = S.p.hook || {};
      if (k.enabled && (k.screen || "").trim() && t >= 0.05 && t < e.hook_end) {
        const [l1, l2] = splitTwo(k.screen.trim().toUpperCase());
        hookHtml = `<span>${esc(l1)}</span>` + (l2 ? `<br><span style="color:${st.hook_color || "#FF5FA2"}">${esc(l2)}</span>` : "");
      } else if (t >= e.hook_end) {
        const it = (e.timeline || []).find((x) => x.kind === "main" && t >= x.out && t < x.out + (x.end - x.start));
        if (it) {
          const src = it.start + (t - it.out);
          const c = (S.p.captions || []).find((c) => (c.piece ?? it.piece) === it.piece && Math.max(c.start, it.start) <= src && src < Math.min(c.end, it.end));
          if (c) {
            const words = c.text.split(/\s+/).filter(Boolean);
            let wt = c.words || [];
            if (wt.length !== words.length) { const d = (c.end - c.start) / Math.max(1, words.length); wt = words.map((_, i) => ({ s: c.start + i * d })); }
            let kk = 0; for (let i = 0; i < words.length; i++) if (i === 0 || wt[i].s <= src) kk = i;
            const hi = st.highlight || "#FFE600", emph = new Set(c.emph || []);
            capHtml = words.map((w, i) => {
              const txt = esc(st.caps !== false ? w.toUpperCase() : w);
              if (i === kk) return `<span class="w hl" style="color:${hi}">${txt}</span>`;   // pops 118% → 100% like the export
              return emph.has(i) ? `<span class="w" style="color:${hi}">${txt}</span>` : `<span class="w">${txt}</span>`;
            }).join(" ");
          }
        }
      }
    }
    const key = [hookHtml, capHtml, s.toFixed(4), y, st.size, st.hook_size].join("|");
    if (key === lastKey) return;
    lastKey = key;
    // libass sizes fonts by line height, the browser by em: for Arial Black the export's text is ~0.72× the CSS size
    const ASS_EM = 0.72, size = (st.size || 80) * s * ASS_EM, hsz = (st.hook_size || 70) * s * ASS_EM;
    Object.assign(capEl.style, { top: y * s + "px", fontSize: size + "px", padding: `0 ${140 * s}px`, lineHeight: 1.2,
      webkitTextStroke: `${18 * s}px #000`, textShadow: `${4 * s}px ${4 * s}px 0 rgba(0,0,0,.44)` });   // outline 9 each side
    capEl.innerHTML = capHtml; capEl.classList.toggle("hidden", !capHtml);
    Object.assign(hookEl.style, { top: y * s + "px", fontSize: hsz + "px", padding: `0 ${100 * s}px`, lineHeight: (hsz + 44 * s) * 1.0 + "px" });
    hookEl.querySelectorAll && (hookEl.innerHTML = hookHtml);
    hookEl.querySelectorAll("span").forEach((sp) => (sp.style.padding = `${6 * s}px ${22 * s}px`));
    hookEl.classList.toggle("hidden", !hookHtml);
  };
  frame();
  // drag the captions (or the yellow guide) up and down
  const startDrag = (ev) => {
    if (!S.p) return;
    ev.preventDefault(); ev.stopPropagation();
    const r = wrap.getBoundingClientRect(), s = r.width / 1080;
    wrap.classList.add("dragging");
    const move = (e2) => {
      const y = Math.round(Math.min(1800, Math.max(200, (e2.clientY - r.top) / s)) / 10) * 10;
      S.p.caption_style.y = y; $("#capY").value = y; lastKey = "";
    };
    const up = () => {
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
      wrap.classList.remove("dragging"); changed(false);
    };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    move(ev);
  };
  [capEl, hookEl, guide].forEach((el) => el.addEventListener("pointerdown", startDrag));
})();
function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ------------------------------------------------------------------ export pane */
function drawExport() {
  const y = window.scrollY;
  requestAnimationFrame(() => window.scrollTo(0, y));
  const dr = S.p.drafts || [];
  const v = $("#draftV");
  // Every reel has drafts/draft-v1.mp4 etc., so a file name alone can't tell reels apart: the player
  // remembers which reel (and which draft URL) it shows, and switches when either no longer belongs here.
  const here = `${S.day}/${S.folder}`;
  if (dr.length) {
    const last = dr[dr.length - 1], u = mediaUrl(last.file) + "?t=" + Math.round(last.time);
    const showsOwnDraft = v.dataset.reel === here && dr.some((d) => v.dataset.url === mediaUrl(d.file) + "?t=" + Math.round(d.time));
    // a preview that just finished always replaces the one on screen; a draft you picked yourself stays until then
    if (!showsOwnDraft || v.dataset.newest !== u) { v.src = u; v.dataset.url = u; v.dataset.reel = here; v.dataset.newest = u; }
  } else if (v.getAttribute("src")) { v.removeAttribute("src"); v.dataset.url = ""; v.dataset.reel = ""; v.dataset.newest = ""; v.load(); }
  updateStale();
  const dl = $("#draftList"); dl.innerHTML = "";
  if (!dr.length) dl.textContent = "No preview yet.";
  S.selDrafts = new Set([...(S.selDrafts || [])].filter((n) => dr.some((d) => d.file === "drafts/" + n)));
  if (dr.length > 1) dl.append(h("div", { class: "drow" },
    h("button", { class: "ghost small", onclick: () => { S.selDrafts = new Set(dr.map((d) => d.file.replace("drafts/", ""))); drawExport(); } }, "Select all"),
    h("button", { class: "small danger", disabled: !S.selDrafts.size, onclick: async () => {
      const n = S.selDrafts.size;
      if (!confirm(`Delete ${n} draft${n > 1 ? "s" : ""}? They go to the Trash.`)) return;
      for (const name of S.selDrafts) { const r = await api("DELETE", reelUrl("/draft/" + enc(name))).catch(() => null); if (r) S.p.drafts = r.drafts; }
      S.selDrafts.clear(); drawExport(); toast("Moved to the Trash.");
    }, id: "delDrafts" }, `Delete selected (${S.selDrafts.size})`)));
  dr.slice().reverse().forEach((d) => {
    const name = d.file.replace("drafts/", "");
    dl.append(h("div", { class: "drow" },
      dr.length > 1 ? h("input", { type: "checkbox", checked: S.selDrafts.has(name), onchange: (e) => {
        e.target.checked ? S.selDrafts.add(name) : S.selDrafts.delete(name);
        const b = $("#delDrafts"); if (b) { b.textContent = `Delete selected (${S.selDrafts.size})`; b.disabled = !S.selDrafts.size; }
      } }) : null,
      h("button", { type: "button", class: "linkbtn", onclick: (e) => {
        e.preventDefault(); const v = $("#draftV"), u = mediaUrl(d.file) + "?t=" + Math.round(d.time);
        v.src = u; v.dataset.url = u; v.dataset.reel = `${S.day}/${S.folder}`; v.play();
      } }, name),
      h("button", { class: "ghost small", title: "Delete this draft", onclick: async () => {
        if (!confirm(`Delete ${name}? It goes to the Trash.`)) return;
        const r = await api("DELETE", reelUrl("/draft/" + enc(name))); S.p.drafts = r.drafts; drawExport();
      } }, "🗑")));
  });
  const fb = $("#finalBox"); fb.innerHTML = "";
  const f = S.p.final;
  if (f) {
    fb.append(h("p", {}, h("b", {}, f.file), ` · ${new Date(f.time * 1000).toLocaleString()} · ${f.report ? f.report.size_mb + " MB" : ""}`));
    if (f.report) for (const c of f.report.checks) fb.append(h("div", { class: "check" }, h("span", { class: c.ok ? "ok" : "bad" }, c.ok ? "✓" : "✗"), h("span", {}, h("b", {}, c.name), " — ", c.detail)));
    fb.append(h("div", { class: "row" }, h("button", { class: "small", onclick: () => api("POST", reelUrl("/reveal"), { file: f.file }) }, "Show in Finder")),
      h("div", { class: "remind" }, "Before uploading: check the file's metadata yourself (Finder ▸ Get Info). The app removed title, date, encoder and location tags, but it's worth a look. Publishing is up to you."));
  }
}
function drawRenderStatus() {
  const j = S.p.job || {};
  const busy = !!j.render;
  $("#draftBtn").disabled = busy; $("#finalBtn").disabled = busy;
  // "Rendering… 42% · Encoding the preview… · about 0:12 left"
  const rtext = (word) => {
    const f = j.render_progress || 0, el = j.render_started ? Date.now() / 1000 - j.render_started : 0;
    const parts = [`${word}… ${Math.floor(f * 100)}%`];
    if (j.render_phase) parts.push(j.render_phase);
    if (f >= 0.08 && f < 1 && el > 3) parts.push(`about ${fmtDur(el * (1 - f) / f)} left`);
    return parts.join(" · ");
  };
  $("#draftProg").textContent = j.render === "draft" ? rtext("Rendering") : "";
  $("#finalProg").textContent = j.render === "final" ? rtext("Exporting") : "";
  for (const [id, kind] of [["#draftBar", "draft"], ["#finalBar", "final"]]) {
    const b = $(id), on = j.render === kind;
    b.classList.toggle("hidden", !on);
    const waiting = on && (j.render_progress || 0) < 0.05;   // voiceover / getting ready / waiting its turn
    b.classList.toggle("indet", waiting);
    if (on) b.firstChild.style.width = waiting ? "" : `${((j.render_progress || 0) * 100).toFixed(1)}%`;
  }
  if (j.render_error && !busy) {
    if (S.lastErr !== j.render_error) { S.lastErr = j.render_error; toast("Render failed — see details below the buttons", 6000); }
    $("#finalProg").textContent = "Render failed: " + j.render_error.slice(0, 400);  // stays visible until the next render
  }
  const doneKey = j.render_done + (S.p.drafts || []).length + (S.p.final ? S.p.final.time : 0);
  if (j.render_done && S.lastDone !== doneKey) {
    const fresh = S.lastDone !== undefined;  // a render finished while you're here (not just opening the reel)
    S.lastDone = doneKey;
    drawExport();
    if (j.render_done === "final") drawReelNav();   // the picker shows ✓ and "Next to export" appears
    // auto-play only in the tab where you clicked Update preview (or the one you're looking at), never in background tabs
    const mine = S.expectDraft || (document.visibilityState === "visible" && document.hasFocus());
    if (fresh && j.render_done === "draft" && mine) $("#draftV").play().catch(() => {});
    S.expectDraft = false;
  }
}

/* only one thing plays at a time: starting any video/audio pauses the others */
document.addEventListener("play", (e) => {
  $$("video, audio").forEach((m) => { if (m !== e.target && !m.paused) m.pause(); });
  if (e.target !== V) S.playList = null;
}, true);
function stopAll() { $$("video, audio").forEach((m) => m.pause()); S.playList = null; }
// a tab you can't see must stay silent
document.addEventListener("visibilitychange", () => { if (document.hidden) stopAll(); });
// playing in one Reelmaker tab pauses every other open Reelmaker tab
const chan = "BroadcastChannel" in window ? new BroadcastChannel("reelmaker-audio") : null;
const tabId = Math.random().toString(36).slice(2);
if (chan) chan.onmessage = (e) => { if (e.data && e.data.playing && e.data.from !== tabId) stopAll(); };
document.addEventListener("play", () => chan && chan.postMessage({ playing: true, from: tabId }), true);
async function startRender(final) {
  clearTimeout(saveTimer);
  if (!final) { $("#staleNote").classList.add("hidden"); S.expectDraft = true; S.renderSig = renderSig(S.p); }
  const p = S.p;
  await api("PUT", reelUrl(), { name: p.name, segments: p.segments, cold_open: p.cold_open, crops: p.crops, hook: p.hook, captions: p.captions, caption_style: p.caption_style });
  await api("POST", reelUrl("/render"), { final });
  window.usageKick && window.usageKick();
  S.p.job = { ...(S.p.job || {}), render: final ? "final" : "draft", render_progress: 0, render_error: null };
  drawRenderStatus(); schedulePoll(500);
}
$("#draftBtn").onclick = () => startRender(false);
$("#finalBtn").onclick = () => startRender(true);

route();
