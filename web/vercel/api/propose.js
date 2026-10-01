// POST /api/propose  {record, submitter?, website?}
//
// The website's "Add a conference" form. `record` is a `conference-agent add
// --json` record (keys from data/add_fields.json, i.e. `add --fields json`).
// After validation it is committed to a new branch as
// `submissions/<id>-<stamp>.json` and a pull request is opened against the
// repository, so nothing reaches the table until a maintainer merges it and
// runs `conference-agent add --json` on the file.
//
// Needs GITHUB_TOKEN: a fine-grained token on the repository with Contents and
// Pull requests read/write. GITHUB_REPO (owner/name) and GITHUB_BASE (branch)
// override the defaults below.

import crypto from "node:crypto";
import { allowAction, nameId, origin } from "./_lib.js";

const REPO = process.env.GITHUB_REPO || "josephrich98/conference-agent";
const BASE = process.env.GITHUB_BASE || "main";
// Submissions per client address, and in total, per day.
const MAX_PER_ADDRESS_PER_DAY = 10;
const MAX_PER_DAY = 100;
// Fields the form never sends: renaming is an update, and deadline_time is a
// shorthand for the structured *_time / *_timezone fields the form collects.
const EXCLUDED = new Set(["new_conference_name", "deadline_time"]);
const MAX_TEXT = 2000;
const MAX_TAGS = 20;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const URL_FIELDS = new Set(["url", "attendance_source"]);
// Deadline extension history: the kinds, and how many entries / how far back
// (matches database.EXTENSION_RETENTION_YEARS; older entries are not kept).
const EXTENSION_KINDS = ["abstract", "late_abstract", "paper"];
const MAX_EXTENSIONS = 20;
const EXTENSION_RETENTION_YEARS = 5;

class InputError extends Error {}

let schema = null;
let catalog = null;
async function siteData(site) {
  if (!schema || !catalog) {
    const [s, c] = await Promise.all([
      fetch(`${site}/data/add_fields.json`),
      fetch(`${site}/data/conferences.json`),
    ]);
    if (!s.ok || !c.ok) throw new Error(`site data fetch failed: ${s.status} / ${c.status}`);
    schema = await s.json();
    const payload = await c.json();
    catalog = new Map((payload.conferences || []).map((r) => [r.id, r]));
  }
  return { schema, catalog };
}

function isDate(value) {
  if (!DATE_RE.test(value)) return false;
  const d = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === value;
}

function isHttpUrl(value) {
  try {
    const u = new URL(value);
    return (u.protocol === "http:" || u.protocol === "https:") && u.hostname.includes(".");
  } catch (e) {
    return false;
  }
}

// The oldest extended date kept, as YYYY-MM-DD.
function extensionCutoff() {
  const d = new Date();
  d.setUTCFullYear(d.getUTCFullYear() - EXTENSION_RETENTION_YEARS);
  return d.toISOString().slice(0, 10);
}

function extensions(value) {
  if (!Array.isArray(value)) throw new InputError("deadline extensions: expected a list.");
  if (value.length > MAX_EXTENSIONS) throw new InputError("Too many deadline extensions.");
  const cutoff = extensionCutoff();
  return value.map((entry) => {
    if (!entry || typeof entry !== "object") throw new InputError("deadline extensions: malformed entry.");
    const type = String(entry.type || "").trim().toLowerCase();
    const original = String(entry.original || "").trim();
    const extended = String(entry.extended || "").trim();
    if (!EXTENSION_KINDS.includes(type)) throw new InputError("deadline extensions: unknown submission type.");
    if (!isDate(original) || !isDate(extended)) throw new InputError("deadline extensions: give an original and an extended date.");
    if (extended <= original) throw new InputError("deadline extensions: the extended date must be after the original.");
    if (extended < cutoff) throw new InputError(`deadline extensions: entries more than ${EXTENSION_RETENTION_YEARS} years old are not kept.`);
    return { type, original, extended };
  });
}

function text(name, value) {
  const s = String(value).trim();
  if (s.length > MAX_TEXT) throw new InputError(`${name} is too long.`);
  return s;
}

