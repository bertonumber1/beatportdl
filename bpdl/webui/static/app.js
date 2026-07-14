"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  status: null,
  queue: [],
  wizardQueueIndex: null,
  wizardScan: null, // {genres, subgenres, artists} rank entries with .selected
  searchResults: [],
  explore: { section: "top100", page: 1, kind: "tracks", selected: new Map(), _rendered: [], loaded: false },
  activity: new Map(), // track id -> card element
  runCounts: { downloaded: 0, skipped: 0, failed: 0 },
  failedTracks: [],
  wizardLargeCatalogue: false,
};

// ---- tiny fetch helpers ----

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `request failed (${resp.status})`);
  }
  return data;
}

function initials(name) {
  return (name || "?").trim().charAt(0).toUpperCase();
}

// Track/release/artist names come from Beatport and routinely contain &, ", '
// or < (think `Rock & Roll (12" Mix)`) — interpolating them raw into innerHTML
// breaks title="..." attributes and card markup. Escape at every interpolation.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// ---- preview player (Beatport's public ~2-min samples; one at a time) ----

const preview = { audio: new Audio(), btn: null };
preview.audio.preload = "none";
preview.audio.addEventListener("ended", stopPreview);
preview.audio.addEventListener("error", stopPreview);
preview.audio.addEventListener("timeupdate", () => {
  if (!preview.btn || !preview.audio.duration) return;
  preview.btn.style.setProperty("--pv", `${(preview.audio.currentTime / preview.audio.duration) * 100}%`);
});

function stopPreview() {
  preview.audio.pause();
  if (preview.btn) {
    preview.btn.classList.remove("playing");
    preview.btn.textContent = "▶";
    preview.btn.style.removeProperty("--pv");
  }
  preview.btn = null;
}

function makePreviewBtn(url) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "preview-btn";
  b.title = "Play preview";
  b.textContent = "▶";
  b.addEventListener("click", (e) => {
    e.stopPropagation(); // don't toggle the card's selection
    if (preview.btn === b) { stopPreview(); return; }
    stopPreview();
    preview.audio.src = url;
    preview.audio.play().catch(() => stopPreview());
    preview.btn = b;
    b.classList.add("playing");
    b.textContent = "❚❚";
  });
  return b;
}

function hueFor(name) {
  let h = 0;
  for (const c of name || "") h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function artStyle(name, cover) {
  if (cover) return `background-image:url('${esc(cover)}')`;
  const h = hueFor(name);
  return `background:linear-gradient(135deg, hsl(${h},55%,32%), hsl(${(h + 40) % 360},55%,20%))`;
}

function showToast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (kind ? " " + kind : "");
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 3800);
}

// ---- top-level view state machine ----

function render() {
  const s = state.status;
  if (!s) return;

  $("#setup-panel").classList.toggle("hidden", s.configured);
  $("#connecting-panel").classList.toggle("hidden", !s.configured || s.login_status === "ok");
  $("#dashboard").classList.toggle("hidden", !(s.configured && s.login_status === "ok"));

  const pill = $("#conn-status");
  pill.className = "pill";
  if (!s.configured) {
    pill.classList.add("pending");
    pill.querySelector(".label").textContent = "Setup needed";
  } else if (s.login_status === "ok") {
    pill.classList.add("ok");
    pill.querySelector(".label").textContent = "Connected";
  } else if (s.login_status === "error") {
    pill.classList.add("error");
    pill.querySelector(".label").textContent = "Connection failed";
  } else {
    pill.classList.add("connecting");
    pill.querySelector(".label").textContent = "Connecting…";
  }

  if (s.configured && s.login_status !== "ok") {
    $("#connecting-spinner").classList.toggle("hidden", s.login_status === "error");
    $("#connecting-text").textContent =
      s.login_status === "error" ? `Couldn't connect: ${s.login_error}` : "Connecting to Beatport…";
    $("#retry-login-btn").classList.toggle("hidden", s.login_status !== "error");
  }

  renderQueue();
}

// ---- queue ----

