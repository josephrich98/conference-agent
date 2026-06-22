/*
 * Boolean search query language for the conference table — browser port.
 *
 * This is a faithful port of `web/search.py`. The Python version compiles a
 * query string to a SQLAlchemy filter; this version compiles the same grammar to
 * a predicate `(row) => boolean` evaluated over the in-memory catalog (the static
 * `data/conferences.json` snapshot). The two must agree on which rows match —
 * `tests/test_search_parity.py` enforces that against the live database.
 *
 * Keep this in lockstep with `web/search.py`: the field registry, aliases,
 * tokenizer regex, parser, and comparison semantics are all mirrored from it.
 *
 * Exposes `buildPredicate(query)` (returns a `(row) => boolean`, or `null` for an
 * empty query meaning "match everything") and `sortRows(rows, sort, order)`.
 */

// --- Field registry (mirrors web/search.py) --------------------------------

// Public text field -> underlying column(s). A scoped match is a case-insensitive
// substring OR-ed across the listed columns.
const TEXT_FIELDS = {
  conference: ["acronym", "name"],
  category: ["category"],
  subcategory: ["subcategory"],
  format: ["format"],
  location: ["location"],
  size: ["size"],
  remote: ["remote_option"],
  cost: ["cost"],
  registration: ["upcoming_registration", "prior_registration"],
};

// Public date field -> [upcoming column, prior column]. Comparisons run against
// the displayed value: upcoming, falling back to prior.
const DATE_FIELDS = {
  abstract_due: ["upcoming_abstract_deadline", "prior_abstract_deadline"],
  paper_due: ["upcoming_paper_deadline", "prior_paper_deadline"],
  conference_dates: ["upcoming_start_date", "prior_start_date"],
};

// Public integer (month) field -> underlying column.
const INT_FIELDS = {
  conference_month: "conference_month",
  abstract_month: "abstract_month",
  paper_month: "paper_month",
};

// Columns scanned by a bare (unscoped) keyword.
const BARE_SEARCH_COLUMNS = [
  "acronym",
  "name",
  "category",
  "subcategory",
  "format",
  "location",
  "size",
  "remote_option",
  "cost",
  "upcoming_registration",
  "prior_registration",
  "url",
  "notes",
];

// Legacy / convenience names accepted but not advertised.
const ALIASES = {
  name: "conference",
  acronym: "conference",
  formats: "format",
  remote_option: "remote",
  abstract: "abstract_due",
  deadline: "abstract_due",
  upcoming_abstract_deadline: "abstract_due",
  prior_abstract: "abstract_due",
  paper: "paper_due",
  upcoming_paper_deadline: "paper_due",
  prior_paper: "paper_due",
  upcoming: "conference_dates",
  date: "conference_dates",
  upcoming_start_date: "conference_dates",
  prior_start: "conference_dates",
  submission_month: "abstract_month",
};

class QueryError extends Error {}

function resolveField(name) {
  let key = name.toLowerCase();
  key = ALIASES[key] || key;
  if (!(key in TEXT_FIELDS) && !(key in DATE_FIELDS) && !(key in INT_FIELDS)) {
    throw new QueryError(`Unknown field: ${JSON.stringify(name)}`);
  }
  return key;
}

// --- Tokenizer (mirrors web/search.py) -------------------------------------

const OP_ALT = ">=|<=|=>|=<|>|<|=";
const OP_NORMALIZE = { "=>": ">=", "=<": "<=" };
const OPERATORS = new Set(["AND", "OR", "NOT"]);

// Sticky, single-line equivalent of the verbose Python regex. Alternatives, in
// order: whitespace, '(', ')', scoped field:value, quoted keyword, bare word.
const TOKEN_RE = new RegExp(
  [
    "\\s+",
    "(?<lparen>\\()",
    "(?<rparen>\\))",
    "(?<field>[A-Za-z_]\\w*)" +
      "(?:" +
      `\\s*:\\s*(?<colon_op>${OP_ALT})?` +
      "|" +
      `\\s*(?<bare_op>${OP_ALT})` +
      ")\\s*" +
      '(?<val>"[^"]*"|\\*|[^\\s()]+)',
    '(?<quoted>"[^"]*")',
    '(?<word>[^\\s()":]+)',
  ].join("|"),
  "y"
);

