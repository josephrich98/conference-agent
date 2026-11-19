// GET /api/subscriptions  (Authorization: Bearer <SUBSCRIPTIONS_TOKEN>)
//
// Every confirmed subscription as [{email, id}]. Read by the local refresh job
// (scripts/notify_subscribers.py), which emails subscribers when a conference
// they follow changes.

import { allSubscriptions, authorizedMaintainer } from "./_lib.js";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (!authorizedMaintainer(req)) return res.status(401).json({ error: "Unauthorized." });
  try {
    return res.status(200).json({ subscriptions: await allSubscriptions() });
  } catch (e) {
    console.error("list subscriptions failed:", e);
    return res.status(500).json({ error: "Couldn't list subscriptions." });
  }
}