function renderQueue() {
  const grid = $("#queue-grid");
  grid.innerHTML = "";
  $("#queue-count").textContent = state.queue.length;
  $("#queue-empty").classList.toggle("hidden", state.queue.length > 0);
  $("#start-btn").disabled = state.queue.length === 0 || state.status?.downloading;
  $("#start-btn").classList.toggle("hidden", !!state.status?.downloading);
  $("#stop-btn").classList.toggle("hidden", !state.status?.downloading);

  updateQueueSelCount();
  state.queue.forEach((item, idx) => {
    const card = document.createElement("div");
    card.className = "card queue-card";
    card.dataset.idx = idx;
    card.innerHTML = `
      <div class="card-art" style="${artStyle(item.name, item.cover)}">${item.cover ? "" : initials(item.name)}</div>
      <div class="card-body">
        <div class="card-badge">${item.type.replace(/s$/, "")}${item.filters === undefined ? "" : item.filters ? " · filtered" : item.needs_wizard ? " · needs filters" : " · unfiltered"}</div>
        <div class="card-name" title="${esc(item.name)}">${esc(item.name)}</div>
        <div class="card-subtitle" title="${esc(item.subtitle)}">${esc(item.subtitle)}</div>
      </div>
      ${(item.type === "labels" || item.type === "artists") ? `<button class="card-edit" title="${item.needs_wizard ? "Choose filters" : "Edit filters"}">&#9998;</button>` : ""}
      <button class="card-remove" title="Remove">&times;</button>
    `;
    card.querySelector(".card-remove").addEventListener("click", (e) => { e.stopPropagation(); removeQueueItem(idx); });
    const editBtn = card.querySelector(".card-edit");
    if (editBtn) editBtn.addEventListener("click", (e) => { e.stopPropagation(); openWizard(idx, item.url); });
    card.addEventListener("click", () => {
      card.classList.toggle("selected");
      updateQueueSelCount();
    });
    grid.appendChild(card);
  });
}

function updateQueueSelCount() {
  const n = document.querySelectorAll("#queue-grid .queue-card.selected").length;
  const btn = $("#remove-selected-btn");
  btn.classList.toggle("hidden", n === 0);
  btn.textContent = `Remove ${n} selected`;
}

async function removeQueueItem(idx) {
  try {
    const data = await api("DELETE", `/api/queue/${idx}`);
    state.queue = data.queue;
    renderQueue();
  } catch (e) {
    showToast(`Failed to remove: ${e.message}`, "error");
  }
}

async function removeSelectedQueueItems() {
  const idxs = Array.from(document.querySelectorAll("#queue-grid .queue-card.selected"))
    .map((c) => Number(c.dataset.idx))
    .sort((a, b) => b - a); // delete from the end so earlier indexes stay valid
  if (!idxs.length) return;
  $("#remove-selected-btn").disabled = true;
  try {
    let data = null;
    for (const idx of idxs) {
      data = await api("DELETE", `/api/queue/${idx}`);
    }
    if (data) state.queue = data.queue;
    showToast(`Removed ${idxs.length} item(s) from the queue.`, "success");
  } catch (e) {
    showToast(`Failed to remove: ${e.message}`, "error");
    try { state.queue = (await api("GET", "/api/status")).queue || []; } catch (_) {}
  }
  $("#remove-selected-btn").disabled = false;
  renderQueue();
}

// ---- adding items ----

async function handleAdd() {
  const input = $("#url-input");
  const raw = input.value.trim();
  if (!raw) return;
  $("#input-status").textContent = "Working…";
  $("#input-status").classList.remove("err");
  try {
    const isUrl = raw.startsWith("https://www.beatport.com") || raw.startsWith("https://www.beatsource.com");
    const data = await api("POST", "/api/queue", { input: raw });
    if (isUrl) {
      const item = data.added;
      state.queue.push(item);
      renderQueue();
      input.value = "";
      $("#input-status").textContent = `Added "${item.name}".`;
      if (item.needs_wizard) openWizard(state.queue.length - 1, item.url);
    } else {
      state.searchResults = data.search_results || [];
      openSearchModal();
      $("#input-status").textContent = "";
    }
  } catch (e) {
    $("#input-status").textContent = e.message;
    $("#input-status").classList.add("err");
  }
}

// ---- search modal ----

function openSearchModal() {
  const grid = $("#search-results-grid");
  grid.innerHTML = "";
  if (!state.searchResults.length) {
    grid.innerHTML = '<p class="muted small">No results found.</p>';
  }
  state.searchResults.forEach((r, i) => {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.idx = i;
    card.innerHTML = `
      <div class="card-art" style="${artStyle(r.name, r.cover)}">${r.cover ? "" : initials(r.name)}</div>
      <div class="card-body">
        <div class="card-badge">${esc(r.kind)}</div>
        <div class="card-name" title="${esc(r.name)}">${esc(r.name)}</div>
        <div class="card-subtitle" title="${esc(r.subtitle)}">${(r.artists && r.artists.length) ? artistLinksHtml({ artists: r.artists, label: r.label, artist: r.subtitle }) : esc(r.subtitle)}</div>
      </div>
    `;
    if (r.preview) card.appendChild(makePreviewBtn(r.preview));
    wireCatalogueLinks(card);
    card.addEventListener("click", () => card.classList.toggle("selected"));
    grid.appendChild(card);
  });
  $("#search-modal").classList.remove("hidden");
}

async function addSelectedSearchResults() {
  const selected = $$("#search-results-grid .card.selected").map((c) => state.searchResults[Number(c.dataset.idx)]);
  let firstWizard = null; // first added label/artist still needing the filter wizard
  for (const r of selected) {
    try {
      const data = await api("POST", "/api/queue", { input: r.url });
      const item = data.added;
      state.queue.push(item);
      if (item.needs_wizard && firstWizard === null) firstWizard = { idx: state.queue.length - 1, url: item.url };
    } catch (e) {
      showToast(`Failed to add "${r.name}": ${e.message}`, "error");
    }
  }
  renderQueue();
  $("#search-modal").classList.add("hidden");
  if (selected.length) showToast(`Added ${selected.length} item(s) to queue.`, "success");
  if (firstWizard) openWizard(firstWizard.idx, firstWizard.url);
}

// ---- wizard ----

const LARGE_CATALOGUE_THRESHOLD = 150;

function openWizard(queueIndex, url) {
  state.wizardQueueIndex = queueIndex;
  state.wizardScan = null;
  const item = state.queue[queueIndex];
  $("#wizard-title").textContent = "What do you want to queue?";
  $("#wizard-scope-name").textContent = `"${item ? item.name : url}" — browse and pick individual releases, filter the catalogue, or queue the whole thing.`;
  $("#wizard-scope").classList.remove("hidden");
  $("#wizard-scanning").classList.add("hidden");
  $("#wizard-results").classList.add("hidden");
  $("#wizard-browse").classList.add("hidden");
  $("#wizard-filter").classList.add("hidden");
  $("#wizard-modal").classList.remove("hidden");
  $("#wizard-modal").dataset.url = url;

  $("#wizard-scope-size").textContent = "Checking size…";
  $("#wizard-scope-warning").classList.add("hidden");
  $("#wizard-scope-confirm-row").classList.add("hidden");
  $("#wizard-scope-confirm-checkbox").checked = false;
  $("#wizard-scope-all-btn").disabled = false;
  state.wizardLargeCatalogue = false;

  api("POST", "/api/peek", { url })
    .then((data) => {
      if (data.count == null) {
        $("#wizard-scope-size").textContent = "";
        return;
      }
      $("#wizard-scope-size").textContent = `${data.count} ${data.kind} in this catalogue.`;
      if (data.count > LARGE_CATALOGUE_THRESHOLD) {
        state.wizardLargeCatalogue = true;
        $("#wizard-scope-warning").textContent =
          `That's a large catalogue (${data.count} ${data.kind}) — queuing everything unfiltered will use significant time, bandwidth, and storage.`;
        $("#wizard-scope-warning").classList.remove("hidden");
        $("#wizard-scope-confirm-row").classList.remove("hidden");
        $("#wizard-scope-all-btn").disabled = true;
      }
    })
    .catch(() => {
      $("#wizard-scope-size").textContent = "";
    });
}

function startWizardScan(url) {
  $("#wizard-title").textContent = "Scanning…";
  $("#wizard-scope").classList.add("hidden");
  $("#wizard-scanning").classList.remove("hidden");
  $("#wizard-results").classList.add("hidden");
  $("#wizard-scan-status").textContent = "Starting scan — this can take a while for large catalogues…";
  api("POST", "/api/scan", { url }).catch((e) => {
    $("#wizard-scan-status").textContent = `Scan failed: ${e.message}`;
  });
}

function chipList(container, entries, selectedSet) {
  container.innerHTML = "";
  const max = entries.length ? entries[0].count : 1;
  entries.forEach((e) => {
    const chip = document.createElement("div");
    chip.className = "chip" + (selectedSet.has(e.name) ? " selected" : "");
    chip.innerHTML = `<span>${esc(e.name)}</span><span class="chip-bar"><span class="chip-bar-fill" style="width:${Math.max(6, (e.count / max) * 100)}%"></span></span><span class="chip-count">${e.count}</span>`;
    chip.addEventListener("click", () => {
      chip.classList.toggle("selected");
      if (selectedSet.has(e.name)) selectedSet.delete(e.name);
      else selectedSet.add(e.name);
    });
    container.appendChild(chip);
  });
}

// ---- stats charts (single measure everywhere -> one hue, values labeled) ----

function formatNum(n) {
  return Number(n).toLocaleString("en-GB");
}

// Shared floating tooltip for column charts (hbar rows label every value inline,
// so only columns need it).
function chartTip() {
  let tip = document.getElementById("chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "chart-tip";
    document.body.appendChild(tip);
  }
  return tip;
}

// Ranked horizontal bars: name | bar | value-at-tip. All one hue — the length
// carries the magnitude, so coloring by value would just re-encode it.
function renderHBars(container, entries, labelKey, valueKey, formatValue, limit = 12) {
  container.innerHTML = "";
  if (!entries || !entries.length) {
    container.innerHTML = '<p class="muted small">Nothing yet.</p>';
    return;
  }
  const rows = entries.slice(0, limit);
  const max = Math.max(...rows.map((e) => e[valueKey])) || 1;
  rows.forEach((e) => {
    const value = formatValue ? formatValue(e[valueKey]) : formatNum(e[valueKey]);
    const row = document.createElement("div");
    row.className = "hbar-row";
    row.innerHTML = `
      <span class="hbar-name" title="${esc(e[labelKey])}">${esc(e[labelKey])}</span>
      <span class="hbar-track"><span class="hbar-fill" style="width:${Math.max(1.5, (e[valueKey] / max) * 100)}%"></span></span>
      <span class="hbar-val">${esc(value)}</span>`;
    container.appendChild(row);
  });
}

