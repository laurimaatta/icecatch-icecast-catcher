"use strict";
// IceCatch – web UI (no dependencies). UI texts are Finnish.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const DAYS = ["Ma", "Ti", "Ke", "To", "Pe", "La", "Su"];

const state = { streams: [], recs: [], recTotal: 0, playing: null, selected: new Set() };
const PAGE = 50;

// ------------------------------------------------------------ helpers
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

// Opens a dialog; resolves to true when the user saves, false when they cancel
function openDialog(dlg) {
  return new Promise(resolve => {
    dlg.returnValue = "";
    dlg.showModal();
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

// ------------------------------------------------------------ tabs
function showTab(name) {
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(s => (s.hidden = s.id !== "tab-" + name));
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  ({ recordings: loadRecordings, schedules: loadSchedules, streams: loadStreams, status: loadStatus })[name]();
}
$$("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------ streams
async function fetchStreams() {
  state.streams = await api("/streams");
  const sel = $("#f-stream"), cur = sel.value;
  sel.innerHTML = `<option value="">Kaikki lähteet</option>` +
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
          ${s.auto_record ? `<span>Automaattinen, enintään ${dur(s.max_duration_min * 60)}</span>
            ${s.enabled && s.on_air != null ? `<span class="${s.on_air ? "ok" : "muted"}">${s.on_air ? "Lähetys käynnissä" : "Ei lähetystä"}</span>` : ""}` : ""}</div>
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
  f.silence_db.value = s?.silence_db ?? -50;
  f.enabled.checked = s ? !!s.enabled : true;
  f.auto_record.checked = s ? !!s.auto_record : false;
  f.max_hours.value = s ? s.max_duration_min / 60 : 4;
  $("#str-test-result").textContent = "";
  if (!(await openDialog($("#dlg-str")))) return;
  const body = { name: f.name.value, url: f.url.value, enabled: f.enabled.checked,
                 silence_db: f.silence_db.value,
                 auto_record: f.auto_record.checked, max_duration_min: Math.round(f.max_hours.value * 60) };
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

// ------------------------------------------------------------ schedules
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

// ------------------------------------------------------------ recordings
function recQuery(offset = 0, limit = PAGE) {
  // Only finished recordings are listed; running ones are shown on the status tab.
  const p = new URLSearchParams({ limit, offset, sort: $("#f-sort").value, status: "ready" });
  if ($("#f-stream").value) p.set("stream_id", $("#f-stream").value);
  if ($("#f-keep").checked) p.set("keep", "1");
  return "/recordings?" + p;
}

function hasFilters() {
  return !!($("#f-stream").value || $("#f-keep").checked);
}

async function loadRecordings(append = false, limit = PAGE) {
  if (!append) await guard(fetchStreams);
  const data = await guard(() => api(recQuery(append ? state.recs.length : 0, limit)));
  if (!data) return;
  state.recs = append ? state.recs.concat(data.items) : data.items;
  state.recTotal = data.total;
  // Selection covers visible rows only: filtering must not hide rows that would be deleted.
  const selectable = new Set(state.recs.filter(canSelect).map(r => r.id));
  state.selected = new Set([...state.selected].filter(id => selectable.has(id)));
  renderRecordings();
}

// Refresh: reload as many rows as are visible now, so the list and selection stay put
const refreshRecordings = () => loadRecordings(false, Math.min(500, Math.max(PAGE, state.recs.length)));

function canSelect(r) {
  return !r.keep;
}

function renderBulk() {
  const chosen = state.recs.filter(r => state.selected.has(r.id));
  const bytes = chosen.reduce((sum, r) => sum + (r.file_size || 0), 0);
  $("#rec-bulk").hidden = !chosen.length;
  $("#bulk-count").textContent = `${chosen.length} valittu${bytes ? ` · ${size(bytes)}` : ""}`;
  const selectable = state.recs.filter(canSelect);
  const all = $("#f-all");
  $("#select-all-wrap").hidden = !selectable.length;
  all.checked = selectable.length > 0 && chosen.length === selectable.length;
  all.indeterminate = chosen.length > 0 && chosen.length < selectable.length;
  $$("#rec-list .card[data-id]").forEach(c => c.classList.toggle("selected", state.selected.has(+c.dataset.id)));
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) return;
  if (!confirm(`Poistetaanko ${ids.length} tallennetta?\nÄänitiedostot poistetaan pysyvästi.`)) return;
  await guard(async () => {
    const res = await api("/recordings/delete", { method: "POST", body: { ids } });
    if (res.deleted.includes(state.playing)) closePlayer();
    state.selected.clear();
    const skipped = res.skipped.length ? `, ${res.skipped.length} ohitettu: ${res.skipped[0].reason}` : "";
    toast(`Poistettu ${res.deleted.length}${skipped}`, res.skipped.length > 0);
    await refreshRecordings();
  });
}

function renderRecordings() {
  const el = $("#rec-list");
  const filtered = hasFilters();
  $("#f-clear").hidden = !filtered;
  $("#rec-summary").textContent = state.recTotal ? `${state.recTotal} tallennetta` : "";
  $("#rec-more").hidden = state.recs.length >= state.recTotal;
  renderBulk();
  if (!state.recs.length) {
    el.innerHTML = `<div class="empty-state">${filtered ? "Valinnoilla ei löytynyt tallenteita." :
      "Ei vielä tallenteita. Lisää striimi ja aikataulu, tai aloita tallennus heti Striimit-välilehdeltä."}</div>`;
    return;
  }
  el.innerHTML = state.recs.map(r => {
    const playable = !!r.file_path;
    const saved = r.raw_duration_s && r.duration_s && r.raw_duration_s - r.duration_s > 5
      ? ` <span title="Alkuperäinen ${dur(r.raw_duration_s)}">(−${dur(r.raw_duration_s - r.duration_s)} hiljaisuutta)</span>` : "";
    const tags = r.tags ? r.tags.split(",").map(t => `<span class="tag">${esc(t)}</span>`).join(" ") : "";
    let actions = "";
    actions += `<button class="icon fav ${r.keep ? "on" : ""}" data-fav="${r.id}" title="${r.keep ? "Poista suosikeista" : "Merkitse suosikiksi ja säilytettäväksi"}">${r.keep ? "★" : "☆"}</button>`;
    if (playable) actions += `<a href="/media/${r.id}?download=1"><button class="icon" title="Lataa">⬇</button></a>`;
    actions += `<button class="icon" data-edit="${r.id}" title="Muokkaa">✎</button>`;
    actions += `<button class="icon danger" data-del="${r.id}" title="Poista">🗑</button>`;
    return `
    <div class="card ${state.playing === r.id ? "playing" : ""}" data-id="${r.id}">
      <input type="checkbox" class="sel" data-sel="${r.id}" aria-label="Valitse" ${state.selected.has(r.id) ? "checked" : ""}
        ${canSelect(r) ? "" : `disabled title="★-tallenne on merkitty säilytettäväksi"`}>
      <button class="play" data-play="${r.id}" ${playable ? "" : "disabled"} title="Toista">${state.playing === r.id ? "❚❚" : "▶"}</button>
      <div class="main">
        <div class="title">${esc(r.title)}</div>
        <div class="sub">
          <span>${esc(r.stream_name || "–")}</span><span>${fdate(r.started_at)}</span>
          <span>${dur(r.duration_s) + saved}</span>
          ${r.file_size ? `<span>${size(r.file_size)}</span>` : ""}
          ${tags}
        </div>
        ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ""}
      </div>
      <div class="actions">${actions}</div>
    </div>`;
  }).join("");
  $$("[data-sel]", el).forEach(c => c.onchange = () => {
    if (c.checked) state.selected.add(+c.dataset.sel); else state.selected.delete(+c.dataset.sel);
    renderBulk();
  });
  renderBulk();
  $$("[data-play]", el).forEach(b => b.onclick = () => play(state.recs.find(r => r.id == b.dataset.play)));
  $$("[data-edit]", el).forEach(b => b.onclick = () => editRecording(state.recs.find(r => r.id == b.dataset.edit)));
  $$("[data-del]", el).forEach(b => b.onclick = () => deleteRecording(state.recs.find(r => r.id == b.dataset.del)));
  $$("[data-fav]", el).forEach(b => b.onclick = () => toggleFavorite(state.recs.find(r => r.id == b.dataset.fav)));
}

async function toggleFavorite(r) {
  await guard(async () => {
    Object.assign(r, await api(`/recordings/${r.id}`, { method: "PUT", body: { keep: !r.keep } }));
    if (r.keep) state.selected.delete(r.id);
    if ($("#f-keep").checked) refreshRecordings(); else renderRecordings();
  });
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
  const warning = r.keep ? "\n\n★ Tämä on merkitty säilytettäväksi." : "";
  if (!confirm(`Poistetaanko tallenne "${r.title}"?\nÄänitiedosto poistetaan pysyvästi.${warning}`)) return;
  await guard(async () => {
    await api(`/recordings/${r.id}`, { method: "DELETE" });
    if (state.playing === r.id) closePlayer();
    state.selected.delete(r.id);
    toast("Poistettu"); refreshRecordings();
  });
}

async function stopRecording(id) {
  if (!confirm("Pysäytetäänkö tallennus nyt? Tähän asti tallennettu osa säilyy.")) return;
  await guard(async () => {
    await api(`/recordings/${id}/stop`, { method: "POST" });
    toast("Pysäytetään…");
  });
}

["#f-stream", "#f-keep"].forEach(s => { $(s).onchange = () => loadRecordings(); });
$("#f-sort").value = store("rec-sort") === "oldest" ? "oldest" : "newest";
$("#f-sort").onchange = e => { store("rec-sort", e.target.value); loadRecordings(); };
$("#f-clear").onclick = () => {
  $("#f-stream").value = "";
  $("#f-keep").checked = false;
  loadRecordings();
};
$("#f-all").onchange = e => {
  state.selected = new Set(e.target.checked ? state.recs.filter(canSelect).map(r => r.id) : []);
  renderRecordings();
};
$("#bulk-clear").onclick = () => { state.selected.clear(); renderRecordings(); };
$("#bulk-delete").onclick = deleteSelected;
$("#rec-more").onclick = () => loadRecordings(true);

// ------------------------------------------------------------ player
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

// ------------------------------------------------------------ status
async function loadStatus() {
  const s = await guard(() => api("/status"));
  if (!s) return;
  const pct = Math.round((s.disk.used / s.disk.total) * 100);
  const c = s.counts;
  $("#status-body").innerHTML = `
    ${s.disk.low ? `<p class="bad"><strong>Levytila on lopussa.</strong> Uudet tallennukset voivat jäädä tekemättä.</p>` : ""}
    <div class="grid">
      <div class="stat"><div class="l">Levytila vapaana</div><div class="v">${size(s.disk.free)}</div>
        <div class="bar"><div style="width:${pct}%"></div></div><div class="small muted" style="margin-top:6px">${pct} % käytössä · ${esc(s.disk.path)}</div></div>
      <div class="stat"><div class="l">Arkisto</div><div class="v">${c.ready || 0} tallennetta</div>
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
    <div class="section-h">Automaattisesti tallennettavat</div>
    <div class="list">${s.auto.length ? s.auto.map(a => `
      <div class="card"><div class="main"><div class="title">${esc(a.name)}</div>
      <div class="sub"><span class="${a.on_air ? "ok" : "muted"}">${a.on_air == null ? "Tarkistetaan…" : a.on_air ? (a.wait_offline ? "Lähetys käynnissä, odottaa seuraavaa lähetystä" : "Lähetys käynnissä") : "Ei lähetystä"}</span>
      <span>enintään ${dur(a.max_duration_min * 60)}</span></div></div></div>`).join("")
      : `<div class="empty-state">Yhtään striimiä ei tallenneta automaattisesti</div>`}</div>
    <div class="section-h">Seuraavat ajastetut</div>
    <div class="list">${s.upcoming.length ? s.upcoming.map(u => `
      <div class="card"><div class="main"><div class="title">${esc(u.title)}</div>
      <div class="sub"><span>${esc(u.stream_name)}</span><span>${fdate(u.start)}</span><span>${dur(u.duration_min * 60)}</span></div></div></div>`).join("")
      : `<div class="empty-state">Ei ajastettuja tallennuksia</div>`}</div>
    <p class="small muted about">${esc(s.name)} ${esc(s.version)} · <em>Catch the stream. Keep the moment.</em> · palvelimen aika ${fdate(s.now)}</p>`;
  $$("[data-stop]", $("#status-body")).forEach(b => b.onclick = () => stopRecording(+b.dataset.stop));
}

// ------------------------------------------------------------ theme
const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}
$("#theme").value = THEMES.includes(store("theme")) ? store("theme") : "system";
$("#theme").onchange = e => { store("theme", e.target.value); applyTheme(e.target.value); };

// ------------------------------------------------------------ live updates
// The server sends "change" over Server-Sent Events whenever data or recorder state
// changes; the visible tab is then reloaded. No page refresh is needed.
const TABS = ["recordings", "schedules", "streams", "status"];
const LOADERS = { recordings: () => refreshRecordings(), schedules: loadSchedules, streams: loadStreams, status: loadStatus };
const currentTab = () => TABS.find(t => !$("#tab-" + t).hidden) || "recordings";

async function refreshLive() {
  let s;
  try { s = await api("/status"); } catch { return; }
  const live = $("#live");
  live.hidden = !s.active.length;
  live.innerHTML = s.active.length ? `<span class="dot"></span> Tallentaa: ${s.active.map(a => esc(a.stream_name)).join(", ")}` : "";
}

function refreshAll() {
  refreshLive();
  LOADERS[currentTab()]();
}
const refreshSoon = debounce(refreshAll, 250);

function connectEvents() {
  if (!window.EventSource) { setInterval(refreshAll, 10000); return; }
  const es = new EventSource("/api/events");
  let connectedBefore = false;
  // "hello" arrives on every (re)connect: after a break, catch up on missed changes
  es.addEventListener("hello", () => {
    $("#offline").hidden = true;
    if (connectedBefore) refreshSoon();
    connectedBefore = true;
  });
  es.addEventListener("change", refreshSoon);
  es.onerror = () => { $("#offline").hidden = false; };
}

// Disk usage and clocks change without events: refresh the status tab now and then
setInterval(() => { if (!document.hidden && currentTab() === "status") loadStatus(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshSoon(); });

// ------------------------------------------------------------ startup
const tabFromHash = () => (TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "recordings");
window.addEventListener("hashchange", () => showTab(tabFromHash()));
showTab(tabFromHash());
refreshLive();
connectEvents();
