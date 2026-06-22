/*
 * Per-conference iCalendar (.ics) generation — browser port of
 * `conference_agent/calendar_sync.py`.
 *
 * The static site has no server, so a row's "📅 cal" button builds the .ics text
 * here and downloads it as a Blob. The output mirrors the Python feed: up to three
 * all-day events for the upcoming edition (abstract deadline, paper deadline,
 * conference dates), each with reminders at the configured lead times, RFC 5545
 * line folding, and a stable base32hex-derived UID per event so re-downloading
 * updates the event in place rather than duplicating it.
 *
 * (The whole-list subscribe feed was dropped in the static migration; only the
 * per-row download remains, which is what this module serves.)
 */

// Mirrors conference_agent/config.py.
const REMINDER_LEAD_DAYS = [28, 7, 1];
const REMINDER_HOUR = 9;

function icsEscape(text) {
  return String(text)
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\r\n/g, "\\n")
    .replace(/\n/g, "\\n");
}

// Fold a content line to <=75 octets (RFC 5545 §3.1), not splitting a multibyte
// UTF-8 sequence; continuation lines begin with a single space.
function icsFold(line) {
  const data = new TextEncoder().encode(line);
  if (data.length <= 75) return line;
  const decoder = new TextDecoder();
  const pieces = [];
  let start = 0;
  let limit = 75;
  while (data.length - start > limit) {
    let end = start + limit;
    while (end > start && (data[end] & 0xc0) === 0x80) end -= 1; // back up over continuation bytes
    pieces.push(data.slice(start, end));
    start = end;
    limit = 74; // continuation lines lose one octet to the leading space
  }
  pieces.push(data.slice(start));
  return pieces.map((p) => decoder.decode(p)).join("\r\n ");
}

// RFC 5545 base32hex (extended hex alphabet), no padding — matches Python's
// base64.b32hexencode(...).lower().rstrip("=").
function base32hexEncode(bytes) {
  const ALPHA = "0123456789abcdefghijklmnopqrstuv";
  let out = "";
  let bits = 0;
  let value = 0;
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += ALPHA[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += ALPHA[(value << (5 - bits)) & 31];
  return out;
}

function eventId(conferenceId, kind) {
  const key = new TextEncoder().encode(`${conferenceId}-${kind}`);
  return `conf${base32hexEncode(key)}`;
}

// TRIGGER for a reminder `days` before an all-day event, anchored to the morning
// so calendar apps label "N days before" correctly (see the Python docstring).
function alarmTrigger(days) {
  const hoursBefore = days * 24 - REMINDER_HOUR;
  const wholeDays = Math.floor(hoursBefore / 24);
  const remHours = hoursBefore - wholeDays * 24;
  if (wholeDays && remHours) return `-P${wholeDays}DT${remHours}H`;
  if (wholeDays) return `-P${wholeDays}D`;
  return `-PT${remHours}H`;
}

const pad = (n, w) => String(n).padStart(w, "0");

// "YYYY-MM-DD" -> "YYYYMMDD".
function compactDate(iso) {
  return iso.replace(/-/g, "");
}

// "YYYY-MM-DD" + 1 day -> "YYYYMMDD" (exclusive all-day DTEND).
function dateEndExclusive(iso) {
  const [y, m, d] = iso.split("-").map((x) => parseInt(x, 10));
  const dt = new Date(Date.UTC(y, m - 1, d + 1));
  return `${pad(dt.getUTCFullYear(), 4)}${pad(dt.getUTCMonth() + 1, 2)}${pad(dt.getUTCDate(), 2)}`;
}

function nowStamp() {
  const d = new Date();
  return (
    `${pad(d.getUTCFullYear(), 4)}${pad(d.getUTCMonth() + 1, 2)}${pad(d.getUTCDate(), 2)}` +
    `T${pad(d.getUTCHours(), 2)}${pad(d.getUTCMinutes(), 2)}${pad(d.getUTCSeconds(), 2)}Z`
  );
}

// The upcoming-edition events a row yields (mirrors _edition_events).
function editionEvents(row) {
  const events = [];
  const acronym = row.acronym || "";
  const label = `${acronym} ${row.name || ""}`;
  const url = row.url ? `\n${row.url}` : "";

  if (row.upcoming_abstract_deadline) {
    events.push({
      kind: "abstract",
      summary: `${acronym} — abstract deadline`,
      start: row.upcoming_abstract_deadline,
      end: row.upcoming_abstract_deadline,
      description: `Abstract submission deadline for ${label}.${url}`,
    });
  }
  if (row.upcoming_paper_deadline) {
    events.push({
      kind: "paper",
      summary: `${acronym} — paper deadline`,
      start: row.upcoming_paper_deadline,
      end: row.upcoming_paper_deadline,
      description: `Full paper / manuscript deadline for ${label}.${url}`,
    });
  }
  if (row.upcoming_start_date) {
    const end = row.upcoming_end_date || row.upcoming_start_date;
    const year = row.upcoming_start_date.split("-")[0];
    events.push({
      kind: "conference",
      summary: `${acronym} ${year}`,
      start: row.upcoming_start_date,
      end,
      description: `${label} conference dates.${url}`,
    });
  }
  return events;
}

/** Render one conference row as a complete iCalendar (.ics) document. */
function conferenceToIcs(row, calendarName = "Conference Agent") {
  const stamp = nowStamp();
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Conference Agent//Conference Calendar//EN",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    `NAME:${icsEscape(calendarName)}`,
    `X-WR-CALNAME:${icsEscape(calendarName)}`,
    "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    "X-PUBLISHED-TTL:PT12H",
  ];

  for (const ev of editionEvents(row)) {
    const uid = `${eventId(row.id, ev.kind)}@conference-agent`;
    lines.push(
      "BEGIN:VEVENT",
      `UID:${uid}`,
      `DTSTAMP:${stamp}`,
      `DTSTART;VALUE=DATE:${compactDate(ev.start)}`,
      `DTEND;VALUE=DATE:${dateEndExclusive(ev.end)}`,
      `SUMMARY:${icsEscape(ev.summary)}`,
      `DESCRIPTION:${icsEscape(ev.description)}`
    );
    if (row.url) lines.push(`URL:${row.url}`);
    lines.push("TRANSP:TRANSPARENT");
    const days = [...new Set(REMINDER_LEAD_DAYS)].sort((a, b) => b - a);
    for (const d of days) {
      lines.push(
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        `DESCRIPTION:${icsEscape(ev.summary)}`,
        `TRIGGER:${alarmTrigger(d)}`,
        "END:VALARM"
      );
    }
    lines.push("END:VEVENT");
  }
  lines.push("END:VCALENDAR");
  return lines.map((l) => icsFold(l) + "\r\n").join("");
}

/** Trigger a browser download of a row's .ics. */
function downloadIcs(row) {
  const ics = conferenceToIcs(row);
  const blob = new Blob([ics], { type: "text/calendar;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${row.id}.ics`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { conferenceToIcs, eventId };
} else {
  window.ConferenceCalendar = { conferenceToIcs, downloadIcs };
}