// Column chart for ordered distributions (BPM buckets, keys, months).
// Peak column gets an inline value; the tooltip carries the rest.
function renderColumns(container, entries, labelKey, valueKey, formatValue, labelEvery = 1, tickFmt = null) {
  container.innerHTML = "";
  if (!entries || !entries.length) {
    container.innerHTML = '<p class="muted small">Nothing yet.</p>';
    return;
  }
  const max = Math.max(...entries.map((e) => e[valueKey])) || 1;
  const peak = entries.findIndex((e) => e[valueKey] === max);
  const tip = chartTip();
  entries.forEach((e, i) => {
    const value = formatValue ? formatValue(e[valueKey]) : formatNum(e[valueKey]);
    const cell = document.createElement("div");
    cell.className = "col-cell";
    const capLabel = i === peak ? `<span class="col-peak">${esc(value)}</span>` : "";
    cell.innerHTML = `
      <span class="col-slot">${capLabel}<span class="col-fill" style="height:${Math.max(1.5, (e[valueKey] / max) * 100)}%"></span></span>
      <span class="col-tick">${i % labelEvery === 0 ? esc(tickFmt ? tickFmt(e[labelKey]) : e[labelKey]) : ""}</span>`;
    cell.addEventListener("mousemove", (ev) => {
      tip.textContent = `${e[labelKey]} — ${value}`;
      tip.style.display = "block";
      tip.style.left = `${ev.clientX + 12}px`;
      tip.style.top = `${ev.clientY - 28}px`;
    });
    cell.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    container.appendChild(cell);
  });
}

function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

async function openStatsModal(days) {
  $("#stats-modal").classList.remove("hidden");
  let stats;
  try {
    stats = await api("GET", "/api/stats" + (days ? `?days=${days}` : ""));
  } catch (e) {
    $("#stats-tiles").innerHTML = `<p class="muted small">Failed to load stats: ${esc(e.message)}</p>`;
    return;
  }

  const sc = stats.status_counts || {};
  const attempts = (sc.downloaded || 0) + (sc.failed || 0);
  const successRate = attempts ? ((sc.downloaded || 0) / attempts) * 100 : null;
  const tiles = $("#stats-tiles");
  tiles.innerHTML = `
    <div class="stats-tile"><div class="stats-tile-value">${formatNum(stats.total_tracks)}</div><div class="stats-tile-label">Tracks</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatNum(stats.total_releases)}</div><div class="stats-tile-label">Releases</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatNum(stats.total_labels)}</div><div class="stats-tile-label">Labels</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatNum(stats.total_artists)}</div><div class="stats-tile-label">Artists</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatBytes(stats.total_bytes)}</div><div class="stats-tile-label">Downloaded</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${successRate === null ? "—" : successRate.toFixed(1) + "%"}</div><div class="stats-tile-label">Success rate</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatNum(sc.failed || 0)}</div><div class="stats-tile-label">Failed</div></div>
  `;

  renderHBars($("#stats-genres"), stats.genres, "name", "count");
  renderHBars($("#stats-artists"), stats.artists, "name", "count");
  renderHBars($("#stats-labels"), stats.labels, "name", "count");
  renderHBars($("#stats-subgenres"), stats.subgenres, "name", "count");
  renderHBars($("#stats-failures"), stats.failure_reasons, "name", "count", null, 8);
  const shortMonth = (m) => `${m.slice(5)}/${m.slice(2, 4)}`; // 2026-07 -> 07/26
  renderColumns($("#stats-daily"), (stats.activity_by_day || []).slice(-30), "day", "count", null, 2, (d) => d.slice(8));
  renderColumns($("#stats-bpm"), stats.bpm_buckets, "range", "count", null, 2, (r) => r.split("-")[0]);
  renderColumns($("#stats-keys"), stats.keys, "name", "count");
  renderColumns($("#stats-activity"), (stats.activity_by_month || []).slice(-24), "month", "count", null, 1, shortMonth);
  renderColumns($("#stats-volume"), (stats.bytes_by_month || []).slice(-24), "month", "bytes", formatBytes, 1, shortMonth);
}

function renderWizardResults(payload) {
  state.wizardScan = {
    genres: payload.genres,
    subgenres: payload.subgenres,
    artists: payload.artists,
    selectedGenres: new Set(),
    selectedSubgenres: new Set(),
    selectedArtists: new Set(),
  };
  $("#wizard-title").textContent = "Filter this catalogue";
  $("#wizard-scanning").classList.add("hidden");
  $("#wizard-results").classList.remove("hidden");
  const bpm = payload.bpm_max ? ` · BPM ${payload.bpm_min}–${payload.bpm_max}` : "";
  $("#wizard-summary").textContent = `${payload.total} tracks scanned${bpm}. Select genres/subgenres/artists to keep — leave empty for "all".`;
  chipList($("#wizard-genres"), payload.genres, state.wizardScan.selectedGenres);
  chipList($("#wizard-subgenres"), payload.subgenres, state.wizardScan.selectedSubgenres);
  chipList($("#wizard-artists"), payload.artists, state.wizardScan.selectedArtists);
}

async function confirmWizard(bypass) {
  const idx = state.wizardQueueIndex;
  if (idx === null) return;
  const payload = bypass
    ? { bypass: true }
    : {
        bypass: false,
        genres: Array.from(state.wizardScan.selectedGenres),
        subgenres: Array.from(state.wizardScan.selectedSubgenres),
        artists: Array.from(state.wizardScan.selectedArtists),
        date_from: $("#wizard-date-from").value,
        date_to: $("#wizard-date-to").value,
      };
  const data = await api("POST", `/api/queue/${idx}/filters`, payload);
  state.queue[idx] = data.item;
  renderQueue();
  $("#wizard-modal").classList.add("hidden");
}

// ---- browse & pick individual releases ----

function startWizardBrowse(url) {
  state.browse = { url, page: 1, selected: new Map() };
  $("#wizard-title").textContent = "Browse & pick releases";
  $("#wizard-scope").classList.add("hidden");
  $("#wizard-browse").classList.remove("hidden");
  loadBrowsePage(1);
}