function tokenize(query) {
  const tokens = [];
  TOKEN_RE.lastIndex = 0;
  let pos = 0;
  while (pos < query.length) {
    TOKEN_RE.lastIndex = pos;
    const m = TOKEN_RE.exec(query);
    if (!m || m.index !== pos) {
      throw new QueryError(`Unexpected character at position ${pos}`);
    }
    pos = TOKEN_RE.lastIndex;
    const g = m.groups;

    if (m[0].trim() === "" && !g.lparen && !g.rparen) {
      continue; // whitespace
    }
    if (g.lparen) {
      tokens.push({ kind: "lparen" });
    } else if (g.rparen) {
      tokens.push({ kind: "rparen" });
    } else if (g.field !== undefined) {
      const field = resolveField(g.field);
      let op = g.colon_op || g.bare_op || null;
      op = OP_NORMALIZE[op] || op;
      const raw = g.val;
      if (raw === "*") {
        tokens.push({ kind: "term", term: { field, op: null, value: "", presence: true } });
      } else {
        const value = raw.startsWith('"') ? raw.slice(1, -1) : raw;
        tokens.push({ kind: "term", term: { field, op, value, presence: false } });
      }
    } else if (g.quoted !== undefined) {
      const value = g.quoted.slice(1, -1);
      tokens.push({ kind: "term", term: { field: null, op: null, value, presence: false } });
    } else if (g.word !== undefined) {
      const word = g.word;
      const upper = word.toUpperCase();
      if (OPERATORS.has(upper)) {
        tokens.push({ kind: upper.toLowerCase() });
      } else {
        tokens.push({ kind: "term", term: { field: null, op: null, value: word, presence: false } });
      }
    }
  }
  return tokens;
}

// --- Parser (recursive descent, mirrors web/search.py) ---------------------

class Parser {
  constructor(tokens) {
    this.tokens = tokens;
    this.i = 0;
  }
  peek() {
    return this.i < this.tokens.length ? this.tokens[this.i] : null;
  }
  next() {
    return this.tokens[this.i++];
  }
  parse() {
    if (this.tokens.length === 0) return null;
    const node = this.parseOr();
    if (this.peek() !== null) {
      throw new QueryError("Unbalanced parentheses or trailing tokens");
    }
    return node;
  }
  parseOr() {
    const children = [this.parseAnd()];
    while (this.peek() && this.peek().kind === "or") {
      this.next();
      children.push(this.parseAnd());
    }
    return children.length === 1 ? children[0] : { op: "OR", children };
  }
  parseAnd() {
    const children = [this.parseNot()];
    for (;;) {
      const tok = this.peek();
      if (tok === null || tok.kind === "or" || tok.kind === "rparen") break;
      if (tok.kind === "and") this.next(); // explicit AND, else implicit
      children.push(this.parseNot());
    }
    return children.length === 1 ? children[0] : { op: "AND", children };
  }
  parseNot() {
    if (this.peek() && this.peek().kind === "not") {
      this.next();
      return { not: this.parseNot() };
    }
    return this.parseAtom();
  }
  parseAtom() {
    const tok = this.peek();
    if (tok === null) throw new QueryError("Unexpected end of query");
    if (tok.kind === "lparen") {
      this.next();
      const node = this.parseOr();
      const closing = this.peek();
      if (closing === null || closing.kind !== "rparen") {
        throw new QueryError("Missing closing parenthesis");
      }
      this.next();
      return node;
    }
    if (tok.kind === "term") {
      this.next();
      return { term: tok.term };
    }
    throw new QueryError(`Unexpected token: ${tok.kind}`);
  }
}

// --- Value helpers ---------------------------------------------------------

// SQL `col IS NOT NULL` semantics: present iff not null/undefined. (Empty string
// counts as present, matching SQLAlchemy's isnot(None).)
function isPresent(v) {
  return v !== null && v !== undefined;
}

function ilike(value, needle) {
  if (!isPresent(value)) return false;
  return String(value).toLowerCase().includes(needle.toLowerCase());
}

