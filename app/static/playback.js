"use strict";
window.createNeolinkPlayback = function (options) {
  const { state, dom, api, timeline, focusRecord, changed, cursorChanged, loadEvents, report } = options;
  const video = dom("video");
  let epoch = 0, intent = 0, session = null, hls = null, heartbeat = null;
  let sourceAbort = new AbortController();
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  function visible(mode) {
    dom("video-empty").hidden = mode !== "empty";
    dom("video-loading").hidden = mode !== "loading";
    video.hidden = mode !== "video";
  }
  function loading(title, text) {
    state.loading = true;
    dom("loading-title").textContent = title;
    dom("loading-text").textContent = text;
    visible("loading");
    changed();
  }
  async function stop(reload = false, preserveIntent = false) {
    if (!preserveIntent) ++intent;
    const stoppedEpoch = ++epoch, lease = session;
    session = null;
    clearInterval(heartbeat); heartbeat = null;
    sourceAbort.abort(); sourceAbort = new AbortController();
    state.mode = null; state.activeRecord = null; state.loading = false; state.recordingReady = false;
    state.mediaTime = 0; state.videoDuration = null;
    if (hls) { hls.destroy(); hls = null; }
    video.pause(); video.removeAttribute("src"); video.load();
    visible("empty"); changed();
    if (lease) {
      try { await api("/api/live/" + lease, { method: "DELETE" }); }
      catch (error) { report(error.message + " The session will expire automatically."); }
    }
    if (reload && stoppedEpoch === epoch) await loadEvents();
  }
  function metadata(signal) {
    if (video.readyState >= 1) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error("The video did not load in time.")), 30000);
      function finish(error) {
        clearTimeout(timer);
        video.removeEventListener("loadedmetadata", loaded);
        video.removeEventListener("error", failed);
        signal.removeEventListener("abort", aborted);
        error ? reject(error) : resolve();
      }
      const loaded = () => finish();
      const failed = () => finish(new Error("The video could not be read."));
      const aborted = () => finish(new DOMException("Playback ended", "AbortError"));
      video.addEventListener("loadedmetadata", loaded);
      video.addEventListener("error", failed);
      signal.addEventListener("abort", aborted, { once: true });
      if (signal.aborted) aborted();
    });
  }
  async function seek(seconds, expectedEpoch) {
    await metadata(sourceAbort.signal);
    if (epoch !== expectedEpoch) return;
    video.currentTime = timeline.mediaOffset(seconds, video.duration);
    state.mediaTime = video.currentTime; state.videoDuration = video.duration;
    cursorChanged();
    await video.play().catch(() => report("Click Play in the video player."));
  }
  async function playEvent(row, offset = 0) {
    const camera = state.cameras.find(item => item.id === row.camera_id);
    if (!camera) { report("The camera for this recording is no longer configured."); return; }
    if (state.mode === "event" && state.activeRecord?.id === row.id && !state.loading && !video.hidden) {
      focusRecord(row, offset);
      await seek(offset, epoch);
      return;
    }
    const request = ++intent;
    await stop(false, true);
    if (request !== intent) return;
    const expectedEpoch = epoch;
    state.camera = camera; state.mode = "event"; state.activeRecord = row;
    state.selected = row.id; state.mediaTime = Math.max(0, offset);
    focusRecord(row, offset); report("");
    loading("Preparing recording", "Loading the recording from " + camera.name + " …");
    try {
      let job = await api("/api/cameras/" + row.camera_id + "/events/" + row.id + "/prepare", { method: "POST" });
      while (["queued", "downloading", "converting"].includes(job.state)) {
        if (expectedEpoch !== epoch) return;
        const messages = {
          queued: "Preparing the camera for download …",
          downloading: "Downloading the full recording from " + camera.name + " …",
          converting: "Preparing the video for your browser …"
        };
        dom("loading-text").textContent = messages[job.state];
        await wait(1000);
        if (expectedEpoch !== epoch) return;
        job = await api("/api/jobs/" + job.id);
      }
      if (expectedEpoch !== epoch) return;
      if (job.state === "error") throw new Error(job.error);
      if (job.state !== "ready" || !job.url) throw new Error("The recording is not ready yet.");
      video.muted = true; video.src = job.url;
      await seek(offset, expectedEpoch);
      if (expectedEpoch !== epoch) return;
      state.loading = false; state.recordingReady = true; visible("video"); changed(); cursorChanged();
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
        dom("viewer").scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    } catch (error) {
      if (expectedEpoch !== epoch || error.name === "AbortError") return;
      await stop(); report(error.message);
    }
  }
  async function liveVideo(url, expectedEpoch) {
    video.muted = true;
    state.loading = false; visible("video"); changed();
    if (window.Hls?.isSupported()) {
      hls = new Hls({ liveSyncDurationCount: 3, maxBufferLength: 12 });
      hls.loadSource(url); hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (epoch === expectedEpoch) video.play().catch(() => report("Click Play in the video player."));
      });
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal && epoch === expectedEpoch) {
          stop(); report("The live stream was interrupted. Start it again.");
        }
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      await video.play().catch(() => report("Click Play in the video player."));
    } else throw new Error("This browser does not support HLS. Use a current browser.");
  }
  async function startLive() {
    const camera = state.camera;
    if (!camera || !camera.live_available) return;
    const request = ++intent;
    await stop(false, true);
    if (request !== intent) return;
    const expectedEpoch = epoch;
    state.mode = "live"; report("");
    loading("Starting live view", "Connecting to " + camera.name + " …");
    try {
      let live = await api("/api/cameras/" + camera.id + "/live", { method: "POST" });
      if (expectedEpoch !== epoch) {
        await api("/api/live/" + live.session, { method: "DELETE" }); return;
      }
      session = live.session;
      while (live.state === "starting") {
        await wait(1000);
        if (expectedEpoch !== epoch) return;
        live = await api("/api/live/" + session + "/heartbeat", { method: "POST" });
      }
      if (expectedEpoch !== epoch) return;
      if (live.state === "error") throw new Error(live.error);
      heartbeat = setInterval(async () => {
        const lease = session;
        if (!lease) return;
        try {
          const current = await api("/api/live/" + lease + "/heartbeat", { method: "POST" });
          if (lease !== session) return;
          if (current.state === "error") throw new Error(current.error);
        } catch (error) {
          if (lease === session) { await stop(); report(error.message); }
        }
      }, 10000);
      await liveVideo(live.url, expectedEpoch);
    } catch (error) {
      if (expectedEpoch !== epoch) return;
      await stop(); report(error.message);
    }
  }
  async function selectCamera(camera) {
    const request = ++intent;
    await stop(false, true);
    if (request !== intent) return;
    state.camera = camera; changed();
  }
  video.addEventListener("timeupdate", () => {
    if (state.mode !== "event" || state.loading) return;
    state.mediaTime = video.currentTime; state.videoDuration = video.duration;
    cursorChanged();
  });
  video.addEventListener("loadedmetadata", () => {
    state.videoDuration = video.duration;
    cursorChanged();
  });
  video.addEventListener("pause", () => {
    if (state.mode === "live" && session) stop(true);
    else if (state.mode === "event" && !state.loading) changed();
  });
  video.addEventListener("playing", () => { if (state.mode) changed(); });
  video.addEventListener("ended", () => {
    if (state.mode === "live") stop(true);
    else if (state.mode === "event") { changed(); cursorChanged(); }
  });
  video.addEventListener("error", () => {
    if (state.mode && !state.loading) { stop(); report("The video could not be played."); }
  });
  window.addEventListener("pagehide", () => { if (session) navigator.sendBeacon("/api/live/" + session + "/stop"); });
  return { playEvent, startLive, stop, selectCamera };
};