async function loadBrowsePage(page) {
  const grid = $("#wizard-browse-grid");
  $("#wizard-browse-status").textContent = "Loading…";
  grid.innerHTML = "";
  try {
    const data = await api("POST", "/api/browse", { url: state.browse.url, page });
    state.browse.page = data.page;
    $("#wizard-browse-status").textContent =
      `${data.count} ${data.kind} in this catalogue — tap to select the ones you want.`;
    $("#wizard-browse-page").textContent = `Page ${data.page}`;
    $("#wizard-browse-prev").disabled = !data.has_prev;
    $("#wizard-browse-next").disabled = !data.has_next;
    data.items.forEach((it) => {
      const sel = state.browse.selected.has(it.url);
      const card = document.createElement("div");
      card.className = "browse-card" + (sel ? " selected" : "");
      const meta = [it.catno, it.year, it.track_count ? `${it.track_count} trk` : ""]
        .filter(Boolean).join(" · ");
      card.innerHTML = `
        ${it.cover ? `<img class="browse-cover" src="${esc(it.cover)}" loading="lazy">`
                   : `<div class="browse-cover placeholder"></div>`}
        <div class="browse-info">
          <div class="browse-title">${esc(it.name)}</div>
          <div class="browse-artist muted small">${esc(it.artist)}</div>
          <div class="browse-meta muted small">${esc(meta)}</div>
        </div>
        <div class="browse-check">✓</div>`;
      card.addEventListener("click", () => {
        if (state.browse.selected.has(it.url)) state.browse.selected.delete(it.url);
        else state.browse.selected.set(it.url, it.name);
        card.classList.toggle("selected");
        updateBrowseSelCount();
      });
      grid.appendChild(card);
    });
    updateBrowseSelCount();
  } catch (e) {
    $("#wizard-browse-status").textContent = `Failed to load: ${e.message}`;
  }
}

function updateBrowseSelCount() {
  const n = state.browse.selected.size;
  $("#wizard-browse-selcount").textContent = `${n} selected`;
  $("#wizard-browse-add").disabled = n === 0;
  $("#wizard-browse-add").textContent = n ? `Add ${n} to queue` : "Add selected to queue";
}

async function addBrowseSelected() {
  const picks = Array.from(state.browse.selected.entries());
  if (!picks.length) return;
  $("#wizard-browse-add").disabled = true;
  let added = 0;
  for (const [url] of picks) {
    try {
      const data = await api("POST", "/api/queue", { input: url });
      if (data.added) { state.queue.push(data.added); added++; }
    } catch (e) {
      showToast(`Failed to add one release: ${e.message}`, "error");
    }
  }
  // drop the original label/artist item — we cherry-picked instead of queuing it whole
  const idx = state.wizardQueueIndex;
  if (idx !== null && state.queue[idx] && state.queue[idx].needs_wizard) {
    try {
      const data = await api("DELETE", `/api/queue/${idx}`);
      state.queue = data.queue;
    } catch (_) { /* leave it; not fatal */ }
  }
  renderQueue();
  $("#wizard-modal").classList.add("hidden");
  showToast(`Added ${added} release(s) to the queue.`, "success");
}

// ---- faceted filter (BPM / genre / sub-genre / artists), Beatport-style ----

async function startWizardFilter(url) {
  state.filter = { url, page: 1, selected: new Map(), selectedArtists: new Set() };
  $("#wizard-title").textContent = "Filter by BPM / genre / artist";
  $("#wizard-scope").classList.add("hidden");
  $("#wizard-filter").classList.remove("hidden");
  $("#filter-artists-wrap").classList.add("hidden");
  $("#filter-grid").innerHTML = "";
  $("#filter-status").textContent = "Set BPM and/or genre, then Apply.";
  $("#filter-bpm-min").value = "";
  $("#filter-bpm-max").value = "";
  $("#filter-subgenre").innerHTML = '<option value="">Any sub-genre</option>';
  updateFilterSelCount();
  // populate genres once
  const gsel = $("#filter-genre");
  if (gsel.dataset.loaded !== "1") {
    try {
      const data = await api("GET", "/api/genres");
      data.genres.forEach((g) => {
        const o = document.createElement("option");
        o.value = g.id; o.textContent = g.name;
        gsel.appendChild(o);
      });
      gsel.dataset.loaded = "1";
    } catch (e) { /* leave "Any genre" only */ }
  }
  gsel.value = "";
}

async function onGenreChange() {
  const gid = $("#filter-genre").value;
  const ssel = $("#filter-subgenre");
  ssel.innerHTML = '<option value="">Any sub-genre</option>';
  if (!gid) return;
  try {
    const data = await api("GET", `/api/subgenres/${gid}`);
    data.subgenres.forEach((s) => {
      const o = document.createElement("option");
      o.value = s.id; o.textContent = s.name;
      ssel.appendChild(o);
    });
  } catch (e) { /* leave "Any" */ }
}

function filterPayload(page, wantFacet) {
  const f = state.filter;
  const bmin = parseInt($("#filter-bpm-min").value, 10);
  const bmax = parseInt($("#filter-bpm-max").value, 10);
  return {
    url: f.url,
    genre_id: parseInt($("#filter-genre").value, 10) || null,
    sub_genre_id: parseInt($("#filter-subgenre").value, 10) || null,
    bpm_min: Number.isFinite(bmin) ? bmin : null,
    bpm_max: Number.isFinite(bmax) ? bmax : null,
    artist_ids: Array.from(f.selectedArtists),
    page,
    want_facet: wantFacet,
  };
}

async function applyFilter(page = 1, wantFacet = true) {
  const f = state.filter;
  $("#filter-status").textContent = "Filtering…";
  $("#filter-grid").innerHTML = "";
  try {
    const data = await api("POST", "/api/filter", filterPayload(page, wantFacet));
    f.page = data.page;
    $("#filter-status").textContent = `${data.count} matching track(s).` +
      (data.count > 100 ? " Showing 100 per page." : "");
    $("#filter-page").textContent = `Page ${data.page}`;
    $("#filter-prev").disabled = !data.has_prev;
    $("#filter-next").disabled = !data.has_next;
    $("#filter-selectall").checked = false;
    if (data.artists) renderFilterArtists(data.artists);
    renderFilterGrid(data.tracks);
  } catch (e) {
    $("#filter-status").textContent = `Filter failed: ${e.message}`;
  }
}