// Inclusive [lower, upper] ISO-date bounds for a partial date string. ISO dates
// compare lexicographically, so bounds are returned as "YYYY-MM-DD" strings.
function parseDateBounds(value) {
  const parts = value.split("-");
  const bad = () => new QueryError(`Invalid date: ${JSON.stringify(value)}`);
  const isNum = (s) => /^\d+$/.test(s);
  const pad = (n, w) => String(n).padStart(w, "0");
  if (parts.length === 1) {
    if (!isNum(parts[0])) throw bad();
    const y = pad(parseInt(parts[0], 10), 4);
    return [`${y}-01-01`, `${y}-12-31`];
  }
  if (parts.length === 2) {
    if (!isNum(parts[0]) || !isNum(parts[1])) throw bad();
    const year = parseInt(parts[0], 10);
    const month = parseInt(parts[1], 10);
    if (month < 1 || month > 12) throw bad();
    const lastDay = new Date(year, month, 0).getDate(); // day 0 of next month
    const y = pad(year, 4);
    const mm = pad(month, 2);
    return [`${y}-${mm}-01`, `${y}-${mm}-${pad(lastDay, 2)}`];
  }
  if (parts.length === 3) {
    if (!isNum(parts[0]) || !isNum(parts[1]) || !isNum(parts[2])) throw bad();
    const y = parseInt(parts[0], 10);
    const mo = parseInt(parts[1], 10);
    const d = parseInt(parts[2], 10);
    // Validate via round-trip, matching Python's date() construction.
    const probe = new Date(y, mo - 1, d);
    if (probe.getFullYear() !== y || probe.getMonth() !== mo - 1 || probe.getDate() !== d) {
      throw bad();
    }
    const iso = `${pad(y, 4)}-${pad(mo, 2)}-${pad(d, 2)}`;
    return [iso, iso];
  }
  throw bad();
}

function dateValue(row, field) {
  const [upcoming, prior] = DATE_FIELDS[field];
  const u = row[upcoming];
  return isPresent(u) ? u : row[prior] ?? null;
}

const MONTH_NAMES = (() => {
  const full = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
  ];
  const map = {};
  full.forEach((name, idx) => {
    const num = idx + 1;
    map[name] = num;
    map[name.slice(0, 3)] = num;
  });
  return map;
})();

function parseMonth(value) {
  const token = value.trim().toLowerCase();
  if (/^\d+$/.test(token)) {
    const num = parseInt(token, 10);
    if (num >= 1 && num <= 12) return num;
    throw new QueryError(`Month out of range (1-12): ${JSON.stringify(value)}`);
  }
  if (token in MONTH_NAMES) return MONTH_NAMES[token];
  throw new QueryError(`Invalid month: ${JSON.stringify(value)} (use 1-12 or a month name)`);
}

// --- Compiler (AST -> predicate, mirrors web/search.py) --------------------

function compileDateTerm(term) {
  const [lower, upper] = parseDateBounds(term.value);
  const op = term.op || "=";
  return (row) => {
    const v = dateValue(row, term.field);
    if (!isPresent(v)) return false;
    switch (op) {
      case "=": return v >= lower && v <= upper;
      case ">": return v > upper;
      case ">=": return v >= lower;
      case "<": return v < lower;
      case "<=": return v <= upper;
      default: throw new QueryError(`Unsupported operator: ${op}`);
    }
  };
}

function compileIntTerm(term) {
  const col = INT_FIELDS[term.field];
  const value = parseMonth(term.value);
  const op = term.op || "=";
  if (!["=", ">", ">=", "<", "<="].includes(op)) {
    throw new QueryError(`Unsupported operator: ${op}`);
  }
  return (row) => {
    const v = row[col];
    if (!isPresent(v)) return false;
    switch (op) {
      case "=": return v === value;
      case ">": return v > value;
      case ">=": return v >= value;
      case "<": return v < value;
      case "<=": return v <= value;
    }
  };
}

function compileTerm(term) {
  // Presence test: field:*
  if (term.presence) {
    if (term.field in DATE_FIELDS) return (row) => isPresent(dateValue(row, term.field));
    if (term.field in INT_FIELDS) {
      const col = INT_FIELDS[term.field];
      return (row) => isPresent(row[col]);
    }
    const cols = TEXT_FIELDS[term.field];
    return (row) => cols.some((c) => isPresent(row[c]));
  }

  // Bare keyword: substring across all bare-search columns.
  if (term.field === null) {
    return (row) => BARE_SEARCH_COLUMNS.some((c) => ilike(row[c], term.value));
  }

  if (term.field in DATE_FIELDS) return compileDateTerm(term);
  if (term.field in INT_FIELDS) return compileIntTerm(term);

  // Scoped text field.
  const cols = TEXT_FIELDS[term.field];
  return (row) => cols.some((c) => ilike(row[c], term.value));
}

