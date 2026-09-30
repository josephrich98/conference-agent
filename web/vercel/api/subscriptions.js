// GET /api/subscriptions  (Authorization: Bearer <SUBSCRIBE_SECRET>)
//
// Every confirmed subscription as [{email, id}]. Read by the local refresh job
// (scripts/notify_subscribers.py), which emails subscribers when a conference
// they follow changes.

import crypto from "node:crypto";
import { allSubscriptions } from "./_lib.js";

function authorized(req) {
  const want = Buffer.from(`Bearer ${process.env.SUBSCRIBE_SECRET || ""}`);
  const got = Buffer.from(String(req.headers.authorization || ""));
  return Boolean(process.env.SUBSCRIBE_SECRET) && got.length === want.length && crypto.timingSafeEqual(got, want);
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (!authorized(req)) return res.status(401).json({ error: "Unauthorized." });
  try {
    return res.status(200).json({ subscriptions: await allSubscriptions() });
  } catch (e) {
    console.error("list subscriptions failed:", e);
    return res.status(500).json({ error: "Couldn't list subscriptions." });
  }
}
