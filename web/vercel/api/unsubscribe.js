// GET  /api/unsubscribe?email&id&sig -> a page with an Unsubscribe button
// POST /api/unsubscribe[?email&id&sig] -> removes the subscription(s)
//
// Links come from the update emails (conference_agent/subscriptions.py signs
// them); id "*" removes every subscription for the address. The POST also
// serves RFC 8058 one-click unsubscribe, where the mail client POSTs
// "List-Unsubscribe=One-Click" to the URL with the parameters in its query.

import {
  escapeHtml,
  formField,
  isValidEmail,
  isValidId,
  normalizeEmail,
  page,
  removeSubscriptions,
  verify,
} from "./_lib.js";

export default async function handler(req, res) {
  const pick = (k) => req.query[k] ?? (req.method === "POST" ? formField(req, k) : undefined);
  const email = normalizeEmail(pick("email"));
  const id = String(pick("id") || "");
  const sig = String(pick("sig") || "");

  const ok = isValidEmail(email) && (id === "*" || isValidId(id)) && verify(sig, "unsubscribe", email, id);
  if (!ok) return page(res, 400, "Invalid link", "<p>This unsubscribe link is incomplete or has been altered.</p>");
  const what = id === "*" ? "any conference" : `<b>${escapeHtml(id)}</b>`;

  if (req.method !== "POST") {
    const inputs = Object.entries({ email, id, sig })
      .map(([k, v]) => `<input type="hidden" name="${k}" value="${escapeHtml(v)}">`)
      .join("");
    return page(
      res,
      200,
      "Unsubscribe",
      `<p>Stop emailing <b>${escapeHtml(email)}</b> about ${what}?</p>` +
        `<form method="post" action="/api/unsubscribe">${inputs}<button type="submit">Unsubscribe</button></form>`
    );
  }

  try {
    await removeSubscriptions(email, id);
  } catch (e) {
    console.error("unsubscribe failed:", e);
    return page(res, 500, "Something went wrong", "<p>Please try the link again later.</p>");
  }
  return page(res, 200, "Unsubscribed", `<p><b>${escapeHtml(email)}</b> won't get further emails about ${what}.</p>`);
}
