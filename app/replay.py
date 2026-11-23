"""Neolink's human-readable replay list, validated before exposing recordings."""
import hashlib
import re
from datetime import datetime, timedelta


class AppError(Exception):
    def __init__(self, message, status=502):
        self.message, self.status = message, status
        super().__init__(message)


def event_id(camera_id, name, stream):
    return hashlib.sha256(f"{camera_id}\0{name}\0{stream}".encode()).hexdigest()


def parse_files(output, camera_id, requested_stream):
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    if "No files for this day." in clean:
        return []
    header = re.search(r"^Files \((\d+)\):\s*$", clean, re.M)
    if not header:
        raise AppError("Neolink did not return a readable event list. Check the connection and Neolink version.")
    rows = []
    for line in clean[header.end():].splitlines():
        if not line.strip():
            continue
        parts = re.split(r" {2,}", line.strip())
        if len(parts) < 5:
            raise AppError("The event format returned by this Neolink version is not supported.")
        name, size, start, end, stream = parts[:5]
        if name == "—":
            raise AppError("An event has no filename and cannot be played.")
        if stream not in ("mainStream", "subStream", "externStream", "—"):
            raise AppError("Unknown stream format in the event list.")
        stream = requested_stream if stream == "—" else stream
        record_type = parts[5] if len(parts) > 5 and not parts[5].startswith("[AI:") else ""
        ai = re.search(r"\[AI:\s*(.*?)\]", line)
        tags = [s.strip() for s in record_type.split(",") if s.strip()]
        duration = None
        try:
            duration = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
        except ValueError:
            pass
        rows.append({
            "id": event_id(camera_id, name, stream), "name": name,
            "size": size.strip(), "start": None if start == "—" else start,
            "end": None if end == "—" else end, "stream": stream,
            "tags": tags, "ai": [s.strip() for s in ai.group(1).split(",")] if ai else [],
            "duration": duration,
        })
    if len(rows) != int(header.group(1)):
        raise AppError("The event list is incomplete. Reload it.")
    return sorted(rows, key=lambda row: row["start"] or "", reverse=True)


def demo_events(camera_id, day, stream="subStream", offset_seconds=0):
    base = datetime.fromisoformat(day + "T18:42:00") + timedelta(seconds=offset_seconds)
    rows = []
    for i, tags in enumerate([["md", "people"], ["pir", "vehicle"], ["md", "dog_cat"], ["pir"], ["sched"]]):
        start = base - timedelta(hours=i * 2, minutes=i * 7)
        end = start + timedelta(seconds=18 + i * 6)
        name = f"demo-{day}-{i}"
        rows.append({"id": event_id(camera_id, name, stream), "name": name,
                     "start": start.isoformat(), "end": end.isoformat(),
                     "duration": int((end - start).total_seconds()), "size": f"{2+i} MB",
                     "stream": stream, "tags": tags, "ai": tags[1:]})
    return rows
