"use strict";
// Stream-arkisto – web-käyttöliittymä (ei riippuvuuksia)

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const DAYS = ["Ma", "Ti", "Ke", "To", "Pe", "La", "Su"];
const STATUS = { ready: "Valmis", recording: "Tallentaa", processing: "Käsitellään", empty: "Tyhjä", failed: "Virhe" };

const state = { streams: [], recs: [], recTotal: 0, playing: null, activeIds: new Set() };

// ------------------------------------------------------------ apurit
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Virhe ${res.status}`);
  return data;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dur(s) {
  if (s == null) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min ${sec ? sec + " s" : ""}`.trim() : `${sec} s`;
}
function size(b) {
  if (b == null) return "–";
  const u = ["t", "kt", "Mt", "Gt", "Tt"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i > 1 ? 1 : 0).replace(".", ",")} ${u[i]}`;
}
function fdate(iso, withYear = true) {
  if (!iso) return "";
  const d = new Date(iso);
  const day = DAYS[(d.getDay() + 6) % 7];
  return `${day} ${d.getDate()}.${d.getMonth() + 1}.${withYear ? d.getFullYear() : ""} klo ${String(d.getHours()).padStart(2, "0")}.${String(d.getMinutes()).padStart(2, "0")}`;
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = err ? "err" : ""; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 2500);
}
async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}
function store(key, val) {
  try { if (val === undefined) return JSON.parse(localStorage.getItem(key)); localStorage.setItem(key, JSON.stringify(val)); }
  catch { return null; }
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// Avaa dialogin ja palauttaa lupauksen, joka ratkeaa kun käyttäjä tallentaa (true) tai peruu (false)
function openDialog(dlg) {
  return new Promise(resolve => {
    dlg.returnValue = "";
    dlg.showModal();
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

// ------------------------------------------------------------ välilehdet
function showTab(name) {
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(s => (s.hidden = s.id !== "tab-" + name));
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  ({ recordings: loadRecordings, schedules: loadSchedules, streams: loadStreams, status: loadStatus })[name]();
}
$$("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------ striimit
async function fetchStreams() {
  state.streams = await api("/streams");
  const sel = $("#f-stream"), cur = sel.value;
  sel.innerHTML = `<option value="">Kaikki striimit</option>` +
    state.streams.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  sel.value = cur;
}

async function loadStreams() {
  await guard(fetchStreams);
  const el = $("#str-list");
  if (!state.streams.length) {
    el.innerHTML = `<div class="empty-state">Ei vielä striimejä. Lisää ensimmäinen Icecast-osoite yllä olevasta napista.</div>`;
    return;
  }
  el.innerHTML = state.streams.map(s => `
    <div class="card ${s.enabled ? "" : "off"}">
      <div class="main">
        <div class="title">${esc(s.name)} ${s.enabled ? "" : '<span class="muted small">(pois käytöstä)</span>'}</div>
        <div class="sub"><span>${esc(s.url)}</span></div>
        <div class="sub"><span>${s.recording_count} tallennetta</span>
          <span>Hiljaisuus: ${s.silence_min_s > 0 ? `yli ${String(s.silence_min_s).replace(".", ",")} s pois (${s.silence_db} dB)` : "ei poistoa"}</span></div>
      </div>
      <div class="actions">
        <button data-now="${s.id}" ${s.enabled ? "" : "disabled"}>● Tallenna nyt</button>
        <button class="icon" data-edit="${s.id}" title="Muokkaa">✎</button>
        <button class="icon danger" data-del="${s.id}" title="Poista">🗑</button>
      </div>
    </div>`).join("");
  $$("[data-edit]", el).forEach(b => b.onclick = () => editStream(state.streams.find(s => s.id == b.dataset.edit)));
  $$("[data-del]", el).forEach(b => b.onclick = () => deleteStream(state.streams.find(s => s.id == b.dataset.del)));
  $$("[data-now]", el).forEach(b => b.onclick = () => recordNow(state.streams.find(s => s.id == b.dataset.now)));
}

async function editStream(s) {
  const f = $("#form-str");
  $("#str-h").textContent = s ? "Muokkaa striimiä" : "Uusi striimi";
  f.name.value = s?.name ?? ""; f.url.value = s?.url ?? "";
  f.silence_min_s.value = s?.silence_min_s ?? 3; f.silence_db.value = s?.silence_db ?? -50;
  f.enabled.checked = s ? !!s.enabled : true;
  $("#str-test-result").textContent = "";
  if (!(await openDialog($("#dlg-str")))) return;
  const body = { name: f.name.value, url: f.url.value, enabled: f.enabled.checked,
                 silence_min_s: f.silence_min_s.value, silence_db: f.silence_db.value };
  await guard(async () => {
    await api(s ? `/streams/${s.id}` : "/streams", { method: s ? "PUT" : "POST", body });
    toast("Striimi tallennettu"); loadStreams();
  });
}

$("#str-test").onclick = async () => {
  const out = $("#str-test-result"), url = $("#form-str").url.value;
  out.innerHTML = `<span class="muted">Testataan…</span>`;
  try {
    const r = await api("/streams/test", { method: "POST", body: { url } });
    if (!r.ok) { out.innerHTML = `<span class="bad">✕ ${esc(r.error)}</span>`; return; }
    const br = r.bit_rate ? Math.round(r.bit_rate / (r.bit_rate > 1000 ? 1000 : 1)) + " kbps" : "";
    out.innerHTML = `<span class="ok">✓ Toimii:</span> ${esc(r.codec || "?")} ${br} ${r.sample_rate ? r.sample_rate / 1000 + " kHz" : ""}`
      + (r.station ? ` · ${esc(r.station)}` : "") + (r.now_playing ? `<br><span class="muted">Nyt: ${esc(r.now_playing)}</span>` : "");
    const f = $("#form-str");
    if (!f.name.value && r.station) f.name.value = r.station;
  } catch (e) { out.innerHTML = `<span class="bad">✕ ${esc(e.message)}</span>`; }
};

async function deleteStream(s) {
  if (!confirm(`Poistetaanko striimi "${s.name}"?\n\nMyös sen aikataulut poistuvat. Tallenteet säilyvät.`)) return;
  await guard(async () => { await api(`/streams/${s.id}`, { method: "DELETE" }); toast("Poistettu"); loadStreams(); });
}

async function recordNow(s) {
  const f = $("#form-now");
  $("#now-name").textContent = s.name; f.title.value = "";
  if (!(await openDialog($("#dlg-now")))) return;
  await guard(async () => {
    await api(`/streams/${s.id}/record`, { method: "POST", body: { duration_min: f.duration_min.value, title: f.title.value } });
    toast("Tallennus aloitettu"); refreshLive(); showTab("recordings");
  });
}

// ------------------------------------------------------------ aikataulut
async function loadSchedules() {
  await guard(fetchStreams);
  const rows = await guard(() => api("/schedules")) || [];
  const el = $("#sch-list");
  if (!rows.length) {
    el.innerHTML = `<div class="empty-state">${state.streams.length ? "Ei aikatauluja. Lisää ohjelma, joka tallennetaan säännöllisesti." : "Lisää ensin striimi Striimit-välilehdellä."}</div>`;
    return;
  }
  el.innerHTML = rows.map(r => {
    const days = r.days.split(",").map(Number);
    const dtxt = days.length === 7 ? "Joka päivä" : days.join() === "0,1,2,3,4" ? "Ma–Pe" : days.join() === "5,6" ? "La–Su" : days.map(d => DAYS[d]).join(" ");
    const [h, m] = r.start_time.split(":").map(Number);
    const endM = (h * 60 + m + r.duration_min) % 1440;
    const end = `${String(Math.floor(endM / 60)).padStart(2, "0")}.${String(endM % 60).padStart(2, "0")}`;
    return `
    <div class="card ${r.enabled ? "" : "off"}">
      <div class="main">
        <div class="title">${esc(r.title)} ${r.enabled ? "" : '<span class="muted small">(pois käytöstä)</span>'}</div>
        <div class="sub"><span>${esc(r.stream_name)}</span><span>${dtxt}</span>
          <span>klo ${r.start_time.replace(":", ".")}–${end} (${dur(r.duration_min * 60)})</span>
          <span>${r.keep_days ? `säilytys ${r.keep_days} pv` : "säilytetään aina"}</span></div>
      </div>
      <div class="actions">
        <button class="icon" data-edit="${r.id}" title="Muokkaa">✎</button>
        <button class="icon danger" data-del="${r.id}" title="Poista">🗑</button>
      </div>
    </div>`;
  }).join("");
  $$("[data-edit]", el).forEach(b => b.onclick = () => editSchedule(rows.find(r => r.id == b.dataset.edit)));
  $$("[data-del]", el).forEach(b => b.onclick = () => deleteSchedule(rows.find(r => r.id == b.dataset.del)));
}

async function editSchedule(r) {
  await guard(fetchStreams);
  if (!state.streams.length) { toast("Lisää ensin striimi", true); return; }
  const f = $("#form-sch");
  $("#sch-h").textContent = r ? "Muokkaa aikataulua" : "Uusi aikataulu";
  f.stream_id.innerHTML = state.streams.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  f.stream_id.value = r?.stream_id ?? state.streams[0].id;
  f.title.value = r?.title ?? "";
  const days = (r?.days ?? "0,1,2,3,4,5,6").split(",");
  $$("input[name=d]", f).forEach(c => (c.checked = days.includes(c.value)));
  f.start_time.value = r?.start_time ?? "";
  f.duration_min.value = r?.duration_min ?? 60;
  f.keep_days.value = r?.keep_days ?? "";
  f.enabled.checked = r ? !!r.enabled : true;
  if (!(await openDialog($("#dlg-sch")))) return;
  const body = {
    stream_id: f.stream_id.value, title: f.title.value, start_time: f.start_time.value,
    days: $$("input[name=d]:checked", f).map(c => c.value), duration_min: f.duration_min.value,
    keep_days: f.keep_days.value || null, enabled: f.enabled.checked,
  };
  await guard(async () => {
    await api(r ? `/schedules/${r.id}` : "/schedules", { method: r ? "PUT" : "POST", body });
    toast("Aikataulu tallennettu"); loadSchedules();
  });
}

async function deleteSchedule(r) {
  if (!confirm(`Poistetaanko aikataulu "${r.title}"?\nJo tehdyt tallenteet säilyvät.`)) return;
  await guard(async () => { await api(`/schedules/${r.id}`, { method: "DELETE" }); toast("Poistettu"); loadSchedules(); });
}

$("#sch-add").onclick = () => editSchedule(null);
$("#str-add").onclick = () => editStream(null);

// ------------------------------------------------------------ tallenteet
function recQuery(offset = 0) {
  const p = new URLSearchParams({ limit: 50, offset });
  if ($("#f-q").value) p.set("q", $("#f-q").value);
  if ($("#f-stream").value) p.set("stream_id", $("#f-stream").value);
  if ($("#f-status").value) p.set("status", $("#f-status").value);
  return "/recordings?" + p;
}

async function loadRecordings(append = false) {
  if (!append) await guard(fetchStreams);
  const data = await guard(() => api(recQuery(append ? state.recs.length : 0)));
  if (!data) return;
  state.recs = append ? state.recs.concat(data.items) : data.items;
  state.recTotal = data.total;
  renderRecordings();
}

function renderRecordings() {
  const el = $("#rec-list");
  const filtered = $("#f-q").value || $("#f-stream").value || $("#f-status").value;
  $("#rec-summary").textContent = state.recTotal ? `${state.recTotal} tallennetta` : "";
  $("#rec-more").hidden = state.recs.length >= state.recTotal;
  if (!state.recs.length) {
    el.innerHTML = `<div class="empty-state">${filtered ? "Haulla ei löytynyt tallenteita." :
      "Ei vielä tallenteita. Lisää striimi ja aikataulu, tai aloita tallennus heti Striimit-välilehdeltä."}</div>`;
    return;
  }
  el.innerHTML = state.recs.map(r => {
    const playable = !!r.file_path;
    const saved = r.raw_duration_s && r.duration_s && r.raw_duration_s - r.duration_s > 5
      ? ` <span title="Alkuperäinen ${dur(r.raw_duration_s)}">(−${dur(r.raw_duration_s - r.duration_s)} hiljaisuutta)</span>` : "";
    const tags = r.tags ? r.tags.split(",").map(t => `<span class="tag">${esc(t)}</span>`).join(" ") : "";
    let actions = "";
    if (r.status === "recording") actions += `<button data-stop="${r.id}">■ Pysäytä</button>`;
    if (r.status === "failed") actions += `<button data-reproc="${r.id}" title="Yritä käsittelyä uudelleen">↻</button>`;
    if (playable) actions += `<a href="/media/${r.id}?download=1"><button class="icon" title="Lataa">⬇</button></a>`;
    actions += `<button class="icon" data-edit="${r.id}" title="Muokkaa">✎</button>`;
    if (r.status !== "recording") actions += `<button class="icon danger" data-del="${r.id}" title="Poista">🗑</button>`;
    return `
    <div class="card ${state.playing === r.id ? "playing" : ""}" data-id="${r.id}">
      <button class="play" data-play="${r.id}" ${playable ? "" : "disabled"} title="Toista">${state.playing === r.id ? "❚❚" : "▶"}</button>
      <div class="main">
        <div class="title">${r.keep ? '<span class="keep" title="Suojattu">★</span> ' : ""}${esc(r.title)}</div>
        <div class="sub">
          <span>${esc(r.stream_name || "–")}</span><span>${fdate(r.started_at)}</span>
          <span>${r.status === "recording" ? "päättyy " + fdate(r.planned_end, false).replace(/^.. /, "") : dur(r.duration_s) + saved}</span>
          ${r.file_size ? `<span>${size(r.file_size)}</span>` : ""}
          ${r.status !== "ready" ? `<span class="badge ${r.status}">${STATUS[r.status] || r.status}</span>` : ""}
          ${tags}
        </div>
        ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ""}
        ${r.error && r.status !== "ready" ? `<div class="desc ${r.status === "failed" ? "bad" : ""}">${esc(r.error)}</div>` : ""}
      </div>
      <div class="actions">${actions}</div>
    </div>`;
  }).join("");
  $$("[data-play]", el).forEach(b => b.onclick = () => play(state.recs.find(r => r.id == b.dataset.play)));
  $$("[data-edit]", el).forEach(b => b.onclick = () => editRecording(state.recs.find(r => r.id == b.dataset.edit)));
  $$("[data-del]", el).forEach(b => b.onclick = () => deleteRecording(state.recs.find(r => r.id == b.dataset.del)));
  $$("[data-stop]", el).forEach(b => b.onclick = () => stopRecording(+b.dataset.stop));
  $$("[data-reproc]", el).forEach(b => b.onclick = () => guard(async () => {
    await api(`/recordings/${b.dataset.reproc}/reprocess`, { method: "POST" }); toast("Käsittely jonossa"); loadRecordings();
  }));
}

async function editRecording(r) {
  const f = $("#form-rec");
  f.title.value = r.title; f.description.value = r.description; f.tags.value = r.tags.split(",").join(", ");
  f.keep.checked = !!r.keep;
  $("#rec-meta").innerHTML = [
    `Striimi: ${esc(r.stream_name || "–")}`,
    `Alkoi: ${fdate(r.started_at)}${r.ended_at ? ` · päättyi ${fdate(r.ended_at, false)}` : ""}`,
    r.duration_s != null ? `Kesto: ${dur(r.duration_s)} (alkuperäinen ${dur(r.raw_duration_s)})` : "",
    r.file_path ? `Tiedosto: ${esc(r.file_path)} · ${size(r.file_size)}` : "",
  ].filter(Boolean).join("<br>");
  if (!(await openDialog($("#dlg-rec")))) return;
  await guard(async () => {
    const upd = await api(`/recordings/${r.id}`, { method: "PUT",
      body: { title: f.title.value, description: f.description.value, tags: f.tags.value, keep: f.keep.checked } });
    Object.assign(r, upd); renderRecordings(); toast("Tallennettu");
    if (state.playing === r.id) $("#p-title").textContent = r.title;
  });
}

async function deleteRecording(r) {
  if (!confirm(`Poistetaanko tallenne "${r.title}"?\nÄänitiedosto poistetaan pysyvästi.`)) return;
  await guard(async () => {
    await api(`/recordings/${r.id}`, { method: "DELETE" });
    if (state.playing === r.id) closePlayer();
    toast("Poistettu"); loadRecordings();
  });
}

async function stopRecording(id) {
  if (!confirm("Pysäytetäänkö tallennus nyt? Tähän asti tallennettu osa säilyy.")) return;
  await guard(async () => {
    await api(`/recordings/${id}/stop`, { method: "POST" });
    toast("Pysäytetään…"); setTimeout(() => { loadRecordings(); refreshLive(); }, 1500);
  });
}

$("#f-q").addEventListener("input", debounce(() => loadRecordings(), 300));
$("#f-stream").onchange = () => loadRecordings();
$("#f-status").onchange = () => loadRecordings();
$("#rec-more").onclick = () => loadRecordings(true);

// ------------------------------------------------------------ soitin
const audio = $("#audio");

function play(r) {
  if (state.playing === r.id) { audio.paused ? audio.play() : audio.pause(); return; }
  state.playing = r.id;
  $("#player").hidden = false;
  $("#p-title").textContent = r.title;
  $("#p-sub").textContent = `${r.stream_name || ""} · ${fdate(r.started_at)}`;
  audio.src = `/media/${r.id}`;
  const pos = store("pos-" + r.id);
  audio.addEventListener("loadedmetadata", () => { if (pos && pos < audio.duration - 5) audio.currentTime = pos; }, { once: true });
  audio.playbackRate = +$("#p-rate").value;
  audio.play().catch(() => {});
  renderRecordings();
}
function closePlayer() {
  audio.pause(); audio.removeAttribute("src"); audio.load();
  state.playing = null; $("#player").hidden = true; renderRecordings();
}
audio.addEventListener("timeupdate", () => {
  if (state.playing && Math.floor(audio.currentTime) % 5 === 0) store("pos-" + state.playing, audio.currentTime);
});
audio.addEventListener("ended", () => store("pos-" + state.playing, 0));
audio.addEventListener("play", () => $$(`[data-play="${state.playing}"]`).forEach(b => (b.textContent = "❚❚")));
audio.addEventListener("pause", () => $$(`[data-play="${state.playing}"]`).forEach(b => (b.textContent = "▶")));
$("#p-back").onclick = () => (audio.currentTime = Math.max(0, audio.currentTime - 30));
$("#p-fwd").onclick = () => (audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 30));
$("#p-rate").onchange = e => (audio.playbackRate = +e.target.value);
$("#p-close").onclick = closePlayer;

// ------------------------------------------------------------ tila
async function loadStatus() {
  const s = await guard(() => api("/status"));
  if (!s) return;
  const pct = Math.round((s.disk.used / s.disk.total) * 100);
  const c = s.counts;
  $("#status-body").innerHTML = `
    <div class="grid">
      <div class="stat"><div class="l">Levytila vapaana</div><div class="v">${size(s.disk.free)}</div>
        <div class="bar"><div style="width:${pct}%"></div></div><div class="small muted" style="margin-top:6px">${pct} % käytössä · ${esc(s.disk.path)}</div></div>
      <div class="stat"><div class="l">Arkisto</div><div class="v">${(c.ready || 0) + (c.empty || 0)} tallennetta</div>
        <div class="small muted">${size(s.archive_bytes)} · ${dur(s.archive_seconds)}</div></div>
      <div class="stat"><div class="l">Käsittely</div><div class="v">${s.processing ? "Käynnissä" : "Vapaa"}</div>
        <div class="small muted">${s.queue} jonossa${c.failed ? ` · <span class="bad">${c.failed} epäonnistunutta</span>` : ""}</div></div>
    </div>
    <div class="section-h">Käynnissä nyt</div>
    <div class="list">${s.active.length ? s.active.map(a => `
      <div class="card"><span class="dot"></span><div class="main"><div class="title">${esc(a.title)}</div>
      <div class="sub"><span>${esc(a.stream_name)}</span><span>alkoi ${fdate(a.started_at, false)}</span><span>päättyy ${fdate(a.end, false)}</span>
      ${a.reconnects ? `<span class="bad">${a.reconnects} katkoa</span>` : ""}</div></div>
      <div class="actions"><button data-stop="${a.id}">■ Pysäytä</button></div></div>`).join("")
      : `<div class="empty-state">Ei käynnissä olevia tallennuksia</div>`}</div>
    <div class="section-h">Seuraavat ajastetut</div>
    <div class="list">${s.upcoming.length ? s.upcoming.map(u => `
      <div class="card"><div class="main"><div class="title">${esc(u.title)}</div>
      <div class="sub"><span>${esc(u.stream_name)}</span><span>${fdate(u.start)}</span><span>${dur(u.duration_min * 60)}</span></div></div></div>`).join("")
      : `<div class="empty-state">Ei ajastettuja tallennuksia</div>`}</div>
    <p class="small muted" style="margin-top:20px">Stream-arkisto ${esc(s.version)} · palvelimen aika ${fdate(s.now)}</p>`;
  $$("[data-stop]", $("#status-body")).forEach(b => b.onclick = async () => { await stopRecording(+b.dataset.stop); setTimeout(loadStatus, 1600); });
}

// ------------------------------------------------------------ live-ilmaisin ja päivitys
async function refreshLive() {
  let s;
  try { s = await api("/status"); } catch { return; }
  const live = $("#live");
  live.hidden = !s.active.length;
  live.innerHTML = s.active.length ? `<span class="dot"></span> Tallentaa: ${s.active.map(a => esc(a.stream_name)).join(", ")}` : "";
  // jos tallennusten tila muuttui, päivitä lista
  const ids = new Set(s.active.map(a => a.id));
  const changed = ids.size !== state.activeIds.size || [...ids].some(i => !state.activeIds.has(i)) || s.processing || s.queue;
  state.activeIds = ids;
  if (!$("#tab-recordings").hidden && (changed || state.recs.some(r => r.status === "processing" || r.status === "recording"))) {
    loadRecordings();
  }
  if (!$("#tab-status").hidden) loadStatus();
}
setInterval(refreshLive, 8000);

// ------------------------------------------------------------ käynnistys
const TABS = ["recordings", "schedules", "streams", "status"];
const tabFromHash = () => (TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "recordings");
window.addEventListener("hashchange", () => showTab(tabFromHash()));
showTab(tabFromHash());
refreshLive();
