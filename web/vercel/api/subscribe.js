// POST /api/subscribe  {email, id}
//
// Subscribes an address to update emails for one conference. A browser that
// already confirmed this address (the ca_verified cookie) is subscribed at once;
// otherwise a signed confirmation link is emailed and nothing is stored until
// it is opened. The reply never reveals whether an address is already subscribed.

import {
  CONFIRM_TTL_SECONDS,
  addSubscription,
  allowConfirmation,
  conferenceById,
  hasSubscription,
  isValidEmail,
  isValidId,
  normalizeEmail,
  origin,
  sendMail,
  sign,
  verifiedEmail,
} from "./_lib.js";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  const body = req.body && typeof req.body === "object" ? req.body : {};
  const email = normalizeEmail(body.email);
  const id = String(body.id || "");
  if (!isValidEmail(email)) return res.status(400).json({ error: "Enter a valid email address." });
  if (!isValidId(id)) return res.status(400).json({ error: "Unknown conference." });

  try {
    const site = origin(req);
    const conf = await conferenceById(site, id);
    if (!conf) return res.status(404).json({ error: "Unknown conference." });
    const label = conf.acronym || conf.name || id;

    if (verifiedEmail(req) === email) {
      await addSubscription(email, id);
      return res.status(200).json({
        status: "subscribed",
        message: `Subscribed. ${email} will get an email when ${label} changes.`,
      });
    }

    const pending = {
      status: "pending",
      message: `Check ${email} for a link to confirm updates for ${label} (skip this if you're already subscribed).`,
    };
    if (await hasSubscription(email, id)) return res.status(200).json(pending);
    if (!(await allowConfirmation(email))) {
      return res.status(429).json({ error: "Too many confirmation emails to this address today. Try again tomorrow." });
    }

    const exp = String(Math.floor(Date.now() / 1000) + CONFIRM_TTL_SECONDS);
    const q = new URLSearchParams({ email, id, exp, sig: sign("confirm", email, id, exp) });
    await sendMail({
      to: email,
      subject: `Confirm update emails for ${label}`,
      text:
        `Someone (hopefully you) asked Conference Agent to email ${email} when ` +
        `${conf.name || label} changes its deadlines or dates.\n\n` +
        `Confirm within 3 days:\n${site}/api/confirm?${q}\n\n` +
        `If this wasn't you, ignore this email and nothing will be sent.\n`,
    });
    return res.status(200).json(pending);
  } catch (e) {
    console.error("subscribe failed:", e);
    return res.status(500).json({ error: "Couldn't subscribe right now. Please try again later." });
  }
}
