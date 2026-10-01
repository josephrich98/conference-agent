// Shared helpers for the email-notification functions (subscribe / confirm /
// unsubscribe / subscriptions). The leading underscore keeps Vercel from
// exposing this file as a route.
//
// Storage is a *private* Vercel Blob store. A subscription is an empty-ish blob
// at `subs/<base64url(email)>/<conference id>`, so listing the `subs/` prefix
// yields every (email, id) pair without reading any blob bodies. Pending
// confirmations are not stored at all: the confirmation link carries an HMAC
// over (email, id, expiry), so only a confirmed subscription is ever written.
//
// Every token here is HMAC-SHA256 (hex) keyed by SUBSCRIBE_SECRET over the
// newline-joined parts, and `conference_agent/subscriptions.py` computes the
// same tokens for the unsubscribe links in the update emails the local refresh
// job sends. Keep the two in sync.

import crypto from "node:crypto";
import { del, get, list, put } from "@vercel/blob";
import nodemailer from "nodemailer";

// Deliberately simple: one @, no spaces, a dot in the domain, a 2+ char TLD.
// Mirrored in index.html (the prompt validates before posting).
export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
// Ids are slugs of conference names (some over 100 characters); subscriptions
// stored before that change carry the former acronym ids, which also match.
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;

export const CONFIRM_TTL_SECONDS = 3 * 24 * 3600;
// Confirmation emails per address per day, so the form cannot be used to flood
// an inbox.
const MAX_CONFIRMATIONS_PER_DAY = 25;
const VERIFIED_COOKIE = "ca_verified";

export function normalizeEmail(email) {
  return String(email || "").trim().toLowerCase();
}

export function isValidEmail(email) {
  return email.length <= 254 && EMAIL_RE.test(email);
}

export function isValidId(id) {
  return ID_RE.test(String(id || ""));
}

function secret() {
  const s = process.env.SUBSCRIBE_SECRET;
  if (!s) throw new Error("SUBSCRIBE_SECRET is not set");
  return s;
}

export function sign(...parts) {
  return crypto.createHmac("sha256", secret()).update(parts.join("\n")).digest("hex");
}

export function verify(sig, ...parts) {
  const want = Buffer.from(sign(...parts), "hex");
  const got = Buffer.from(String(sig || ""), "hex");
  return got.length === want.length && crypto.timingSafeEqual(got, want);
}

const b64 = (s) => Buffer.from(s, "utf8").toString("base64url");
const unb64 = (s) => Buffer.from(s, "base64url").toString("utf8");

// --- Blob storage -----------------------------------------------------------

const subPath = (email, id) => `subs/${b64(email)}/${id}`;

export async function addSubscription(email, id) {
  await put(subPath(email, id), JSON.stringify({ created: new Date().toISOString() }), {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "application/json",
  });
}

async function listAll(prefix) {
  const out = [];
  let cursor;
  do {
    const page = await list({ prefix, cursor });
    out.push(...page.blobs);
    cursor = page.hasMore ? page.cursor : undefined;
  } while (cursor);
  return out;
}

export async function hasSubscription(email, id) {
  const blobs = await listAll(subPath(email, id));
  return blobs.some((b) => b.pathname === subPath(email, id));
}

// Remove one subscription, or every subscription for the address when id is "*".
export async function removeSubscriptions(email, id) {
  const prefix = id === "*" ? `subs/${b64(email)}/` : subPath(email, id);
  const blobs = (await listAll(prefix)).filter(
    (b) => id === "*" || b.pathname === subPath(email, id)
  );
  if (blobs.length) await del(blobs.map((b) => b.url));
  return blobs.length;
}

// Every confirmed (email, id) pair, decoded from the blob pathnames.
export async function allSubscriptions() {
  const subs = [];
  for (const b of await listAll("subs/")) {
    const [, enc, id] = b.pathname.split("/");
    if (enc && id) subs.push({ email: unb64(enc), id });
  }
  return subs;
}

// Record one confirmation send for the address; false when over the daily cap.
export async function allowConfirmation(email) {
  return allowAction(`throttle/${b64(email)}`, MAX_CONFIRMATIONS_PER_DAY);
}

