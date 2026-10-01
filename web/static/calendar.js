/*
 * Per-conference iCalendar (.ics) generation — browser port of
 * `conference_agent/calendar_sync.py`.
 *
 * The static site has no server, so a row's "📅 cal" button builds the .ics text
 * here and downloads it as a Blob. The output mirrors the Python feed: up to four
 * all-day events for the upcoming edition (abstract deadline, late abstract
 * deadline, paper deadline, conference dates), each with reminders at the
 * configured lead times, RFC 5545
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

// Labels a multi-line deadline_time may prefix each entry with, by event kind.
const DEADLINE_TIME_LABELS = {
  abstract: ["abstract"],
  "late-abstract": ["late abstract", "late-abstract", "late_abstract"],
  paper: ["paper"],
};

// The deadline-time text that applies to one event kind (mirrors
// calendar_sync.deadline_time_for): one shared value, or the matching
// "kind: time" entry (newline- or semicolon-separated), or null.
function deadlineTimeFor(deadlineTime, kind) {
  if (!deadlineTime || !deadlineTime.trim()) return null;
  const entries = deadlineTime.split(/[\n;]/).map((e) => e.trim()).filter(Boolean);
  const labeled = {};
  for (const entry of entries) {
    const m = entry.match(/^\s*([A-Za-z][A-Za-z _-]*?)\s*:\s*(.+)$/);
    if (m) labeled[m[1].trim().toLowerCase()] = m[2].trim();
  }
  if (Object.keys(labeled).length === 0) return deadlineTime.trim();
  for (const label of DEADLINE_TIME_LABELS[kind] || []) {
    if (label in labeled) return labeled[label];
  }
  return null;
}

// Description line carrying the deadline time; the event itself stays all-day.
function deadlineNote(row, kind) {
  const t = deadlineTimeFor(row.deadline_time, kind);
  return t ? `\nDeadline time: ${t}` : "";
}

// Time zones a free-text deadline time may name, tried in order. `offset` is a
// fixed UTC offset in minutes; `zone` is an IANA zone. Standard/daylight
// abbreviations for regions that observe DST (EST/EDT, CET/CEST, ...) resolve
// to the region's wall clock rather than a fixed offset: organizers often keep
// writing "EST" or "CET" through the summer while meaning local time.
const DEADLINE_ZONES = [
  { re: /\bAoE\b|anywhere on earth/i, offset: -12 * 60 },
  { re: /\b(?:UTC|GMT)\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?\b/i, signed: true },
  { re: /\b(?:UTC|GMT|Z)\b/, offset: 0 },
  { re: /\bE[SD]?T\b|\beastern\b/i, zone: "America/New_York" },
  { re: /\bC[SD]?T\b|\bcentral(?! europe)\b/i, zone: "America/Chicago" },
  { re: /\bM[SD]?T\b|\bmountain\b/i, zone: "America/Denver" },
  { re: /\bP[SD]?T\b|\bpacific\b/i, zone: "America/Los_Angeles" },
  { re: /\bAK[SD]?T\b|\balaska\b/i, zone: "America/Anchorage" },
  { re: /\bHST\b|\bhawaii\b/i, zone: "Pacific/Honolulu" },
  { re: /\bCES?T\b|\bcentral europe/i, zone: "Europe/Berlin" },
  { re: /\bWES?T\b/, zone: "Europe/Lisbon" },
  { re: /\bEES?T\b/, zone: "Europe/Athens" },
  { re: /\bBST\b|\bUK time\b|\bLondon\b/i, zone: "Europe/London" },
  { re: /\bIST\b/, zone: "Asia/Kolkata" },
  { re: /\bSGT\b/, zone: "Asia/Singapore" },
  { re: /\bJST\b/, zone: "Asia/Tokyo" },
  { re: /\bKST\b/, zone: "Asia/Seoul" },
  { re: /\bAE[SD]T\b/, zone: "Australia/Sydney" },
  { re: /\bBRT\b/, zone: "America/Sao_Paulo" },
];

// UTC offset (minutes) of an IANA zone at a given instant (ms).
function zoneOffset(timeZone, ms) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(ms));
  const p = Object.fromEntries(parts.map((x) => [x.type, Number(x.value)]));
  return (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - ms) / 60000;
}

// Parse a free-text deadline time ("11:59 PM ET", "23:59 AoE", "Noon PT") into
// {hour, minute, offsetAt(ms)}, or null when it names no time or no zone.
// Parenthetical asides are ignored unless the zone appears only there
// ("11:59 PM CDT (UTC-5)", "AoE (12:00 noon UTC the following day)").
function parseDeadlineTime(text) {
  const main = text.replace(/\([^)]*\)/g, " ");
  let zoneSpec = null;
  for (const source of [main, text]) {
    for (const z of DEADLINE_ZONES) {
      const m = source.match(z.re);
      if (!m) continue;
      if (z.signed) {
        const sign = m[1] === "+" ? 1 : -1;
        zoneSpec = { offset: sign * (Number(m[2]) * 60 + Number(m[3] || 0)) };
      } else {
        zoneSpec = z;
      }
      break;
    }
    if (zoneSpec) break;
  }
  if (!zoneSpec) return null;

  let hour = null;
  let minute = 0;
  const t = main.match(/\b(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)?(?![\d.])/i);
  if (t && (t[2] !== undefined || t[3])) {
    hour = Number(t[1]);
    minute = Number(t[2] || 0);
    const ampm = (t[3] || "").toLowerCase().replace(/\./g, "");
    if (ampm === "pm" && hour < 12) hour += 12;
    if (ampm === "am" && hour === 12) hour = 0;
  } else if (/\bnoon\b/i.test(main)) {
    hour = 12;
  } else if (/\bmidnight\b|\bend of (?:the )?day\b|\bEOD\b/i.test(main) || /\bAoE\b/i.test(main)) {
    // A deadline at "midnight" means the end of the stated day; a bare "AoE"
    // conventionally means 23:59 AoE.
    hour = 23;
    minute = 59;
  }
  if (hour === null || hour > 23 || minute > 59) return null;
  const offsetAt = zoneSpec.zone
    ? (ms) => zoneOffset(zoneSpec.zone, ms)
    : () => zoneSpec.offset;
  return { hour, minute, offsetAt };
}

// The viewer's local equivalent of a deadline time on `dateIso` (YYYY-MM-DD),
// e.g. "2:59 PM PDT" or "5:00 AM PDT the next day"; null when the text cannot
// be parsed or the viewer is already in that offset. `timeZone` overrides the
// viewer's zone (for tests).
function localDeadlineTime(text, dateIso, timeZone) {
  if (!text || !dateIso) return null;
  const parsed = parseDeadlineTime(text);
  if (!parsed) return null;
  const [y, mo, d] = dateIso.split("-").map(Number);
  const wall = Date.UTC(y, mo - 1, d, parsed.hour, parsed.minute);
  // Wall-clock time in the source zone → instant, re-checked once across a DST edge.
  let instant = wall - parsed.offsetAt(wall) * 60000;
  instant = wall - parsed.offsetAt(instant) * 60000;

  const viewerZone = timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (zoneOffset(viewerZone, instant) === parsed.offsetAt(instant)) return null;
  const time = new Date(instant).toLocaleTimeString("en-US", {
    timeZone: viewerZone, hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
  const localDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: viewerZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date(instant));
  const dayDiff = Math.round((Date.parse(localDate) - Date.UTC(y, mo - 1, d)) / 86400000);
  if (dayDiff === 1) return `${time} the next day`;
  if (dayDiff === -1) return `${time} the previous day`;
  return time;
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
      description: `Abstract submission deadline for ${label}.${deadlineNote(row, "abstract")}${url}`,
    });
  }
  if (row.upcoming_late_abstract_deadline) {
    events.push({
      kind: "late-abstract",
      summary: `${acronym} — late abstract deadline`,
      start: row.upcoming_late_abstract_deadline,
      end: row.upcoming_late_abstract_deadline,
      description:
        `Late abstract deadline (poster-only or late-breaking round) ` +
        `for ${label}.${deadlineNote(row, "late-abstract")}${url}`,
    });
  }
  if (row.upcoming_paper_deadline) {
    events.push({
      kind: "paper",
      summary: `${acronym} — paper deadline`,
      start: row.upcoming_paper_deadline,
      end: row.upcoming_paper_deadline,
      description: `Full paper / manuscript deadline for ${label}.${deadlineNote(row, "paper")}${url}`,
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
  module.exports = { conferenceToIcs, eventId, localDeadlineTime };
} else {
  window.ConferenceCalendar = { conferenceToIcs, downloadIcs, localDeadlineTime };
}
