/*
 * Test helper: run the browser "Did you mean" acronym suggestion.
 *
 * Used by tests/test_keyword_search.py. Reads the rows from the JSON file named in
 * argv[2] and a JSON array of query strings on stdin; writes a JSON object mapping
 * each query to suggestAcronym's result (a corrected query or null).
 */
const fs = require("fs");
const path = require("path");
const { suggestAcronym } = require(
  path.resolve(__dirname, "../../conference_agent/web/static/search.js")
);

const rows = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const out = {};
  for (const q of JSON.parse(input)) out[q] = suggestAcronym(q, rows);
  process.stdout.write(JSON.stringify(out));
});
