/* Camera-local wall clock: no browser timezone or daylight-saving conversion. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.NeolinkTimeline = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const DAY = 86400;
  function dayNumber(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!m) return null;
    const date = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    if (date.getUTCFullYear() !== Number(m[1]) || date.getUTCMonth() !== Number(m[2]) - 1 ||
        date.getUTCDate() !== Number(m[3])) return null;
    return date.getTime() / 86400000;
  }
  function seconds(timestamp, day) {
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(timestamp || "");
    const reference = dayNumber(day), actual = match && dayNumber(match[1]);
    if (!match || reference === null || actual === null) return null;
    const h = Number(match[2]), m = Number(match[3]), s = Number(match[4]);
    if (h > 23 || m > 59 || s > 59) return null;
    return (actual - reference) * DAY + h * 3600 + m * 60 + s;
  }
  function bounds(row, day) {
    const start = seconds(row.start, day);
    if (start === null) return null;
    let end = seconds(row.end, day);
    if (end === null && Number.isFinite(row.duration) && row.duration > 0) end = start + row.duration;
    if (end === null || end < start) end = start;
    return { start, end };
  }
  function intersects(row, day, from, to) {
    const b = bounds(row, day);
    return b !== null && b.start < to && (b.end > from || b.start >= from);
  }
  function groups(rows, day, from, to, width, target = 28) {
    if (!(to > from) || !(width > 0)) return [];
    const sorted = rows.filter(row => intersects(row, day, from, to))
      .map(row => ({ row, b: bounds(row, day) })).sort((a, b) => a.b.start - b.b.start || a.b.end - b.b.end);
    const result = [];
    for (const item of sorted) {
      const startPixel = (Math.max(from, item.b.start) - from) / (to - from) * width;
      const endPixel = (Math.min(to, item.b.end) - from) / (to - from) * width;
      const hitStart = Math.max(0, Math.min(width - target, startPixel - target / 2));
      const hitEnd = Math.min(width, Math.max(hitStart + target, endPixel));
      const previous = result[result.length - 1];
      if (previous && hitStart < previous.hitEnd + 2) {
        previous.events.push(item.row);
        previous.hitEnd = Math.max(previous.hitEnd, hitEnd);
        previous.from = Math.min(previous.from, item.b.start);
        previous.to = Math.max(previous.to, item.b.end);
      } else result.push({ events: [item.row], hitStart, hitEnd, from: item.b.start, to: item.b.end });
    }
    return result;
  }
  function seekOffset(row, day, timestamp) {
    const b = bounds(row, day);
    if (!b || !Number.isFinite(timestamp)) return 0;
    return Math.max(0, Math.min(timestamp - b.start, Math.max(0, b.end - b.start)));
  }
  function mediaOffset(offset, duration) {
    const value = Number.isFinite(offset) ? Math.max(0, offset) : 0;
    return Number.isFinite(duration) && duration > 0 ? Math.min(value, Math.max(0, duration - 0.01)) : value;
  }
  function cursor(row, day, elapsed) {
    const b = bounds(row, day);
    if (!b || !Number.isFinite(elapsed)) return null;
    const value = b.start + Math.max(0, elapsed);
    return b.end > b.start ? Math.min(b.end, value) : value;
  }
  function clock(value, withSeconds = false) {
    if (!Number.isFinite(value)) return "—";
    if (value === DAY && !withSeconds) return "24:00";
    const total = Math.floor(((value % DAY) + DAY) % DAY);
    const h = String(Math.floor(total / 3600)).padStart(2, "0");
    const m = String(Math.floor(total / 60) % 60).padStart(2, "0");
    return h + ":" + m + (withSeconds ? ":" + String(total % 60).padStart(2, "0") : "");
  }
  function timestamp(day, value) {
    const number = dayNumber(day);
    if (number === null || !Number.isFinite(value)) return null;
    return new Date((number * DAY + Math.floor(value)) * 1000).toISOString().slice(0, 19);
  }
  function dateClock(day, value, withSeconds = false) {
    const stamp = timestamp(day, value);
    return stamp ? stamp.slice(0, 10) + " " + clock(((value % DAY) + DAY) % DAY, withSeconds) : "—";
  }
  function clampWindow(span, center, lower, upper) {
    return Math.max(lower, Math.min(upper - span, Math.floor(center - span / 2)));
  }
  return { DAY, dayNumber, seconds, bounds, intersects, groups, seekOffset, mediaOffset, cursor, clock,
    timestamp, dateClock, clampWindow };
});
