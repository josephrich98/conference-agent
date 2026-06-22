/*
 * Parity-test helper: run the browser query language over a JSON snapshot.
 *
 * Used by tests/test_search_parity.py to confirm web/static/search.js matches
 * web/search.py. Reads the conference rows from the JSON file named in argv[2]
 * and a JSON array of query strings on stdin; writes a JSON object mapping each
 * query to the sorted list of matching conference ids (or {error: msg}).
 */
const fs = require("fs");
const path = require("path");
const { buildPredicate } = require(path.resolve(__dirname, "../../web/static/search.js"));

const rows = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const queries = JSON.parse(input);
  const out = {};
  for (const q of queries) {
    try {
      const predicate = buildPredicate(q);
      const matched = predicate ? rows.filter(predicate) : rows;
      out[q] = matched.map((r) => r.id).sort();
    } catch (e) {
      out[q] = { error: e.message };
    }
  }
  process.stdout.write(JSON.stringify(out));
});
