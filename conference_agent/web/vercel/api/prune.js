// POST /api/prune  (Authorization: Bearer <SUBSCRIPTIONS_TOKEN>)
//
// Deletes rate-limit records older than a day (see pruneRateLimits). Called by
// scripts/notify_subscribers.py on every nightly run.

import { authorizedMaintainer, pruneRateLimits } from "./_lib.js";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  if (!authorizedMaintainer(req)) return res.status(401).json({ error: "Unauthorized." });
  try {
    return res.status(200).json({ deleted: await pruneRateLimits() });
  } catch (e) {
    console.error("prune failed:", e);
    return res.status(500).json({ error: "Couldn't prune." });
  }
}