function compile(node) {
  if (node.term !== undefined) return compileTerm(node.term);
  if (node.not !== undefined) {
    const child = compile(node.not);
    return (row) => !child(row);
  }
  if (node.op !== undefined) {
    const compiled = node.children.map(compile);
    if (node.op === "AND") return (row) => compiled.every((p) => p(row));
    return (row) => compiled.some((p) => p(row));
  }
  throw new QueryError("Malformed query tree");
}

/**
 * Compile a query string into a predicate `(row) => boolean`, or `null` if the
 * query is empty (meaning "match everything"). Throws `QueryError` on a malformed
 * query — callers should surface `err.message` like the API's 400 detail.
 */
function buildPredicate(query) {
  if (query === null || query === undefined || query.trim() === "") return null;
  const tokens = tokenize(query);
  const node = new Parser(tokens).parse();
  if (node === null) return null;
  return compile(node);
}

// --- Sorting (mirrors web/app.py _run_search ordering) ---------------------

const SORTABLE = new Set([
  "acronym", "name", "category", "subcategory", "format", "location", "size",
  "attendance", "remote_option", "upcoming_start_date", "upcoming_abstract_deadline",
  "upcoming_paper_deadline", "conference_month", "abstract_month", "paper_month",
]);

// Date sort columns fall back to the prior edition's value, matching the table.
const DATE_SORT_FALLBACK = {
  upcoming_start_date: "prior_start_date",
  upcoming_abstract_deadline: "prior_abstract_deadline",
  upcoming_paper_deadline: "prior_paper_deadline",
};

// The derived-month sorts break ties on the underlying displayed date (upcoming,
// falling back to prior) — mirroring the SQL column_property tie-breakers.
const MONTH_SORT_TIEBREAKER = {
  abstract_month: ["upcoming_abstract_deadline", "prior_abstract_deadline"],
  paper_month: ["upcoming_paper_deadline", "prior_paper_deadline"],
  conference_month: ["upcoming_start_date", "prior_start_date"],
};

const NUMERIC_SORT = new Set([
  "attendance", "conference_month", "abstract_month", "paper_month",
]);

function coalesce(row, col, fallbackCol) {
  const v = row[col];
  if (isPresent(v)) return v;
  return fallbackCol ? row[fallbackCol] ?? null : null;
}

// Compare two possibly-null values with NULLs always last (regardless of dir).
function cmpNullsLast(a, b, descending, numeric) {
  const aNull = !isPresent(a);
  const bNull = !isPresent(b);
  if (aNull && bNull) return 0;
  if (aNull) return 1; // nulls last
  if (bNull) return -1;
  let base;
  if (numeric) base = a - b;
  else base = String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0;
  return descending ? -base : base;
}

/** Sort rows like the server did: displayed value, NULLs last, then tie-breakers. */
function sortRows(rows, sort, order) {
  if (!SORTABLE.has(sort)) sort = "upcoming_start_date";
  const descending = order === "desc";
  const fallback = DATE_SORT_FALLBACK[sort];
  const numeric = NUMERIC_SORT.has(sort);
  const tiebreak = MONTH_SORT_TIEBREAKER[sort];

  return rows.slice().sort((ra, rb) => {
    const a = coalesce(ra, sort, fallback);
    const b = coalesce(rb, sort, fallback);
    let c = cmpNullsLast(a, b, descending, numeric);
    if (c !== 0) return c;

    if (tiebreak) {
      const ta = coalesce(ra, tiebreak[0], tiebreak[1]);
      const tb = coalesce(rb, tiebreak[0], tiebreak[1]);
      c = cmpNullsLast(ta, tb, descending, false);
      if (c !== 0) return c;
    }

    if (sort !== "acronym") {
      const ka = ra.acronym ?? ra.name ?? "";
      const kb = rb.acronym ?? rb.name ?? "";
      c = ka < kb ? -1 : ka > kb ? 1 : 0; // always ascending tie-break
      if (c !== 0) return c;
    }
    return 0;
  });
}

// Browser global + CommonJS export (for the Node-based parity test).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { buildPredicate, sortRows, QueryError };
} else {
  window.ConferenceSearch = { buildPredicate, sortRows, QueryError };
}
