/*
 * Test helper for the structured deadline-time helpers in web/static/calendar.js.
 *
 * Used by tests/test_deadline_time.py. Reads a JSON object on stdin with any of:
 *   instants: [[dateIso, time, tz], ...]   -> ms instants the deadline has passed
 *   rows:     [row, ...]                   -> deadlineTimeText for each row,
 *                                             as [12-hour, 24-hour]
 *   zones:    true                         -> the DEADLINE_ZONES table
 * and writes a JSON object with the matching keys.
 */
const path = require("path");
const cal = require(path.resolve(__dirname, "../../web/static/calendar.js"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const req = JSON.parse(input);
  const out = {};
  if (req.instants) out.instants = req.instants.map(([d, t, z]) => cal.deadlineInstant(d, t, z));
  if (req.rows) out.rows = req.rows.map((r) => [cal.deadlineTimeText(r, false), cal.deadlineTimeText(r, true)]);
  if (req.zones) out.zones = cal.DEADLINE_ZONES;
  process.stdout.write(JSON.stringify(out));
});