function renderFilterArtists(artists) {
  const wrap = $("#filter-artists-wrap");
  const box = $("#filter-artists");
  box.innerHTML = "";
  if (!artists.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  artists.forEach((a) => {
    const chip = document.createElement("div");
    chip.className = "chip" + (state.filter.selectedArtists.has(a.id) ? " selected" : "");
    chip.innerHTML = `<span>${esc(a.name)}</span><span class="chip-count">${a.count}</span>`;
    chip.addEventListener("click", () => {
      if (state.filter.selectedArtists.has(a.id)) state.filter.selectedArtists.delete(a.id);
      else state.filter.selectedArtists.add(a.id);
      chip.classList.toggle("selected");
      applyFilter(1, false); // re-filter server-side by artist, keep the facet as-is
    });
    box.appendChild(chip);
  });
}

function renderFilterGrid(tracks) {
  const grid = $("#filter-grid");
  grid.innerHTML = "";
  state.filter._rendered = tracks;
  tracks.forEach((t) => {
    const sel = state.filter.selected.has(t.url);
    const card = document.createElement("div");
    card.className = "browse-card" + (sel ? " selected" : "");
    const meta = [t.bpm ? `${t.bpm} BPM` : "", t.key, t.genre, t.length, t.year].filter(Boolean).join(" · ");
    card.innerHTML = `
      ${t.cover ? `<img class="browse-cover" src="${esc(t.cover)}" loading="lazy">`
                : `<div class="browse-cover placeholder"></div>`}
      <div class="browse-info">
        <div class="browse-title">${esc(t.name)}</div>
        <div class="browse-artist muted small">${artistLinksHtml(t)}</div>
        <div class="browse-meta muted small">${esc(meta)}</div>
      </div>
      <div class="browse-check">✓</div>`;
    if (t.preview) card.appendChild(makePreviewBtn(t.preview));
    wireCatalogueLinks(card);
    card.addEventListener("click", () => {
      if (state.filter.selected.has(t.url)) state.filter.selected.delete(t.url);
      else state.filter.selected.set(t.url, t.name);
      card.classList.toggle("selected");
      updateFilterSelCount();
    });
    grid.appendChild(card);
  });
}

function updateFilterSelCount() {
  const n = state.filter.selected.size;
  $("#filter-selcount").textContent = `${n} selected`;
  $("#filter-add").disabled = n === 0;
  $("#filter-add").textContent = n ? `Add ${n} to queue` : "Add selected to queue";
}

function toggleSelectPage(checked) {
  const tracks = state.filter._rendered || [];
  const cards = $("#filter-grid").querySelectorAll(".browse-card");
  tracks.forEach((t, i) => {
    if (checked) state.filter.selected.set(t.url, t.name);
    else state.filter.selected.delete(t.url);
    if (cards[i]) cards[i].classList.toggle("selected", checked);
  });
  updateFilterSelCount();
}

async function addFilterSelected() {
  const picks = Array.from(state.filter.selected.entries());
  if (!picks.length) return;
  $("#filter-add").disabled = true;
  let added = 0;
  for (const [url] of picks) {
    try {
      const data = await api("POST", "/api/queue", { input: url });
      if (data.added) { state.queue.push(data.added); added++; }
    } catch (e) { showToast(`Failed to add a track: ${e.message}`, "error"); }
  }
  const idx = state.wizardQueueIndex;
  if (idx !== null && state.queue[idx] && state.queue[idx].needs_wizard) {
    try { const d = await api("DELETE", `/api/queue/${idx}`); state.queue = d.queue; } catch (_) {}
  }
  renderQueue();
  $("#wizard-modal").classList.add("hidden");
  showToast(`Added ${added} track(s) to the queue.`, "success");
}

// ---- activity / downloading ----

function ensureActivityCard(id, name, artists, release, cover) {
  if (state.activity.has(id)) return state.activity.get(id);
  const card = document.createElement("div");
  card.className = "card activity-card";
  card.innerHTML = `
    <div class="card-top">
      <div class="card-art" style="${artStyle(name, cover)}">${cover ? "" : initials(name)}</div>
      <div class="card-body">
        <div class="card-name" title="${esc(name)}">${esc(name)}</div>
        <div class="card-subtitle" title="${esc(artists)}">${esc(artists)}${release ? " — " + esc(release) : ""}</div>
      </div>
      <div class="status-icon"><span class="spinner"></span></div>
    </div>
    <div class="progress-track"><div class="progress-fill indeterminate"></div></div>
  `;
  $("#activity-grid").prepend(card);
  state.activity.set(id, card);
  return card;
}

function setStatusIcon(card, kind) {
  const el = card.querySelector(".status-icon");
  if (kind === "done") el.innerHTML = '<span class="check-icon">&#10003;</span>';
  else if (kind === "error") el.innerHTML = '<span class="cross-icon">&#10007;</span>';
  else if (kind === "skipped") el.innerHTML = '<span class="skip-icon">&#8213;</span>';
}

function updateRunStatsBar() {
  const c = state.runCounts;
  const total = c.downloaded + c.skipped + c.failed || 1;
  $("#stat-downloaded").textContent = c.downloaded;
  $("#stat-skipped").textContent = c.skipped;
  $("#stat-failed").textContent = c.failed;
  $("#stats-bar-ok").style.width = `${(c.downloaded / total) * 100}%`;
  $("#stats-bar-warn").style.width = `${(c.skipped / total) * 100}%`;
  $("#stats-bar-err").style.width = `${(c.failed / total) * 100}%`;
}

function handleTrackEvent(ev) {
  $("#activity-section").classList.remove("hidden");
  if (ev.type === "track_start") {
    ensureActivityCard(ev.id, ev.name + (ev.mix_name ? ` (${ev.mix_name})` : ""), (ev.artists || []).join(", "), ev.release, ev.cover);
    return;
  }
  // The card may already be gone (faded out + removed from the map) by the time
  // a terminal event arrives for it — that must never drop the stat itself, only
  // the now-pointless DOM update. Losing the count here is exactly how a client
  // can under/mis-report totals that don't match the server's real numbers.
  const card = state.activity.get(ev.id);

  if (ev.type === "track_progress") {
    if (!card) return;
    const fill = card.querySelector(".progress-fill");
    if (ev.total > 0) {
      fill.classList.remove("indeterminate");
      fill.style.width = `${Math.min(100, (ev.downloaded / ev.total) * 100)}%`;
    }
  } else if (ev.type === "track_done") {
    state.runCounts.downloaded++;
    updateRunStatsBar();
    if (card) {
      card.classList.add("done");
      setStatusIcon(card, "done");
      fadeOutCard(ev.id, card);
    }
  } else if (ev.type === "track_skipped") {
    state.runCounts.skipped++;
    updateRunStatsBar();
    if (card) {
      card.classList.add("skipped");
      setStatusIcon(card, "skipped");
      fadeOutCard(ev.id, card);
    }
  } else if (ev.type === "track_error") {
    state.runCounts.failed++;
    updateRunStatsBar();
    if (card) {
      card.classList.add("error");
      setStatusIcon(card, "error");
      fadeOutCard(ev.id, card);
    }
  }
}

function fadeOutCard(id, card) {
  setTimeout(() => {
    card.classList.add("fading");
    setTimeout(() => {
      card.remove();
      state.activity.delete(id);
    }, 550);
  }, 2200);
}

// ---- SSE ----

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    let ev;
    try {
      ev = JSON.parse(msg.data);
    } catch {
      return;
    }
    handleEvent(ev);
  };
  es.onerror = () => {
    // EventSource auto-reconnects; nothing to do.
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "login_status":
      if (state.status) {
        state.status.login_status = ev.status;
        state.status.login_error = ev.error || "";
      }
      render();
      break;
    case "queue_updated":
      state.queue = ev.queue;
      renderQueue();
      break;
    case "scan_status":
      $("#wizard-scan-status").textContent = ev.message;
      break;
    case "scan_error":
      $("#wizard-scan-status").textContent = `Scan failed: ${ev.error}`;
      break;
    case "scan_done":
      renderWizardResults(ev);
      break;
    case "batch_start":
      state.runCounts = { downloaded: 0, skipped: 0, failed: 0 };
      updateRunStatsBar();
      $("#activity-section").classList.remove("hidden");
      $("#activity-grid").innerHTML = "";
      state.activity.clear();
      if (state.status) state.status.downloading = true;
      renderQueue();
      break;
    case "item_start":
      showToast(`Starting "${ev.name}"…`);
      break;
    case "track_start":
    case "track_progress":
    case "track_done":
    case "track_skipped":
    case "track_error":
      handleTrackEvent(ev);
      break;
    case "batch_done":
      if (state.status) state.status.downloading = false;
      refreshStatus();
      if (ev.stopped) {
        showToast(`Stopped — ${ev.downloaded} downloaded, ${ev.skipped} skipped before stopping.`);
      } else {
        showToast(`Finished — ${ev.downloaded} downloaded, ${ev.skipped} skipped, ${ev.failed} failed.`, ev.failed ? "error" : "success");
      }
      state.failedTracks = ev.failed_tracks || [];
      $("#retry-failed-btn").classList.toggle("hidden", state.failedTracks.length === 0);
      $("#retry-failed-btn").textContent = `Retry ${state.failedTracks.length} failed`;
      break;
    case "settings_saved":
      break;
    case "art_recheck_status":
      $("#art-recheck-status").textContent = ev.message;
      break;
    case "art_recheck_error":
      $("#art-recheck-status").textContent = `Failed: ${ev.error}`;
      showToast(`Art recheck failed: ${ev.error}`, "error");
      break;
    case "art_recheck_done":
      $("#art-recheck-status").textContent =
        `Done — ${ev.files_fixed} file(s) fixed across ${ev.releases_fixed}/${ev.releases_checked} release(s). ` +
        `${ev.already_ok} already had art, ${ev.no_id_tag} file(s) predate ID tagging, ${ev.failed} failed.`;
      showToast(`Art recheck complete — ${ev.files_fixed} file(s) fixed.`, ev.failed ? "error" : "success");
      break;
    case "watch_check_start":
      $("#watch-status").textContent = `Checking ${ev.count} watched item(s)...`;
      break;
    case "watch_check_status":
      $("#watch-status").textContent = ev.message;
      break;
    case "watch_check_error":
      $("#watch-status").textContent = `Failed: ${ev.error}`;
      break;
    case "watch_check_done": {
      const pendingNote = ev.newly_pending ? ` ${ev.newly_pending} pre-release(s) spotted, will download once released.` : "";
      if (ev.new_releases > 0) {
        $("#watch-status").textContent = `Found ${ev.new_releases} new release(s), ${ev.new_tracks} track(s) downloaded.${pendingNote}`;
        showToast(`Watch check: ${ev.new_releases} new release(s) downloaded.`, "success");
      } else {
        $("#watch-status").textContent = `No new releases found.${pendingNote}`;
      }
      refreshWatchList();
      break;
    }
  }
}

