"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  status: null,
  queue: [],
  wizardQueueIndex: null,
  wizardScan: null, // {genres, subgenres, artists} rank entries with .selected
  searchResults: [],
  activity: new Map(), // track id -> card element
  runCounts: { downloaded: 0, skipped: 0, failed: 0 },
  failedTracks: [],
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

function hueFor(name) {
  let h = 0;
  for (const c of name || "") h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function artStyle(name, cover) {
  if (cover) return `background-image:url('${cover}')`;
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
  $("#side-mascot").classList.toggle("visible", !!state.status?.downloading);

  state.queue.forEach((item, idx) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-art" style="${artStyle(item.name, item.cover)}">${item.cover ? "" : initials(item.name)}</div>
      <div class="card-body">
        <div class="card-badge">${item.type.replace(/s$/, "")}${item.filters === undefined ? "" : item.filters ? " · filtered" : item.needs_wizard ? "" : " · unfiltered"}</div>
        <div class="card-name" title="${item.name}">${item.name}</div>
        <div class="card-subtitle" title="${item.subtitle}">${item.subtitle}</div>
      </div>
      ${item.needs_wizard === false && (item.type === "labels" || item.type === "artists") ? '<button class="card-edit" title="Edit filters">&#9998;</button>' : ""}
      <button class="card-remove" title="Remove">&times;</button>
    `;
    card.querySelector(".card-remove").addEventListener("click", () => removeQueueItem(idx));
    const editBtn = card.querySelector(".card-edit");
    if (editBtn) editBtn.addEventListener("click", () => openWizard(idx, item.url));
    grid.appendChild(card);
  });
}

async function removeQueueItem(idx) {
  const data = await api("DELETE", `/api/queue/${idx}`);
  state.queue = data.queue;
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
        <div class="card-badge">${r.kind}</div>
        <div class="card-name" title="${r.name}">${r.name}</div>
        <div class="card-subtitle" title="${r.subtitle}">${r.subtitle}</div>
      </div>
    `;
    card.addEventListener("click", () => card.classList.toggle("selected"));
    grid.appendChild(card);
  });
  $("#search-modal").classList.remove("hidden");
}

async function addSelectedSearchResults() {
  const selected = $$("#search-results-grid .card.selected").map((c) => state.searchResults[Number(c.dataset.idx)]);
  for (const r of selected) {
    try {
      const data = await api("POST", "/api/queue", { input: r.url });
      const item = data.added;
      state.queue.push(item);
    } catch (e) {
      showToast(`Failed to add "${r.name}": ${e.message}`, "error");
    }
  }
  renderQueue();
  $("#search-modal").classList.add("hidden");
  if (selected.length) showToast(`Added ${selected.length} item(s) to queue.`, "success");
}

// ---- wizard ----

function openWizard(queueIndex, url) {
  state.wizardQueueIndex = queueIndex;
  state.wizardScan = null;
  const item = state.queue[queueIndex];
  $("#wizard-title").textContent = "What do you want to queue?";
  $("#wizard-scope-name").textContent = `"${item ? item.name : url}" — queue the whole thing now, or scan first to filter by genre/subgenre/artist/date.`;
  $("#wizard-scope").classList.remove("hidden");
  $("#wizard-scanning").classList.add("hidden");
  $("#wizard-results").classList.add("hidden");
  $("#wizard-modal").classList.remove("hidden");
  $("#wizard-modal").dataset.url = url;
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
    chip.innerHTML = `<span>${e.name}</span><span class="chip-bar"><span class="chip-bar-fill" style="width:${Math.max(6, (e.count / max) * 100)}%"></span></span><span class="chip-count">${e.count}</span>`;
    chip.addEventListener("click", () => {
      chip.classList.toggle("selected");
      if (selectedSet.has(e.name)) selectedSet.delete(e.name);
      else selectedSet.add(e.name);
    });
    container.appendChild(chip);
  });
}

