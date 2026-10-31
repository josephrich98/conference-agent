/*
 * Per-conference iCalendar (.ics) generation — browser port of
 * `conference_agent/calendar_sync.py`.
 *
 * The static site has no server, so a row's "📅 cal" button builds the .ics text
 * here and downloads it as a Blob. The output mirrors the Python feed: up to four
 * all-day events for the upcoming edition, or the prior one when no upcoming
 * date is known (abstract deadline, late abstract
 * deadline, paper deadline, conference dates), each with reminders at the
 * configured lead times, RFC 5545
 * line folding, and a stable base32hex-derived UID per event so re-downloading
 * updates the event in place rather than duplicating it.
 *
 * Subscribable feeds are static files written by `scripts/build_static.py` from
 * the Python builder (`/c/<id>/calendar.ics`, `/field/<tag>/calendar.ics`,
 * `/calendar.ics`); this module serves the one-time download and their URLs.
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

// The three deadline kinds, in display order. Each row carries a 24-hour
// "HH:MM" time and a zone code per kind (`abstract_time` / `abstract_timezone`,
// ...), the stored source of truth the "Deadline time" text is derived from.
const DEADLINE_KINDS = ["abstract", "late_abstract", "paper"];

// Zone codes a deadline may use, each a fixed UTC offset (minutes) or an IANA
// zone. Region codes (ET, CET, ...) name the region's wall clock, so they follow
// daylight time. Mirrors TIMEZONES in conference_agent/deadline_time.py (a test
// pins the two together). A "UTC+9" offset or any IANA name is also accepted.
const DEADLINE_ZONES = {
  AoE: { offset: -12 * 60 },
  UTC: { offset: 0 },
  ET: { zone: "America/New_York" },
  CT: { zone: "America/Chicago" },
  MT: { zone: "America/Denver" },
  PT: { zone: "America/Los_Angeles" },
  AKT: { zone: "America/Anchorage" },
  HST: { zone: "Pacific/Honolulu" },
  CET: { zone: "Europe/Berlin" },
  WET: { zone: "Europe/Lisbon" },
  EET: { zone: "Europe/Athens" },
  UK: { zone: "Europe/London" },
  IST: { zone: "Asia/Kolkata" },
  SGT: { zone: "Asia/Singapore" },
  JST: { zone: "Asia/Tokyo" },
  KST: { zone: "Asia/Seoul" },
  AEST: { zone: "Australia/Sydney" },
  BRT: { zone: "America/Sao_Paulo" },
};

// UTC offset (minutes) of an IANA zone at a given instant (ms).
function zoneOffset(timeZone, ms) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(ms));
  const p = Object.fromEntries(parts.map((x) => [x.type, Number(x.value)]));
  return (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - ms) / 60000;
}

// A zone code -> a function giving its UTC offset (minutes) at an instant, or
// null when the code is unknown.
function zoneOffsetFn(code) {
  if (!code) return null;
  const known = DEADLINE_ZONES[code];
  if (known) return known.zone ? (ms) => zoneOffset(known.zone, ms) : () => known.offset;
  const m = code.match(/^UTC([+-])(\d{1,2})(?::(\d{2}))?$/);
  if (m) {
    const minutes = (m[1] === "+" ? 1 : -1) * (Number(m[2]) * 60 + Number(m[3] || 0));
    return () => minutes;
  }
  if (code.includes("/")) {
    try {
      zoneOffset(code, 0);
      return (ms) => zoneOffset(code, ms);
    } catch (e) { /* unknown IANA name */ }
  }
  return null;
}