// ---- settings ----

function fillForm(form, data) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in data)) continue;
    if (el.type === "checkbox") el.checked = !!data[el.name];
    else el.value = data[el.name];
  }
}

function formToPayload(form) {
  const payload = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") payload[el.name] = el.checked;
    else if (el.type === "number") payload[el.name] = el.value === "" ? null : Number(el.value);
    else payload[el.name] = el.value;
  }
  return payload;
}

async function openSettingsModal() {
  const data = await api("GET", "/api/settings");
  fillForm($("#settings-form"), data);
  $("#settings-modal").classList.remove("hidden");
}

async function refreshWatchList() {
  const data = await api("GET", "/api/watch");
  renderWatchList(data.watched_labels || [], data.watched_artists || []);
}

function renderWatchList(labels, artists) {
  const el = $("#watch-list");
  el.innerHTML = "";
  $("#watch-count").textContent = labels.length + artists.length;
  if (!labels.length && !artists.length) {
    el.innerHTML = '<p class="muted small">Not watching any labels or artists yet — paste one above and every new release lands automatically.</p>';
    return;
  }
  renderWatchSection(el, "Labels", "label", labels);
  renderWatchSection(el, "Artists", "artist", artists);
}

function renderWatchSection(el, heading, kind, entries) {
  if (!entries.length) return;
  const head = document.createElement("p");
  head.className = "muted small";
  head.style.cssText = "margin:4px 0 2px;font-weight:600;letter-spacing:.3px;";
  head.textContent = heading;
  el.appendChild(head);
  entries.forEach((w, idx) => {
    const row = document.createElement("div");
    row.className = "chip";
    row.style.cssText = "cursor:default;flex-direction:column;align-items:stretch;gap:4px;position:relative;";
    const pending = w.pending_releases || [];
    const noun = kind === "artist" ? "track" : "pre-release";
    const pendingText = pending.length
      ? `${pending.length} upcoming ${noun}${pending.length > 1 ? "s" : ""}: ` +
        pending.map((p) => `${p.release_name} (${p.expected_date})`).join(", ")
      : "";
    row.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;">
        <span style="flex:1;">${esc(w.name)}</span>
      </div>
      ${pendingText ? `<span class="muted" style="font-size:11px;">${esc(pendingText)}</span>` : ""}
    `;
    const removeBtn = document.createElement("button");
    removeBtn.className = "card-remove";
    removeBtn.style.cssText = "position:absolute;top:4px;right:4px;";
    removeBtn.innerHTML = "&times;";
    removeBtn.addEventListener("click", async () => {
      const data = await api("DELETE", `/api/watch/${kind}/${idx}`);
      renderWatchList(data.watched_labels || [], data.watched_artists || []);
    });
    row.appendChild(removeBtn);
    el.appendChild(row);
  });
}

async function saveSettingsModal() {
  try {
    await api("POST", "/api/settings", formToPayload($("#settings-form")));
    $("#settings-modal").classList.add("hidden");
    showToast("Settings saved.", "success");
    await refreshStatus();
  } catch (e) {
    showToast(`Failed to save: ${e.message}`, "error");
  }
}

async function submitSetupForm(e) {
  e.preventDefault();
  $("#setup-error").classList.add("hidden");
  try {
    await api("POST", "/api/settings", formToPayload($("#setup-form")));
    await refreshStatus();
  } catch (err) {
    $("#setup-error").textContent = err.message;
    $("#setup-error").classList.remove("hidden");
  }
}

// ---- bootstrap ----

async function refreshStatus() {
  state.status = await api("GET", "/api/status");
  state.queue = state.status.queue || [];
  render();
}

function wireEvents() {
  $("#add-btn").addEventListener("click", handleAdd);
  $("#url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleAdd();
  });
  $("#start-btn").addEventListener("click", async () => {
    try {
      await api("POST", "/api/download/start");
    } catch (e) {
      showToast(e.message, "error");
    }
  });
  $("#stop-btn").addEventListener("click", async () => {
    $("#stop-btn").disabled = true;
    try {
      await api("POST", "/api/download/stop");
      showToast("Stopping — finishing the current track, no new ones will start…");
    } catch (e) {
      showToast(e.message, "error");
    } finally {
      $("#stop-btn").disabled = false;
    }
  });
  $("#remove-selected-btn").addEventListener("click", removeSelectedQueueItems);
  $("#clear-queue-btn").addEventListener("click", async () => {
    try {
      const data = await api("POST", "/api/queue/clear");
      state.queue = data.queue;
      renderQueue();
    } catch (e) {
      showToast(`Failed to clear queue: ${e.message}`, "error");
    }
  });
  $("#retry-failed-btn").addEventListener("click", async () => {
    const tracks = state.failedTracks;
    $("#retry-failed-btn").classList.add("hidden");
    let added = 0;
    for (const t of tracks) {
      try {
        const data = await api("POST", "/api/queue", { input: t.url });
        state.queue.push(data.added);
        added++;
      } catch (e) {
        showToast(`Failed to re-queue "${t.name}": ${e.message}`, "error");
      }
    }
    renderQueue();
    if (added) showToast(`Re-queued ${added} failed track(s) — press Start downloading when ready.`, "success");
  });
  $("#retry-login-btn").addEventListener("click", async () => {
    try {
      await api("POST", "/api/login/retry");
      await refreshStatus();
    } catch (e) {
      showToast(e.message, "error");
    }
  });
  $("#setup-form").addEventListener("submit", submitSetupForm);
  $("#settings-btn").addEventListener("click", openSettingsModal);
  $(".settings-close").addEventListener("click", () => $("#settings-modal").classList.add("hidden"));
  const statsDays = () => {
    const sel = document.querySelector(".stats-range-btn.selected");
    return sel && sel.dataset.days ? Number(sel.dataset.days) : null;
  };
  $("#stats-btn").addEventListener("click", () => openStatsModal(statsDays()));
  $$(".stats-range-btn").forEach((b) => b.addEventListener("click", () => {
    $$(".stats-range-btn").forEach((x) => x.classList.remove("selected"));
    b.classList.add("selected");
    openStatsModal(statsDays());
  }));
  $(".stats-close").addEventListener("click", () => $("#stats-modal").classList.add("hidden"));
  $("#settings-save-btn").addEventListener("click", saveSettingsModal);
  $(".search-close").addEventListener("click", () => $("#search-modal").classList.add("hidden"));
  $("#search-add-btn").addEventListener("click", addSelectedSearchResults);
  $(".wizard-close").addEventListener("click", () => $("#wizard-modal").classList.add("hidden"));
  $("#wizard-scope-all-btn").addEventListener("click", () => confirmWizard(true));
  $("#wizard-scope-confirm-checkbox").addEventListener("change", (e) => {
    $("#wizard-scope-all-btn").disabled = state.wizardLargeCatalogue && !e.target.checked;
  });
  $("#wizard-scope-filterlive-btn").addEventListener("click", () => startWizardFilter($("#wizard-modal").dataset.url));
  $("#filter-genre").addEventListener("change", onGenreChange);
  $("#filter-apply-btn").addEventListener("click", () => { state.filter.selectedArtists.clear(); applyFilter(1, true); });
  $("#filter-prev").addEventListener("click", () => applyFilter(state.filter.page - 1, false));
  $("#filter-next").addEventListener("click", () => applyFilter(state.filter.page + 1, false));
  $("#filter-selectall").addEventListener("change", (e) => toggleSelectPage(e.target.checked));
  $("#filter-add").addEventListener("click", addFilterSelected);
  $("#wizard-scope-browse-btn").addEventListener("click", () => startWizardBrowse($("#wizard-modal").dataset.url));
  $("#wizard-browse-prev").addEventListener("click", () => loadBrowsePage(state.browse.page - 1));
  $("#wizard-browse-next").addEventListener("click", () => loadBrowsePage(state.browse.page + 1));
  $("#wizard-browse-add").addEventListener("click", addBrowseSelected);
  $("#wizard-scope-filter-btn").addEventListener("click", () => startWizardScan($("#wizard-modal").dataset.url));
  $("#wizard-bypass-btn").addEventListener("click", () => confirmWizard(true));
  $("#wizard-confirm-btn").addEventListener("click", () => confirmWizard(false));
  $("#recheck-art-btn").addEventListener("click", async () => {
    $("#art-recheck-status").textContent = "Starting…";
    try {
      await api("POST", "/api/art/recheck", { only_missing: $("#art-only-missing").checked });
    } catch (e) {
      $("#art-recheck-status").textContent = e.message;
    }
  });
  $("#verify-library-btn").addEventListener("click", async () => {
    $("#verify-library-status").textContent = "Checking…";
    $("#remove-missing-btn").classList.add("hidden");
    try {
      const r = await api("GET", "/api/history/verify");
      $("#verify-library-status").textContent =
        `${r.total_checked} tracked — ${r.ok} found on disk, ${r.missing} missing, ${r.no_path_recorded} predate file-path tracking.`;
      if (r.missing > 0) {
        $("#remove-missing-btn").textContent = `Remove ${r.missing} missing entries`;
        $("#remove-missing-btn").classList.remove("hidden");
      }
    } catch (e) {
      $("#verify-library-status").textContent = e.message;
    }
  });
  $("#remove-missing-btn").addEventListener("click", async () => {
    if (!confirm("Remove history entries for tracks whose files are gone? This can't be undone.")) return;
    try {
      const r = await api("POST", "/api/history/remove-missing");
      $("#verify-library-status").textContent = `Removed ${r.removed} entries.`;
      $("#remove-missing-btn").classList.add("hidden");
    } catch (e) {
      $("#verify-library-status").textContent = e.message;
    }
  });
  $("#clear-history-btn").addEventListener("click", async () => {
    if (!confirm("Wipe the entire download history? Dedup will no longer know about anything downloaded before this point. This can't be undone.")) return;
    try {
      const r = await api("POST", "/api/history/clear");
      showToast(`Cleared ${r.removed} history entries.`, "success");
      $("#verify-library-status").textContent = "";
      $("#remove-missing-btn").classList.add("hidden");
    } catch (e) {
      showToast(e.message, "error");
    }
  });
  $("#watch-add-btn").addEventListener("click", async () => {
    const input = $("#watch-url-input");
    const url = input.value.trim();
    if (!url) return;
    try {
      const data = await api("POST", "/api/watch", { url });
      renderWatchList(data.watched_labels || [], data.watched_artists || []);
      input.value = "";
      $("#watch-status").textContent = "Watching.";
    } catch (e) {
      $("#watch-status").textContent = e.message;
    }
  });
  $("#watch-check-now-btn").addEventListener("click", async () => {
    $("#watch-status").textContent = "Checking now…";
    try {
      await api("POST", "/api/watch/check-now");
    } catch (e) {
      $("#watch-status").textContent = e.message;
    }
  });
  $("#explore-toggle").addEventListener("click", toggleExplore);
  $("#explore-genre").addEventListener("change", () => {
    localStorage.setItem("exploreGenre", $("#explore-genre").value);
    state.explore.selected.clear();
    updateExploreSelCount();
    loadExplore(1);
  });
  $$(".explore-tab").forEach((b) => b.addEventListener("click", () => {
    $$(".explore-tab").forEach((x) => x.classList.remove("selected"));
    b.classList.add("selected");
    state.explore.section = b.dataset.section;
    const bpmable = b.dataset.section === "tracks";
    $$(".explore-bpm").forEach((f) => f.classList.toggle("hidden", !bpmable));
    $("#explore-apply").classList.toggle("hidden", !bpmable);
    state.explore.selected.clear();
    updateExploreSelCount();
    loadExplore(1);
  }));
  $("#explore-apply").addEventListener("click", () => loadExplore(1));
  $("#explore-prev").addEventListener("click", () => loadExplore(state.explore.page - 1));
  $("#explore-next").addEventListener("click", () => loadExplore(state.explore.page + 1));
  $("#explore-selectall").addEventListener("change", (e) => toggleExploreSelectPage(e.target.checked));
  $("#explore-add").addEventListener("click", addExploreSelected);
  if (localStorage.getItem("exploreOpen")) toggleExplore(); // restore last session's open state
}

// ---- artist / label catalogue pages ----

// Clickable artist/label names on track cards. Items carry {artists:[{id,name,slug}], label:{...}}
// from _track_item; anything without an id/slug degrades to plain text.
function artistLinksHtml(it) {
  const bits = (it.artists || []).slice(0, 3).map((a) =>
    a.id && a.slug
      ? `<span class="linkish" data-kind="artist" data-id="${a.id}" data-slug="${esc(a.slug)}">${esc(a.name)}</span>`
      : esc(a.name || ""));
  let html = bits.filter(Boolean).join(", ") || esc(it.artist || "");
  if (it.label && it.label.id && it.label.slug) {
    html += ` <span class="linkish lbl" data-kind="label" data-id="${it.label.id}" data-slug="${esc(it.label.slug)}">[${esc(it.label.name)}]</span>`;
  }
  return html;
}

function wireCatalogueLinks(card) {
  card.querySelectorAll(".linkish").forEach((el) => el.addEventListener("click", (e) => {
    e.stopPropagation(); // don't toggle the card's selection
    openCatalogue(
      `https://www.beatport.com/${el.dataset.kind}/${el.dataset.slug}/${el.dataset.id}`,
      el.textContent.replace(/^\[|\]$/g, ""));
  }));
}

