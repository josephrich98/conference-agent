// GET  /api/confirm?email&id&exp&sig  -> a page with a Confirm button
// POST /api/confirm                   -> stores the subscription
//
// The GET only renders a button, because mail scanners fetch links in incoming
// email; a subscription is written only by the POST a person submits. The POST
// also sets the ca_verified cookie so later subscriptions from this browser
// skip the email round-trip.

import {
  addSubscription,
  escapeHtml,
  formField,
  isValidEmail,
  isValidId,
  normalizeEmail,
  page,
  verifiedCookie,
  verify,
} from "./_lib.js";

export default async function handler(req, res) {
  const pick = (k) => (req.method === "POST" ? formField(req, k) : req.query[k]);
  const email = normalizeEmail(pick("email"));
  const id = String(pick("id") || "");
  const exp = String(pick("exp") || "");
  const sig = String(pick("sig") || "");

  const ok =
    isValidEmail(email) && isValidId(id) && /^\d+$/.test(exp) && verify(sig, "confirm", email, id, exp);
  if (!ok) return page(res, 400, "Invalid link", "<p>This confirmation link is incomplete or has been altered.</p>");
  if (Number(exp) < Date.now() / 1000) {
    return page(res, 410, "Link expired", "<p>This confirmation link has expired. Click ✉️ on the conference again to get a new one.</p>");
  }

  if (req.method !== "POST") {
    const hidden = { email, id, exp, sig };
    const inputs = Object.entries(hidden)
      .map(([k, v]) => `<input type="hidden" name="${k}" value="${escapeHtml(v)}">`)
      .join("");
    return page(
      res,
      200,
      "Confirm update emails",
      `<p>Email <b>${escapeHtml(email)}</b> when <b>${escapeHtml(id)}</b> changes its deadlines or dates?</p>` +
        `<form method="post" action="/api/confirm">${inputs}<button type="submit">Confirm</button></form>`
    );
  }

  try {
    await addSubscription(email, id);
  } catch (e) {
    console.error("confirm failed:", e);
    return page(res, 500, "Something went wrong", "<p>The subscription couldn't be saved. Please try the link again later.</p>");
  }
  res.setHeader("Set-Cookie", verifiedCookie(email));
  return page(
    res,
    200,
    "Subscribed",
    `<p><b>${escapeHtml(email)}</b> will get an email, with an updated calendar file, whenever <b>${escapeHtml(id)}</b> changes.</p>` +
      `<p>Each email has an unsubscribe link.</p>`
  );
}
