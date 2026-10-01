/*
 * Test helper: convert deadline times to a viewer's local time.
 *
 * Used by tests/test_deadline_local_time.py. Reads a JSON array of
 * [text, dateIso, viewerZone] triples on stdin and writes a JSON array of the
 * localDeadlineTime results (string or null) in the same order.
 */
const path = require("path");
const { localDeadlineTime } = require(
  path.resolve(__dirname, "../../web/static/calendar.js")
);

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const out = JSON.parse(input).map(([text, date, zone]) => localDeadlineTime(text, date, zone));
  process.stdout.write(JSON.stringify(out));
});