function renderStatBars(container, entries, labelKey, valueKey, formatValue) {
  container.innerHTML = "";
  if (!entries.length) {
    container.innerHTML = '<p class="muted small">Nothing yet.</p>';
    return;
  }
  const max = Math.max(...entries.map((e) => e[valueKey])) || 1;
  entries.forEach((e) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    const value = formatValue ? formatValue(e[valueKey]) : e[valueKey];
    chip.innerHTML = `<span>${e[labelKey]}</span><span class="chip-bar"><span class="chip-bar-fill" style="width:${Math.max(4, (e[valueKey] / max) * 100)}%"></span></span><span class="chip-count">${value}</span>`;
    container.appendChild(chip);
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

async function openStatsModal() {
  $("#stats-modal").classList.remove("hidden");
  const stats = await api("GET", "/api/stats");

  const tiles = $("#stats-tiles");
  tiles.innerHTML = `
    <div class="stats-tile"><div class="stats-tile-value">${stats.total_tracks}</div><div class="stats-tile-label">Tracks</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${stats.total_releases}</div><div class="stats-tile-label">Releases</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${stats.total_labels}</div><div class="stats-tile-label">Labels</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${stats.total_artists}</div><div class="stats-tile-label">Artists</div></div>
    <div class="stats-tile"><div class="stats-tile-value">${formatBytes(stats.total_bytes)}</div><div class="stats-tile-label">Downloaded</div></div>
  `;

  renderStatBars($("#stats-genres"), stats.genres, "name", "count");
  renderStatBars($("#stats-artists"), stats.artists, "name", "count");
  renderStatBars($("#stats-labels"), stats.labels, "name", "count");
  renderStatBars($("#stats-bpm"), stats.bpm_buckets, "range", "count");
  renderStatBars($("#stats-keys"), stats.keys, "name", "count");
  renderStatBars($("#stats-activity"), stats.activity_by_month, "month", "count");
  renderStatBars($("#stats-volume"), stats.bytes_by_month, "month", "bytes", formatBytes);
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

// ---- activity / downloading ----

function ensureActivityCard(id, name, artists, release, cover) {
  if (state.activity.has(id)) return state.activity.get(id);
  const card = document.createElement("div");
  card.className = "card activity-card";
  card.innerHTML = `
    <div class="card-top">
      <div class="card-art" style="${artStyle(name, cover)}">${cover ? "" : initials(name)}</div>
      <div class="card-body">
        <div class="card-name" title="${name}">${name}</div>
        <div class="card-subtitle" title="${artists}">${artists}${release ? " — " + release : ""}</div>
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
      $("#watch-status").textContent = `Checking ${ev.count} watched label(s)...`;
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
  refreshWatchList();
}

async function refreshWatchList() {
  const data = await api("GET", "/api/watch");
  renderWatchList(data.watched_labels || []);
}

function renderWatchList(entries) {
  const el = $("#watch-list");
  el.innerHTML = "";
  if (!entries.length) {
    el.innerHTML = '<p class="muted small">Not watching any labels yet.</p>';
    return;
  }
  entries.forEach((w, idx) => {
    const row = document.createElement("div");
    row.className = "chip";
    row.style.cssText = "cursor:default;flex-direction:column;align-items:stretch;gap:4px;";
    const pending = w.pending_releases || [];
    const pendingText = pending.length
      ? `${pending.length} upcoming pre-release${pending.length > 1 ? "s" : ""}: ` +
        pending.map((p) => `${p.release_name} (${p.expected_date})`).join(", ")
      : "";
    row.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;">
        <span style="flex:1;">${w.name}</span>
      </div>
      ${pendingText ? `<span class="muted" style="font-size:11px;">${pendingText}</span>` : ""}
    `;
    const removeBtn = document.createElement("button");
    removeBtn.className = "card-remove";
    removeBtn.style.cssText = "position:absolute;top:4px;right:4px;";
    removeBtn.innerHTML = "&times;";
    removeBtn.addEventListener("click", async () => {
      const data = await api("DELETE", `/api/watch/${idx}`);
      renderWatchList(data.watched_labels || []);
    });
    row.style.position = "relative";
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
  $("#clear-queue-btn").addEventListener("click", async () => {
    const data = await api("POST", "/api/queue/clear");
    state.queue = data.queue;
    renderQueue();
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
  $("#stats-btn").addEventListener("click", openStatsModal);
  $(".stats-close").addEventListener("click", () => $("#stats-modal").classList.add("hidden"));
  $("#settings-save-btn").addEventListener("click", saveSettingsModal);
  $(".search-close").addEventListener("click", () => $("#search-modal").classList.add("hidden"));
  $("#search-add-btn").addEventListener("click", addSelectedSearchResults);
  $(".wizard-close").addEventListener("click", () => $("#wizard-modal").classList.add("hidden"));
  $("#wizard-scope-all-btn").addEventListener("click", () => confirmWizard(true));
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
  $("#watch-add-btn").addEventListener("click", async () => {
    const input = $("#watch-url-input");
    const url = input.value.trim();
    if (!url) return;
    try {
      const data = await api("POST", "/api/watch", { url });
      renderWatchList(data.watched_labels || []);
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
}

(async function main() {
  wireEvents();
  connectEvents();
  await refreshStatus();
  setInterval(refreshStatus, 8000); // cheap safety net alongside SSE
})();
