/*
 * Test helper: run the browser keyword fallback over a JSON list of rows.
 *
 * Used by tests/test_keyword_search.py. Reads the rows from the JSON file named in
 * argv[2] and a JSON array of query strings on stdin; writes a JSON object mapping
 * each query to {terms, ids}, where ids are the matching conference ids in rank
 * order.
 */
const fs = require("fs");
const path = require("path");
const { keywordSearch, keywordTerms } = require(
  path.resolve(__dirname, "../../web/static/search.js")
);

const rows = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const out = {};
  for (const q of JSON.parse(input)) {
    out[q] = { terms: keywordTerms(q), ids: keywordSearch(q, rows).map((h) => h.row.id) };
  }
  process.stdout.write(JSON.stringify(out));
});