// The wizard's filter pane works off a bare URL, so it doubles as a standalone
// artist/label catalogue page — no queue item involved (addFilterSelected
// already tolerates wizardQueueIndex being null).
async function openCatalogue(url, title) {
  state.wizardQueueIndex = null;
  $("#search-modal").classList.add("hidden"); // don't stack modals if opened from search
  const modal = $("#wizard-modal");
  modal.dataset.url = url;
  ["#wizard-scope", "#wizard-scanning", "#wizard-results", "#wizard-browse"]
    .forEach((s) => $(s).classList.add("hidden"));
  modal.classList.remove("hidden");
  await startWizardFilter(url);
  $("#wizard-title").textContent = title;
  applyFilter(1, true); // show the full catalogue immediately; user narrows from there
}

// ---- explore (storefront: Top 100 / new tracks / new releases / DJ charts) ----

async function toggleExplore() {
  const body = $("#explore-body");
  const open = !body.classList.toggle("hidden");
  $("#explore-toggle").textContent = open ? "Hide ▴" : "Browse Beatport ▾";
  localStorage.setItem("exploreOpen", open ? "1" : "");
  if (open) {
    await loadExploreGenres();
    if (!state.explore.loaded) loadExplore(1);
  } else {
    stopPreview();
  }
}

async function loadExploreGenres() {
  const gsel = $("#explore-genre");
  if (gsel.dataset.loaded) return;
  try {
    const data = await api("GET", "/api/genres");
    data.genres.forEach((g) => {
      const o = document.createElement("option");
      o.value = g.id; o.textContent = g.name;
      gsel.appendChild(o);
    });
    gsel.dataset.loaded = "1";
    const saved = localStorage.getItem("exploreGenre");
    // only restore if the option still exists (Beatport reshuffles genres occasionally)
    if (saved && gsel.querySelector(`option[value="${saved}"]`)) gsel.value = saved;
  } catch (e) { /* keep the "All genres" option only */ }
}