// The instant (ms) a deadline on `dateIso` (YYYY-MM-DD) has passed: the end of
// its stated minute in its zone. With no time it is the end of that day, and
// with no usable zone the zone is AoE (UTC-12) -- the latest moment the day ends
// anywhere, so a date is never treated as passed while it is still the day
// somewhere. `time` is 24-hour "HH:MM".
function deadlineInstant(dateIso, time, tz) {
  const [y, mo, d] = dateIso.split("-").map(Number);
  const t = /^(\d{1,2}):(\d{2})$/.exec(time || "");
  const wall = t
    ? Date.UTC(y, mo - 1, d, Number(t[1]), Number(t[2]) + 1)
    : Date.UTC(y, mo - 1, d + 1, 0, 0);
  const offsetAt = zoneOffsetFn(tz) || (() => -12 * 60);
  // Wall-clock time in the source zone -> instant, re-checked once across a DST edge.
  let instant = wall - offsetAt(wall) * 60000;
  instant = wall - offsetAt(instant) * 60000;
  return instant;
}

// "11:59 PM" (default) or "23:59" for a stored 24-hour "HH:MM".
function formatDeadlineClock(time, military) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(time || "");
  if (!m) return time || "";
  const h = Number(m[1]);
  if (military) return `${String(h).padStart(2, "0")}:${m[2]}`;
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? "AM" : "PM"}`;
}

// One deadline's time as shown: "8:00 PM AoE" / "20:00 AoE", or "" when none.
function formatDeadlineSpec(time, tz, military) {
  return [time ? formatDeadlineClock(time, military) : "", tz || ""].filter(Boolean).join(" ");
}

// A row's (time, zone) for one deadline kind.
function deadlineSpec(row, kind) {
  return { time: row[`${kind}_time`] || null, tz: row[`${kind}_timezone`] || null };
}

// The entries the "Deadline time" cell shows, collapsing identical times into
// one unlabeled entry ("8:00 PM ET", not "abstract: 8:00 PM ET / paper: ...").
// A kind is considered when it has a time or any deadline date; when every
// considered kind shares one non-empty spec there is a single entry with
// `kind: null`, otherwise one labeled entry per kind that has a time. Mirrors
// deadline_entries in conference_agent/deadline_time.py.
function deadlineEntries(row) {
  const specs = Object.fromEntries(DEADLINE_KINDS.map((k) => [k, deadlineSpec(row, k)]));
  const present = DEADLINE_KINDS.filter((k) => specs[k].time || specs[k].tz);
  const dated = (k) => row[`upcoming_${k}_deadline`] || row[`prior_${k}_deadline`];
  const considered = DEADLINE_KINDS.filter((k) => present.includes(k) || dated(k));
  const keys = new Set(considered.map((k) => `${specs[k].time || ""}|${specs[k].tz || ""}`));
  if (keys.size === 1 && !keys.has("|")) return [{ kind: null, ...specs[considered[0]] }];
  return present.map((k) => ({ kind: k, ...specs[k] }));
}

// The derived text for a row ("11:59 PM ET", or labeled lines joined by "\n");
// the same string the server stores as `deadline_time` when not military.
function deadlineTimeText(row, military) {
  return deadlineEntries(row)
    .map((e) => {
      const text = formatDeadlineSpec(e.time, e.tz, military);
      return e.kind ? `${e.kind.replace("_", " ")}: ${text}` : text;
    })
    .join("\n");
}

// The deadline time that applies to one event kind ("abstract", "late-abstract",
// "paper"), as shown, or null. Mirrors calendar_sync.deadline_time_for.
function deadlineTimeFor(row, kind) {
  const spec = deadlineSpec(row, kind.replace("-", "_"));
  return formatDeadlineSpec(spec.time, spec.tz, false) || null;
}

// Description line carrying the deadline time; the event itself stays all-day.
function deadlineNote(row, kind) {
  const t = deadlineTimeFor(row, kind);
  return t ? `\nDeadline time: ${t}` : "";
}

// The viewer's local equivalent of a deadline time on `dateIso` (YYYY-MM-DD),
// e.g. "2:59 PM PDT" or "5:00 AM PDT the next day" ("14:59 PDT" when `military`);
// null when there is no time or zone, or the viewer is already in that offset.
// `timeZone` overrides the viewer's zone (for tests).
function localDeadlineTime(time, tz, dateIso, timeZone, military) {
  const t = /^(\d{1,2}):(\d{2})$/.exec(time || "");
  const offsetAt = zoneOffsetFn(tz);
  if (!t || !offsetAt || !dateIso) return null;
  const [y, mo, d] = dateIso.split("-").map(Number);
  const wall = Date.UTC(y, mo - 1, d, Number(t[1]), Number(t[2]));
  // Wall-clock time in the source zone -> instant, re-checked once across a DST edge.
  let instant = wall - offsetAt(wall) * 60000;
  instant = wall - offsetAt(instant) * 60000;

  const viewerZone = timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (zoneOffset(viewerZone, instant) === offsetAt(instant)) return null;
  const clock = new Date(instant).toLocaleTimeString("en-US", {
    timeZone: viewerZone, hour: military ? "2-digit" : "numeric", minute: "2-digit",
    hourCycle: military ? "h23" : "h12", timeZoneName: "short",
  });
  const localDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: viewerZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date(instant));
  const dayDiff = Math.round((Date.parse(localDate) - Date.UTC(y, mo - 1, d)) / 86400000);
  if (dayDiff === 1) return `${clock} the next day`;
  if (dayDiff === -1) return `${clock} the previous day`;
  return clock;
}

const EDITION_DATES = [
  "abstract_deadline",
  "late_abstract_deadline",
  "paper_deadline",
  "start_date",
  "end_date",
];

// "upcoming", or "prior" when no upcoming date is known (mirrors calendar_edition).
function calendarEdition(row) {
  return EDITION_DATES.some((d) => row[`upcoming_${d}`]) ? "upcoming" : "prior";
}

// The edition's events a row yields (mirrors _edition_events).
function editionEvents(row) {
  const events = [];
  const acronym = row.acronym || "";
  const label = `${acronym} ${row.name || ""}`;
  const url = row.url ? `\n${row.url}` : "";
  const ed = calendarEdition(row);
  const get = (field) => row[`${ed}_${field}`];

  if (get("abstract_deadline")) {
    events.push({
      kind: "abstract",
      summary: `${acronym} — abstract deadline`,
      start: get("abstract_deadline"),
      end: get("abstract_deadline"),
      description: `Abstract submission deadline for ${label}.${deadlineNote(row, "abstract")}${url}`,
    });
  }
  if (get("late_abstract_deadline")) {
    events.push({
      kind: "late-abstract",
      summary: `${acronym} — late abstract deadline`,
      start: get("late_abstract_deadline"),
      end: get("late_abstract_deadline"),
      description:
        `Late abstract deadline (poster-only or late-breaking round) ` +
        `for ${label}.${deadlineNote(row, "late-abstract")}${url}`,
    });
  }
  if (get("paper_deadline")) {
    events.push({
      kind: "paper",
      summary: `${acronym} — paper deadline`,
      start: get("paper_deadline"),
      end: get("paper_deadline"),
      description: `Full paper / manuscript deadline for ${label}.${deadlineNote(row, "paper")}${url}`,
    });
  }
  if (get("start_date")) {
    const start = get("start_date");
    events.push({
      kind: "conference",
      summary: `${acronym} ${start.split("-")[0]}`,
      start,
      end: get("end_date") || start,
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

/**
 * Subscribable feed URLs for one conference: the static `/c/<id>/calendar.ics`
 * that `scripts/build_static.py` writes, as https, as webcal (opens the system
 * calendar app), and as a Google Calendar "add by URL" link.
 */
function feedUrls(id, origin) {
  const https = `${origin}/c/${encodeURIComponent(id)}/calendar.ics`;
  const webcal = https.replace(/^https?:/, "webcal:");
  return {
    https,
    webcal,
    google: `https://calendar.google.com/calendar/r?cid=${encodeURIComponent(webcal)}`,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    conferenceToIcs, eventId, localDeadlineTime, deadlineInstant, deadlineEntries,
    deadlineTimeText, deadlineTimeFor, DEADLINE_ZONES, feedUrls,
  };
} else {
  window.ConferenceCalendar = {
    conferenceToIcs, downloadIcs, localDeadlineTime, deadlineInstant, deadlineEntries,
    formatDeadlineSpec, feedUrls,
  };
}
