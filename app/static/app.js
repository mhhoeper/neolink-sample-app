"use strict";
const $ = id => document.getElementById(id);
const Timeline = window.NeolinkTimeline;
const state = {
  cameras: [], camera: null, scope: null, events: [], statuses: [], filter: "all",
  selected: null, activeRecord: null, mode: null, loading: false, fetching: false,
  day: "", span: Timeline.DAY, from: 0, lower: 0, upper: Timeline.DAY, recent: true,
  visible: 100, choices: [], query: null,
  mediaTime: 0, videoDuration: null, recordingReady: false, demo: false
};
const names = {
  people: "Person detected", vehicle: "Vehicle detected", dog_cat: "Animal detected", face: "Face detected",
  package: "Package detected", visitor: "Visitor", pir: "Motion detected", md: "Motion detected",
  sched: "Scheduled recording", manual: "Manual recording", io: "Alarm recording", cry: "Sound detected"
};
function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name); svg.setAttribute("aria-hidden", "true"); svg.append(use); return svg;
}
function showAlert(message) { $("alert").textContent = message; $("alert").hidden = !message; }
async function api(url, options = {}) {
  const response = await fetch(url, { ...options, cache: "no-store" });
  if (!response.ok) {
    let message = "The server is unavailable. Please try again.";
    try { const body = await response.json(); message = typeof body.detail === "string" ? body.detail : "The request is invalid."; } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
function localTimestamp(now = new Date()) {
  const day = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
  return day + "T" + [now.getHours(), now.getMinutes(), now.getSeconds()].map(value => String(value).padStart(2, "0")).join(":");
}
function eventTime(row) {
  const value = Timeline.seconds(row.start, state.day);
  return value === null ? "Time unknown" : Timeline.dateClock(state.day, value, true);
}
function duration(value) {
  if (!Number.isFinite(value)) return "Unknown";
  return value >= 60 ? Math.floor(value / 60) + " min " + String(value % 60).padStart(2, "0") + " s" : value + " s";
}
function eventType(row) {
  const ai = row.ai || [], tags = row.tags || [];
  const tag = ai[0] || tags.find(item => names[item]) || "";
  return { label: names[tag] || "Camera recording",
    icon: tag === "people" || tag === "face" ? "person" : tag === "vehicle" ? "vehicle" : "motion",
    person: ai.includes("people") || tags.includes("people") };
}
function filteredEvents() {
  return state.events.filter(row => (!state.scope || row.camera_id === state.scope) &&
    (state.filter === "all" || (state.filter === "motion" ?
      (row.tags || []).some(tag => ["md", "pir"].includes(tag)) :
      (row.tags || []).includes(state.filter) || (row.ai || []).includes(state.filter))));
}
function inWindow(row) {
  return Timeline.bounds(row, state.day) === null || Timeline.intersects(row, state.day, state.from, state.from + state.span);
}
function currentPoint() {
  return state.activeRecord ? Timeline.cursor(state.activeRecord, state.day, state.mediaTime) : null;
}
function changeWindow(span, center) {
  state.span = Number(span);
  state.from = Timeline.clampWindow(state.span, center, state.lower, state.upper);
  state.visible = 100; state.choices = [];
  renderTimeline(); renderEvents();
}
function focusRecord(row, offset) {
  if (state.scope && state.scope !== row.camera_id) { state.scope = null; renderCameras(); }
  const b = Timeline.bounds(row, state.day);
  if (!b) return;
  const length = Math.max(600, b.end - b.start + 120);
  const span = [600, 3600, 21600, Timeline.DAY].find(value => value >= length) || Timeline.DAY;
  state.span = span;
  state.from = Timeline.clampWindow(span, b.start + offset, state.lower, state.upper);
  state.visible = 100; state.choices = [];
}
function listMessage(title, text, retry = false) {
  const box = element("div", "list-message");
  box.append(icon("archive"), element("h3", "", title), element("p", "", text));
  if (retry) {
    const button = element("button", "button button-stop", "Reload");
    button.onclick = () => loadEvents(true); box.append(button);
  }
  $("event-list").replaceChildren(box);
}
function renderEvents() {
  const rows = filteredEvents().filter(inWindow);
  $("event-count").textContent = filteredEvents().length;
  $("list-count").textContent = rows.length + " recordings";
  $("show-more").hidden = rows.length <= state.visible;
  if (!rows.length) {
    const missing = state.statuses.filter(camera => camera.state === "error");
    if (state.fetching && !state.events.length) listMessage("Loading events", "Querying the cameras …");
    else if (missing.length === state.cameras.length && missing.length) listMessage("Events unavailable", "No camera could be queried. Check the connection and configuration.", true);
    else listMessage("No recordings in this selection", missing.length ?
      "Some cameras could not be queried. Their status is shown on the timeline." :
      "Choose another time window, date or filter.");
    return;
  }
  $("event-list").replaceChildren(...rows.slice(0, state.visible).map(row => {
    const type = eventType(row);
    const button = element("button", "event-row" + (row.id === state.selected ? " selected" : ""));
    button.dataset.eventId = row.id; button.dataset.cameraId = row.camera_id;
    button.setAttribute("aria-label", row.camera_name + " · " + type.label + " · " + eventTime(row) + " play");
    const marker = element("span", "event-icon" + (type.person ? " people" : "")); marker.append(icon(type.icon));
    const info = element("span", "event-info"), title = element("span", "event-title");
    title.append(element("span", "", type.label));
    const time = element("time", "", eventTime(row));
    if (row.start) time.dateTime = row.start;
    title.append(time);
    const sub = element("span", "event-sub");
    sub.append(element("span", "event-camera-name", row.camera_name), element("span", "", "·"),
      element("span", "", duration(row.duration)), element("span", "", "·"), element("span", "", row.size));
    if ((row.ai || []).length) sub.append(element("span", "event-tag", "AI"));
    info.append(title, sub); button.append(marker, info, icon("play"));
    button.onclick = () => playback.playEvent(row);
    return button;
  }));
}
function formatUpdate(timestamp) {
  if (!Number.isFinite(timestamp)) return "";
  const date = new Date(timestamp * 1000);
  const sameDay = date.toDateString() === new Date().toDateString();
  return new Intl.DateTimeFormat("en-GB", { ...(sameDay ? {} : { day: "2-digit", month: "2-digit" }),
    hour: "2-digit", minute: "2-digit" }).format(date);
}
function renderNotice() {
  const problems = state.statuses.filter(camera => ["error", "stale", "partial"].includes(camera.state));
  const notice = $("event-notice"); notice.hidden = !problems.length;
  notice.textContent = problems.map(camera => camera.name + ": " +
    (camera.state === "stale" ? "older cache snapshot" + (camera.updated_at ? " (" + formatUpdate(camera.updated_at) + ")" : "") : camera.error || "Query failed") +
    (camera.state === "stale" && camera.error ? " · " + camera.error : "")).join(" · ");
  const stamps = state.statuses.map(camera => camera.updated_at).filter(Number.isFinite);
  const cached = state.statuses.length && state.statuses.every(camera => camera.state === "cached");
  $("cache-info").textContent = stamps.length ? (cached ? "Cache · " : "Updated · ") + formatUpdate(Math.min(...stamps)) : "";
}
function renderTimelineChoices() {
  const container = $("timeline-choices"); container.hidden = !state.choices.length;
  if (!state.choices.length) { container.replaceChildren(); return; }
  const title = element("p", "", state.choices.length + " recordings are close together. Choose a recording:");
  const buttons = element("div", "choice-buttons");
  state.choices.slice(0, 30).forEach(row => {
    const button = element("button", "button button-stop", row.camera_name + " · " + eventTime(row));
    button.onclick = () => playback.playEvent(row); buttons.append(button);
  });
  if (state.choices.length > 30) buttons.append(element("span", "", "All recordings can also be selected from the list below."));
  const close = element("button", "button button-stop", "Close");
  close.onclick = () => { state.choices = []; renderTimelineChoices(); }; buttons.append(close);
  container.replaceChildren(title, buttons);
}
function groupSelection(group) {
  const width = Math.max(600, group.to - group.from + 120);
  const span = [600, 3600, 21600, Timeline.DAY].find(value => value >= width) || Timeline.DAY;
  state.span = span;
  state.from = Timeline.clampWindow(span, (group.from + group.to) / 2, state.lower, state.upper);
  state.choices = group.events; state.visible = 100;
  renderTimeline(); renderEvents();
}
function renderTimeline() {
  const container = $("timeline-tracks"), source = filteredEvents();
  const cameras = state.cameras.filter(camera => !state.scope || camera.id === state.scope);
  const existingWidth = container.querySelector(".timeline-track")?.clientWidth;
  const width = existingWidth || Math.max(90, container.clientWidth - (innerWidth <= 640 ? 75 : innerWidth <= 1150 ? 99 : 113));
  const ticks = width < 400 ? 3 : 6;
  $("time-window").textContent = Timeline.clock(state.from) + "–" + Timeline.clock(state.from + state.span);
  $("time-window").title = Timeline.dateClock(state.day, state.from) + " – " + Timeline.dateClock(state.day, state.from + state.span);
  $("range-info").textContent = (state.recent ? "Last 24 hours · " : "Archive · ") +
    Timeline.dateClock(state.day, state.lower) + " – " + Timeline.dateClock(state.day, state.upper);
  $("range-now").disabled = state.recent && state.span === Timeline.DAY;
  $("time-zoom").value = String(state.span);
  $("time-back").disabled = state.from <= state.lower;
  $("time-forward").disabled = state.from + state.span >= state.upper;
  $("timeline-axis").replaceChildren(...Array.from({ length: ticks }, (_, index) => {
    const point = state.from + state.span * index / (ticks - 1);
    const tick = element("span", "", Timeline.clock(((point % Timeline.DAY) + Timeline.DAY) % Timeline.DAY));
    tick.append(element("small", "", Timeline.timestamp(state.day, point).slice(5, 10)));
    tick.style.left = (index / (ticks - 1) * 100) + "%"; return tick;
  }));
  container.replaceChildren(...cameras.map(camera => {
    const status = state.statuses.find(item => item.id === camera.id);
    const lane = element("div", "timeline-row" + (state.activeRecord?.camera_id === camera.id ? " is-active" : ""));
    const label = element("div", "timeline-camera");
    label.append(element("strong", "", camera.name));
    const cameraRows = source.filter(row => row.camera_id === camera.id);
    let note = state.fetching ? "Loading …" : cameraRows.length + " recordings";
    if (status?.state === "error") note = "Query failed";
    if (status?.state === "stale") note = "Older cache";
    if (status?.state === "partial") note = "Incomplete query";
    label.append(element("small", status && ["error", "stale", "partial"].includes(status.state) ? "camera-warning" : "", note));
    const track = element("div", "timeline-track" + (status?.state === "error" ? " is-unknown" : ""));
    track.dataset.cameraId = camera.id;
    track.setAttribute("aria-label", camera.name + " · " + note);
    for (let index = 0; index < ticks; index++) {
      const guide = element("span", "timeline-gridline"); guide.style.left = (index / (ticks - 1) * 100) + "%"; track.append(guide);
    }
    const coarse = matchMedia("(pointer: coarse)").matches;
    const groups = Timeline.groups(cameraRows, state.day, state.from, state.from + state.span, width, coarse ? 44 : 28);
    groups.forEach(group => {
      const selected = group.events.some(row => row.id === state.selected);
      const hit = element("button", "timeline-hit" + (selected ? " selected" : ""));
      hit.style.left = group.hitStart + "px"; hit.style.width = (group.hitEnd - group.hitStart) + "px";
      hit.dataset.count = String(group.events.length);
      const title = camera.name + " · " + (group.events.length === 1 ?
        eventTime(group.events[0]) + " · " + eventType(group.events[0]).label :
        group.events.length + " recordings · " + Timeline.clock(group.from, true) + "–" + Timeline.clock(group.to, true));
      hit.setAttribute("aria-label", title + (group.events.length === 1 ? " play" : " select")); hit.title = title;
      group.events.forEach(row => {
        const b = Timeline.bounds(row, state.day);
        const x = (Math.max(state.from, b.start) - state.from) / state.span * width;
        const length = (Math.min(state.from + state.span, b.end) - Math.max(state.from, b.start)) / state.span * width;
        const bar = element("span", "timeline-bar" + (eventType(row).person ? " is-person" : "") + (length < 2 ? " is-point" : ""));
        bar.style.left = (x - group.hitStart) + "px"; bar.style.width = Math.max(2, length) + "px"; bar.dataset.eventId = row.id;
        hit.append(bar);
        if (row.id === state.selected) {
          const point = element("span", "selected-dot"); point.style.left = (x - group.hitStart) + "px"; hit.append(point);
        }
      });
      if (group.events.length > 1) hit.append(element("span", "timeline-cluster", String(group.events.length)));
      hit.onclick = event => {
        if (group.events.length > 1) { groupSelection(group); return; }
        const row = group.events[0], b = Timeline.bounds(row, state.day), rect = track.getBoundingClientRect();
        const requested = state.from + (event.clientX - rect.left) / rect.width * state.span;
        const offset = event.detail > 0 && requested >= b.start && requested <= b.end ?
          Timeline.seekOffset(row, state.day, requested) : 0;
        playback.playEvent(row, offset);
      };
      track.append(hit);
    });
    const cursor = element("span", "timeline-cursor"); cursor.hidden = true; track.append(cursor);
    lane.append(label, track); return lane;
  }));
  renderTimelineChoices(); updateCursor();
}
function updateCursor() {
  const point = currentPoint();
  const shown = state.mode === "event" && point !== null && point >= state.from && point <= state.from + state.span;
  $("timeline-tracks").querySelectorAll(".timeline-cursor").forEach(cursor => {
    cursor.hidden = !shown;
    if (shown) cursor.style.left = ((point - state.from) / state.span * 100) + "%";
  });
  $("playback-clock").textContent = state.mode === "live" ? "Live" : point === null ? "—" : Timeline.dateClock(state.day, point, true);
  $("playback-progress").textContent = state.activeRecord ?
    duration(Math.floor(state.mediaTime)) + (Number.isFinite(state.videoDuration) ? " / " + duration(Math.round(state.videoDuration)) : " · loading") :
    state.mode === "live" ? "Live view from the selected camera" : "The timeline follows playback.";
}
function renderPlaybackUI() {
  const camera = state.camera, row = state.activeRecord, video = $("video");
  $("camera-name").textContent = camera?.name || "No camera available";
  $("camera-subtitle").textContent = state.demo ? "Demo · no real camera" : "Video source";
  $("live-camera").value = camera?.id || "";
  $("live-start").disabled = !camera?.live_available || state.loading;
  $("live-switch").disabled = !camera?.live_available || state.loading || state.mode === "live";
  $("stop-video").disabled = !state.mode;
  $("camera-state").textContent = state.loading ? "Loading …" : state.mode === "live" ? "Live active" :
    state.mode === "event" ? video.ended ? "Ended" : video.paused ? "Paused" : "Playing" : "Ready";
  $("playback-kind").textContent = state.mode === "live" ? "LIVEVIEW" : row ? "RECORDING" : "CAMERA VIEW";
  $("playback-title").textContent = row ? eventType(row).label : state.mode === "live" ? camera.name + " · Live" : "Choose a recording";
  $("recording-start").textContent = row ? eventTime(row) : "—";
  $("recording-end").textContent = row?.end ? row.end.slice(11, 19) : "—";
  $("recording-duration").textContent = row ? duration(row.duration) : "—";
  $("viewer-label").replaceChildren(element("span", "tiny-square"), document.createTextNode(state.mode === "live" ? " LIVEVIEW" : " RECORDINGS"));
  $("viewer-status").textContent = row ? row.camera_name : "Choose a moment";
  $("viewer-mode").textContent = state.mode === "live" ? "LIVE" : "RECORDINGS & LIVE";
  const download = $("download-recording");
  download.hidden = !(state.mode === "event" && row && state.recordingReady && !state.loading);
  if (!download.hidden) {
    download.href = "/api/cameras/" + row.camera_id + "/events/" + row.id + "/download";
    download.setAttribute("aria-label", "Download recording from " + row.camera_name + " · " + eventTime(row));
  } else download.removeAttribute("href");
  updateCursor();
}
function changed() { renderPlaybackUI(); renderTimeline(); renderEvents(); }
const playback = createNeolinkPlayback({ state, dom: $, api, timeline: Timeline, focusRecord,
  changed, cursorChanged: updateCursor, loadEvents, report: showAlert });
function renderCameras() {
  $("camera-count").textContent = state.cameras.length;
  const all = element("button", "camera-nav-button" + (!state.scope ? " selected" : ""));
  all.append(icon("grid"), element("span", "camera-name", "All cameras"));
  all.setAttribute("aria-pressed", !state.scope ? "true" : "false");
  all.onclick = () => { state.scope = null; state.visible = 100; renderCameras(); renderTimeline(); renderEvents(); };
  $("camera-nav").replaceChildren(all, ...state.cameras.map(camera => {
    const button = element("button", "camera-nav-button" + (state.scope === camera.id ? " selected" : ""));
    button.append(icon("camera"), element("span", "camera-name", camera.name));
    button.setAttribute("aria-pressed", state.scope === camera.id ? "true" : "false");
    button.onclick = async () => {
      state.scope = camera.id; state.visible = 100;
      renderCameras(); renderTimeline(); renderEvents();
      await playback.selectCamera(camera); renderCameras(); renderTimeline(); renderEvents();
    };
    return button;
  }));
}
async function loadEvents(force = false) {
  if (!state.cameras.length || !state.day) return;
  state.query?.abort();
  const controller = new AbortController(); state.query = controller;
  const previousFrom = Timeline.timestamp(state.day, state.from);
  state.fetching = true; $("refresh").disabled = true;
  document.body.classList.add("events-loading"); renderTimeline(); renderEvents();
  try {
    const range = state.recent ? "until=" + encodeURIComponent(localTimestamp()) : "day=" + encodeURIComponent(state.day);
    const result = await api("/api/events?" + range +
      "&stream=" + $("event-stream").value + "&refresh=" + (force ? "true" : "false"), { signal: controller.signal });
    if (controller.signal.aborted) return;
    state.day = result.day;
    state.lower = result.window ? Timeline.seconds(result.window.start, state.day) : 0;
    state.upper = result.window ? Timeline.seconds(result.window.end, state.day) : Timeline.DAY;
    const previousPoint = Timeline.seconds(previousFrom, state.day);
    state.from = state.span === Timeline.DAY ? state.lower :
      Timeline.clampWindow(state.span, (previousPoint ?? state.lower) + state.span / 2, state.lower, state.upper);
    $("event-date").value = state.day;
    state.events = result.events; state.statuses = result.cameras; state.visible = 100;
    state.choices = [];
  } catch (error) {
    if (error.name === "AbortError") return;
    state.events = [];
    state.statuses = state.cameras.map(camera => ({ ...camera, state: "error", error: error.message }));
  } finally {
    if (state.query === controller) {
      state.fetching = false; $("refresh").disabled = false;
      document.body.classList.remove("events-loading");
      renderNotice(); renderTimeline(); renderEvents();
    }
  }
}
$("live-start").onclick = playback.startLive;
$("live-switch").onclick = playback.startLive;
$("stop-video").onclick = () => playback.stop(true);
$("cancel-video").onclick = () => playback.stop();
$("refresh").onclick = () => loadEvents(true);
$("live-camera").onchange = () => {
  const camera = state.cameras.find(item => item.id === $("live-camera").value);
  if (camera) playback.selectCamera(camera);
};
$("event-date").onchange = async () => {
  if (!$("event-date").value) return;
  state.query?.abort();
  await playback.stop();
  state.day = $("event-date").value; state.recent = false;
  state.from = state.lower = 0; state.upper = Timeline.DAY; state.span = Timeline.DAY; state.events = []; state.statuses = [];
  await loadEvents();
};
$("range-now").onclick = async () => {
  state.query?.abort(); await playback.stop();
  state.recent = true; state.span = Timeline.DAY; state.events = []; state.statuses = [];
  await loadEvents();
};
$("event-stream").onchange = async () => { await playback.stop(); await loadEvents(); };
$("time-zoom").onchange = () => {
  const latest = filteredEvents().find(row => Timeline.bounds(row, state.day));
  const center = currentPoint() ?? (latest ? Timeline.bounds(latest, state.day).start : Timeline.DAY / 2);
  changeWindow($("time-zoom").value, center);
  if (state.span === Timeline.DAY && state.recent) loadEvents();
};
$("time-back").onclick = () => { state.from = Math.max(state.lower, state.from - state.span); state.choices = []; renderTimeline(); renderEvents(); };
$("time-forward").onclick = () => { state.from = Math.min(state.upper - state.span, state.from + state.span); state.choices = []; renderTimeline(); renderEvents(); };
$("show-more").onclick = () => { state.visible += 100; renderEvents(); };
document.querySelectorAll("[data-filter]").forEach(button => button.onclick = () => {
  state.filter = button.dataset.filter; state.visible = 100; state.choices = [];
  document.querySelectorAll("[data-filter]").forEach(item => item.classList.toggle("selected", item === button));
  renderTimeline(); renderEvents();
});
if (window.ResizeObserver) new ResizeObserver(entries => {
  if (entries.some(entry => entry.contentRect.width > 0)) renderTimeline();
}).observe($("timeline-tracks"));
async function init() {
  const timestamp = localTimestamp();
  state.day = timestamp.slice(0, 10); $("event-date").value = state.day;
  state.upper = Timeline.seconds(timestamp, state.day); state.lower = state.upper - Timeline.DAY; state.from = state.lower;
  $("today").textContent = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" }).format(new Date());
  try {
    const result = await api("/api/cameras");
    state.cameras = result.cameras; state.camera = state.cameras[0]; state.demo = result.demo;
    $("demo").hidden = !state.demo;
    $("live-camera").replaceChildren(...state.cameras.map(camera => {
      const option = element("option", "", camera.name); option.value = camera.id; return option;
    }));
    renderCameras(); changed(); await loadEvents();
  } catch (error) {
    showAlert(error.message); $("camera-name").textContent = "No cameras available";
    $("camera-nav").replaceChildren(element("p", "nav-loading", "Configuration unavailable"));
  }
}
init();