async function loadExplore(page) {
  const ex = state.explore;
  const genre = $("#explore-genre").value;
  $("#explore-status").textContent = "Loading…";
  try {
    const bmin = parseInt($("#explore-bpm-min").value, 10);
    const bmax = parseInt($("#explore-bpm-max").value, 10);
    const data = await api("POST", "/api/explore", {
      section: ex.section,
      genre_id: genre ? Number(genre) : null,
      bpm_min: ex.section === "tracks" && Number.isFinite(bmin) ? bmin : null,
      bpm_max: ex.section === "tracks" && Number.isFinite(bmax) ? bmax : null,
      page: Math.max(1, page),
    });
    ex.loaded = true;
    ex.page = data.page;
    ex.kind = data.kind;
    renderExploreGrid(data.items);
    const label = { top100: "in the Top 100", tracks: "new tracks", releases: "new releases", charts: "DJ charts" }[ex.section] || "";
    $("#explore-status").textContent = `${data.count} ${label}`;
    $("#explore-page").textContent = (data.has_prev || data.has_next) ? `page ${data.page}` : "";
    $("#explore-prev").disabled = !data.has_prev;
    $("#explore-next").disabled = !data.has_next;
    $("#explore-selectall").checked = false;
  } catch (e) {
    $("#explore-status").textContent = e.message;
  }
}

function exploreMeta(it) {
  if (state.explore.kind === "tracks") {
    return [it.bpm ? `${it.bpm} BPM` : "", it.key, it.genre, it.length, it.year].filter(Boolean).join(" · ");
  }
  const count = it.track_count ? `${it.track_count} track${it.track_count === 1 ? "" : "s"}` : "";
  return [it.catno, count, it.date].filter(Boolean).join(" · ");
}

function renderExploreGrid(items) {
  const grid = $("#explore-grid");
  grid.innerHTML = "";
  stopPreview();
  state.explore._rendered = items;
  items.forEach((it) => {
    const sel = state.explore.selected.has(it.url);
    const card = document.createElement("div");
    card.className = "browse-card" + (sel ? " selected" : "");
    card.innerHTML = `
      ${it.cover ? `<img class="browse-cover" src="${esc(it.cover)}" loading="lazy">`
                 : `<div class="browse-cover placeholder"></div>`}
      <div class="browse-info">
        <div class="browse-title">${esc(it.name)}</div>
        <div class="browse-artist muted small">${artistLinksHtml(it)}</div>
        <div class="browse-meta muted small">${esc(exploreMeta(it))}</div>
      </div>
      <div class="browse-check">✓</div>`;
    if (it.preview) card.appendChild(makePreviewBtn(it.preview));
    wireCatalogueLinks(card);
    card.addEventListener("click", () => {
      if (state.explore.selected.has(it.url)) state.explore.selected.delete(it.url);
      else state.explore.selected.set(it.url, it.name);
      card.classList.toggle("selected");
      updateExploreSelCount();
    });
    grid.appendChild(card);
  });
}

function updateExploreSelCount() {
  const n = state.explore.selected.size;
  $("#explore-selcount").textContent = `${n} selected`;
  $("#explore-add").disabled = n === 0;
  $("#explore-add").textContent = n ? `Add ${n} to queue` : "Add selected to queue";
}

function toggleExploreSelectPage(checked) {
  const items = state.explore._rendered || [];
  const cards = $("#explore-grid").querySelectorAll(".browse-card");
  items.forEach((it, i) => {
    if (checked) state.explore.selected.set(it.url, it.name);
    else state.explore.selected.delete(it.url);
    if (cards[i]) cards[i].classList.toggle("selected", checked);
  });
  updateExploreSelCount();
}

async function addExploreSelected() {
  const picks = Array.from(state.explore.selected.entries());
  if (!picks.length) return;
  $("#explore-add").disabled = true;
  let added = 0;
  for (const [url, name] of picks) {
    try {
      const data = await api("POST", "/api/queue", { input: url });
      if (data.added) { state.queue.push(data.added); added++; }
    } catch (e) {
      showToast(`Failed to add "${name}": ${e.message}`, "error");
    }
  }
  state.explore.selected.clear();
  updateExploreSelCount();
  $("#explore-grid").querySelectorAll(".browse-card.selected").forEach((c) => c.classList.remove("selected"));
  renderQueue();
  if (added) showToast(`Added ${added} item(s) to queue — press Start downloading when ready.`, "success");
}

(async function main() {
  wireEvents();
  connectEvents();
  await refreshStatus();
  refreshWatchList(); // watch list lives on the main page now
  setInterval(refreshStatus, 8000); // cheap safety net alongside SSE
})();