// Record one use of a rate-limited action under `path`; false when it has
// already been used `maxPerDay` times in the last 24 hours.
export async function allowAction(path, maxPerDay) {
  const now = Date.now();
  let sent = [];
  try {
    const res = await get(path, { access: "private", useCache: false });
    if (res && res.statusCode === 200) sent = JSON.parse(await new Response(res.stream).text());
  } catch (e) { /* missing or unreadable: start fresh */ }
  sent = sent.filter((t) => now - t < 24 * 3600 * 1000);
  if (sent.length >= maxPerDay) return false;
  sent.push(now);
  await put(path, JSON.stringify(sent), {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "application/json",
  });
  return true;
}

// --- Catalog lookup ---------------------------------------------------------

// The deployed catalog snapshot, fetched from this same deployment so a
// subscription can only name a conference that exists.
let catalog = null;
export async function conferenceById(origin, id) {
  if (!catalog) {
    const resp = await fetch(`${origin}/data/conferences.json`);
    if (!resp.ok) throw new Error(`catalog fetch failed: ${resp.status}`);
    const payload = await resp.json();
    catalog = new Map((payload.conferences || []).map((c) => [c.id, c]));
  }
  return catalog.get(id) || null;
}

// A conference name's id: mirrors `models.name_id` (lowercase ASCII slug), so
// case, accents, punctuation, and spacing do not distinguish two names.
export function nameId(name) {
  return String(name || "")
    .normalize("NFKD")
    .replace(/[^\x00-\x7f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// --- Browser verification cookie -------------------------------------------

// Set when a confirmation link is opened in the same browser. While the cookie
// names the address being subscribed, further subscriptions from that browser
// take effect immediately instead of sending another confirmation email.
export function verifiedCookie(email) {
  const value = `${b64(email)}.${sign("verified", email)}`;
  return `${VERIFIED_COOKIE}=${value}; Path=/api; Max-Age=${365 * 24 * 3600}; HttpOnly; Secure; SameSite=Lax`;
}

export function verifiedEmail(req) {
  const raw = String(req.headers.cookie || "")
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith(`${VERIFIED_COOKIE}=`));
  if (!raw) return null;
  const [enc, sig] = raw.slice(VERIFIED_COOKIE.length + 1).split(".");
  if (!enc || !sig) return null;
  const email = unb64(enc);
  return verify(sig, "verified", email) ? email : null;
}

// --- HTTP helpers -----------------------------------------------------------

export function origin(req) {
  const proto = req.headers["x-forwarded-proto"] || "https";
  return `${proto}://${req.headers["x-forwarded-host"] || req.headers.host}`;
}

// Form bodies arrive as an object (Vercel parses urlencoded) or a raw string.
export function formField(req, name) {
  const body = req.body;
  if (body && typeof body === "object") return body[name];
  if (typeof body === "string") return new URLSearchParams(body).get(name);
  return undefined;
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// A minimal standalone page for the confirm / unsubscribe links.
export function page(res, status, title, bodyHtml) {
  res.status(status).setHeader("Content-Type", "text/html; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.send(`<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)} · Conference Agent</title>
<style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f7f8fa; color: #1f2933; }
  main { max-width: 480px; margin: 12vh auto; padding: 24px; background: #fff; border: 1px solid #e2e6eb; border-radius: 10px; }
  h1 { font-size: 18px; margin: 0 0 10px; }
  p { font-size: 14px; line-height: 1.5; }
  button { padding: 8px 16px; font-size: 14px; border: none; border-radius: 6px; background: #2563eb; color: #fff; cursor: pointer; }
  a { color: #2563eb; }
</style></head>
<body><main><h1>${escapeHtml(title)}</h1>${bodyHtml}<p><a href="/">Back to Conference Agent</a></p></main></body></html>`);
}

// --- Email ------------------------------------------------------------------

export async function sendMail({ to, subject, text }) {
  const user = process.env.SMTP_USER;
  const pass = process.env.SMTP_PASSWORD;
  if (!user || !pass) throw new Error("SMTP_USER / SMTP_PASSWORD are not set");
  const port = Number(process.env.SMTP_PORT || 587);
  const transport = nodemailer.createTransport({
    host: process.env.SMTP_HOST || "smtp.gmail.com",
    port,
    secure: port === 465,
    auth: { user, pass },
  });
  await transport.sendMail({ from: `"Conference Agent" <${user}>`, to, subject, text });
}