// Validate and normalize one submitted record against the `add` vocabulary.
// Mirrors the CLI's checks so a merged file ingests cleanly.
function cleanRecord(raw, schema) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new InputError("Missing record.");
  const fields = new Map(schema.fields.filter((f) => !EXCLUDED.has(f.name)).map((f) => [f.name, f]));
  const out = {};
  for (const [name, value] of Object.entries(raw)) {
    const f = fields.get(name);
    if (!f) throw new InputError(`Unknown field: ${name}.`);
    if (value === null || value === "" || (Array.isArray(value) && !value.length)) continue;
    const label = name.replace(/_/g, " ");
    if (f.kind === "tags") {
      const list = (Array.isArray(value) ? value : String(value).split(","))
        .map((t) => text(label, t))
        .filter(Boolean);
      if (list.length > MAX_TAGS) throw new InputError(`Too many ${label} values.`);
      if (name === "format") {
        const bad = list.filter((t) => !schema.formats.includes(t.toLowerCase()));
        if (bad.length) throw new InputError(`Unknown format: ${bad.join(", ")}.`);
      }
      if (list.length) out[name] = list;
    } else if (f.kind === "dates") {
      const list = (Array.isArray(value) ? value : String(value).split(/\s+/)).map((d) => String(d).trim()).filter(Boolean);
      if (list.length > 2 || !list.every(isDate)) throw new InputError(`${label}: give a start and optional end date.`);
      if (list.length === 2 && list[1] < list[0]) throw new InputError(`${label}: the end date is before the start date.`);
      if (list.length) out[name] = list;
    } else if (f.kind === "extensions") {
      const list = extensions(value);
      if (list.length) out[name] = list;
    } else if (f.kind === "date") {
      const s = text(label, value);
      if (!isDate(s)) throw new InputError(`${label}: not a valid date.`);
      out[name] = s;
    } else if (f.kind === "int") {
      const s = text(label, value);
      if (!/^\d+$/.test(s)) throw new InputError(`${label}: enter a whole number.`);
      const n = Number(s);
      if (name === "attendance" && n < 1) throw new InputError("Attendance must be at least 1.");
      if (name === "attendance_year" && (n < 1900 || n > new Date().getUTCFullYear())) {
        throw new InputError("Attendance year must be between 1900 and this year.");
      }
      out[name] = n;
    } else if (f.kind === "bool") {
      const s = String(value).trim().toLowerCase();
      if (!["true", "false"].includes(s)) throw new InputError(`${label}: must be true or false.`);
      out[name] = s === "true";
    } else if (f.kind === "enum") {
      const s = text(label, value).toLowerCase();
      if (!schema.remote_options.includes(s)) throw new InputError(`${label}: unknown option.`);
      out[name] = s;
    } else {
      const s = text(label, value);
      if (!s) continue;
      if (URL_FIELDS.has(name) && !isHttpUrl(s)) throw new InputError(`${label}: enter an http(s) link.`);
      if (/_time$/.test(name) && !TIME_RE.test(s)) throw new InputError(`${label}: use HH:MM (24-hour).`);
      out[name] = s;
    }
  }
  if (!out.conference_name) throw new InputError("Conference name is required.");
  if (out.conference_name.length > 300) throw new InputError("Conference name is too long.");
  return out;
}

async function github(path, init = {}) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new Error("GITHUB_TOKEN is not set");
  const resp = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "conference-agent",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`GitHub ${init.method || "GET"} ${path}: ${resp.status} ${data.message || ""}`);
  return data;
}

// Text a visitor typed, made safe to show inline in the pull request body:
// no backticks (it sits in a code span) and no line breaks.
const inline = (s) => String(s).replace(/[`\r\n]+/g, " ").trim().slice(0, 200);

async function openPullRequest(record, submitter) {
  const id = nameId(record.conference_name).slice(0, 80) || "conference";
  const stamp = `${new Date().toISOString().slice(0, 10)}-${crypto.randomBytes(3).toString("hex")}`;
  const branch = `submission/${id}-${stamp}`;
  const path = `submissions/${id}-${stamp}.json`;
  const json = JSON.stringify(record, null, 2) + "\n";
  const label = record.conference_acronym
    ? `${record.conference_acronym} — ${record.conference_name}`
    : record.conference_name;

  const base = await github(`/git/ref/heads/${encodeURIComponent(BASE)}`);
  await github("/git/refs", {
    method: "POST",
    body: JSON.stringify({ ref: `refs/heads/${branch}`, sha: base.object.sha }),
  });
  await github(`/contents/${path}`, {
    method: "PUT",
    body: JSON.stringify({
      message: `Add conference submission: ${inline(label)}`,
      content: Buffer.from(json, "utf8").toString("base64"),
      branch,
    }),
  });
  const body = [
    "Submitted through the website's **Add a conference** form.",
    "",
    submitter ? `Submitted by: \`${inline(submitter)}\`` : "Submitted anonymously.",
    "",
    "```json",
    json.trimEnd(),
    "```",
    "",
    "After merging, add it to the database with:",
    "",
    "```bash",
    `conference-agent add --json ${path}`,
    "```",
  ].join("\n");
  const pr = await github("/pulls", {
    method: "POST",
    body: JSON.stringify({ title: `Add conference: ${inline(label)}`, head: branch, base: BASE, body }),
  });
  // Best effort: the label is created on first use.
  await github(`/issues/${pr.number}/labels`, {
    method: "POST",
    body: JSON.stringify({ labels: ["conference-submission"] }),
  }).catch((e) => console.warn("label failed:", e.message));
  return pr;
}

function clientAddress(req) {
  const fwd = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim();
  return fwd || req.headers["x-real-ip"] || "unknown";
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  const body = req.body && typeof req.body === "object" ? req.body : {};
  // Honeypot: the form hides this input, so only bots fill it in. Reply as if
  // it worked so they get no signal.
  if (body.website) return res.status(200).json({ status: "ok", message: "Thanks!" });

  try {
    const site = origin(req);
    const { schema, catalog } = await siteData(site);
    const record = cleanRecord(body.record, schema);
    const existing = catalog.get(nameId(record.conference_name));
    if (existing) {
      return res.status(409).json({
        error: `${existing.name} is already listed.`,
        url: `${site}/c/${existing.id}/`,
      });
    }
    const submitter = body.submitter ? text("Your name", body.submitter).slice(0, 200) : "";

    const who = crypto.createHash("sha256").update(clientAddress(req)).digest("hex").slice(0, 32);
    if (!(await allowAction(`propose/${who}`, MAX_PER_ADDRESS_PER_DAY))
        || !(await allowAction("propose/all", MAX_PER_DAY))) {
      return res.status(429).json({ error: "Too many submissions today. Please try again tomorrow." });
    }

    const pr = await openPullRequest(record, submitter);
    return res.status(200).json({ status: "ok", url: pr.html_url, number: pr.number });
  } catch (e) {
    if (e instanceof InputError) return res.status(400).json({ error: e.message });
    console.error("propose failed:", e);
    return res.status(500).json({ error: "Couldn't submit right now. Please try again later." });
  }
}
