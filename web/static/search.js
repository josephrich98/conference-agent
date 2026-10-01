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
 * empty query meaning "match everything") and `sortRows(rows, sort, order)`, plus
 * the browser-only `keywordSearch(query, rows)` fallback (see the end of the file).
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
  deadline_time: ["deadline_time"],
};

// Public date field -> [upcoming column, prior column]. Comparisons run against
// the displayed value: upcoming, falling back to prior.
const DATE_FIELDS = {
  abstract_due: ["upcoming_abstract_deadline", "prior_abstract_deadline"],
  late_abstract_due: [
    "upcoming_late_abstract_deadline",
    "prior_late_abstract_deadline",
  ],
  paper_due: ["upcoming_paper_deadline", "prior_paper_deadline"],
  conference_dates: ["upcoming_start_date", "prior_start_date"],
};

// Public integer (month) field -> underlying column.
const INT_FIELDS = {
  conference_month: "conference_month",
  abstract_month: "abstract_month",
  late_abstract_month: "late_abstract_month",
  paper_month: "paper_month",
};

// Public numeric field -> underlying column (values are counts: 5000, 5,000, 5k).
const NUMBER_FIELDS = {
  attendance: "attendance",
};

// Size buckets ranked by magnitude, for comparisons such as `size>=large`.
const SIZE_QUERY_RANK = { small: 1, medium: 2, large: 3, massive: 4 };

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
  "deadline_time",
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
  late_abstract: "late_abstract_due",
  late_breaking: "late_abstract_due",
  poster_due: "late_abstract_due",
  upcoming_late_abstract_deadline: "late_abstract_due",
  prior_late_abstract: "late_abstract_due",
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
  if (
    !(key in TEXT_FIELDS) && !(key in DATE_FIELDS) && !(key in INT_FIELDS) &&
    !(key in NUMBER_FIELDS)
  ) {
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
// Values that stand for the current date (date fields) or month (month fields).
const NOW_WORDS = new Set(["today", "now"]);

function parseDateBounds(value) {
  if (NOW_WORDS.has(value.trim().toLowerCase())) {
    const d = new Date();
    const today = [
      String(d.getFullYear()).padStart(4, "0"),
      String(d.getMonth() + 1).padStart(2, "0"),
      String(d.getDate()).padStart(2, "0"),
    ].join("-");
    return [today, today];
  }
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
  if (NOW_WORDS.has(token)) return new Date().getMonth() + 1;
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

// `getValue(row) <op> value` (default `=`), false when the row's value is absent.
function compareTerm(op, value, getValue) {
  op = op || "=";
  if (!["=", ">", ">=", "<", "<="].includes(op)) {
    throw new QueryError(`Unsupported operator: ${op}`);
  }
  return (row) => {
    const v = getValue(row);
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

function compileIntTerm(term) {
  const col = INT_FIELDS[term.field];
  return compareTerm(term.op, parseMonth(term.value), (row) => row[col]);
}

/** Parse a count: an integer, optionally with thousands separators or a `k` suffix. */
function parseCount(value) {
  const m = /^(\d+(?:\.\d+)?)(k?)$/.exec(value.trim().toLowerCase().replace(/,/g, ""));
  if (!m || (!m[2] && m[1].includes("."))) {
    throw new QueryError(`Invalid number: ${JSON.stringify(value)} (e.g. 5000, 5,000, or 5k)`);
  }
  return Math.round(parseFloat(m[1]) * (m[2] ? 1000 : 1));
}

function compileNumberTerm(term) {
  const col = NUMBER_FIELDS[term.field];
  return compareTerm(term.op, parseCount(term.value), (row) => row[col]);
}

function compileSizeRankTerm(term) {
  const rank = SIZE_QUERY_RANK[term.value.trim().toLowerCase()];
  if (rank === undefined) {
    throw new QueryError(
      `Invalid size: ${JSON.stringify(term.value)} (use ${Object.keys(SIZE_QUERY_RANK).join(", ")})`,
    );
  }
  return compareTerm(term.op, rank, (row) => SIZE_QUERY_RANK[row.size] ?? null);
}

function compileTerm(term) {
  // Presence test: field:*
  if (term.presence) {
    if (term.field in DATE_FIELDS) return (row) => isPresent(dateValue(row, term.field));
    if (term.field in INT_FIELDS) {
      const col = INT_FIELDS[term.field];
      return (row) => isPresent(row[col]);
    }
    if (term.field in NUMBER_FIELDS) {
      const col = NUMBER_FIELDS[term.field];
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
  if (term.field in NUMBER_FIELDS) return compileNumberTerm(term);
  if (term.field === "size" && term.op) return compileSizeRankTerm(term);

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
  "upcoming_late_abstract_deadline", "upcoming_paper_deadline", "conference_month",
  "abstract_month", "late_abstract_month", "paper_month",
]);

// Date sort columns fall back to the prior edition's value, matching the table.
const DATE_SORT_FALLBACK = {
  upcoming_start_date: "prior_start_date",
  upcoming_abstract_deadline: "prior_abstract_deadline",
  upcoming_late_abstract_deadline: "prior_late_abstract_deadline",
  upcoming_paper_deadline: "prior_paper_deadline",
};

// The derived-month sorts break ties on the day of the month of the underlying
// displayed date (upcoming, falling back to prior), so the order is seasonal and
// ignores which year's edition is shown — mirroring the API's tie-breakers.
const MONTH_SORT_TIEBREAKER = {
  abstract_month: ["upcoming_abstract_deadline", "prior_abstract_deadline"],
  late_abstract_month: [
    "upcoming_late_abstract_deadline",
    "prior_late_abstract_deadline",
  ],
  paper_month: ["upcoming_paper_deadline", "prior_paper_deadline"],
  conference_month: ["upcoming_start_date", "prior_start_date"],
};

const NUMERIC_SORT = new Set([
  "attendance", "conference_month", "abstract_month", "late_abstract_month",
  "paper_month", "size",
]);

// Size sorts by magnitude, not alphabetically: ascending is massive -> small.
// Mirrors ``_SIZE_SORT_RANK`` in web/app.py.
const SIZE_SORT_RANK = { massive: 1, large: 2, medium: 3, small: 4 };

// The derived-month sorts roll from the current month rather than January, so
// ascending lists what comes next first (in October: Oct, Nov, ..., Sep).
// Mirrors ``_rolling_month`` in web/app.py.
function rollingMonth(month, current) {
  return isPresent(month) ? (Number(month) - current + 12) % 12 : null;
}

function coalesce(row, col, fallbackCol) {
  if (col === "size") return SIZE_SORT_RANK[row.size] ?? null;
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

/**
 * Sort rows like the server did: displayed value, NULLs last, then tie-breakers.
 * Month sorts begin at `monthStart` (1-12; defaults to this month, local time).
 */
function sortRows(rows, sort, order, monthStart = new Date().getMonth() + 1) {
  if (!SORTABLE.has(sort)) sort = "upcoming_start_date";
  const descending = order === "desc";
  const fallback = DATE_SORT_FALLBACK[sort];
  const numeric = NUMERIC_SORT.has(sort);
  const tiebreak = MONTH_SORT_TIEBREAKER[sort];
  const key = tiebreak
    ? (row) => rollingMonth(row[sort], monthStart)
    : (row) => coalesce(row, sort, fallback);

  return rows.slice().sort((ra, rb) => {
    const a = key(ra);
    const b = key(rb);
    let c = cmpNullsLast(a, b, descending, numeric);
    if (c !== 0) return c;

    if (tiebreak) {
      const day = (row) => {
        const d = coalesce(row, tiebreak[0], tiebreak[1]);
        return isPresent(d) ? Number(String(d).slice(8, 10)) : null;
      };
      c = cmpNullsLast(day(ra), day(rb), descending, true);
      if (c !== 0) return c;
    }

    // Within a size bucket, order by the attendance figure the bucket comes
    // from, following the size direction (ascending size runs largest first).
    if (sort === "size") {
      c = cmpNullsLast(ra.attendance, rb.attendance, !descending, true);
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

// --- Keyword fallback (browser-only; no counterpart in web/search.py) -------
//
// The boolean language above is exact: every bare word must appear verbatim as a
// substring, so a typo ("radiolgy"), an inflection ("radiological"), or a filler
// word ("conferences in europe") returns nothing. When a query matches no rows,
// or does not parse, the page falls back to `keywordSearch`, a forgiving,
// search-engine-style ranking: each word matches loosely (whole word, prefix,
// shared stem, inside a longer word, or a small typo), rows that
// match more of the words rank first, and a hit in the acronym, name, or
// subcategory counts for more than one in the notes. It is a UI convenience
// only, so it does not affect the parity test or the server API.

// Row column -> weight of a hit in that column.
const KEYWORD_FIELDS = [
  ["acronym", 4],
  ["name", 3],
  ["subcategory", 3],
  ["category", 2],
  ["location", 2],
  ["format", 1],
  ["remote_option", 1],
  ["size", 1],
  ["cost", 0.5],
  ["deadline_time", 0.5],
  ["upcoming_registration", 0.5],
  ["prior_registration", 0.5],
  ["url", 0.5],
  ["notes", 0.5],
];

// Long free-text columns where typo-tolerant matching mostly produces noise.
const KEYWORD_EXACT_ONLY = new Set([
  "notes", "url", "upcoming_registration", "prior_registration",
]);

// Displayed-date columns a four-digit year is checked against.
const KEYWORD_DATE_COLUMNS = [
  "upcoming_start_date", "prior_start_date",
  "upcoming_abstract_deadline", "prior_abstract_deadline",
  "upcoming_late_abstract_deadline", "prior_late_abstract_deadline",
  "upcoming_paper_deadline", "prior_paper_deadline",
];

const KEYWORD_MONTH_COLUMNS = [
  "conference_month", "abstract_month", "late_abstract_month", "paper_month",
];

// Filler words dropped from a keyword query (plus the boolean operators).
const STOPWORDS = new Set([
  "a", "an", "the", "and", "or", "not", "of", "in", "on", "at", "for", "to",
  "with", "without", "by", "from", "about", "as", "is", "are", "be", "that",
  "this", "these", "those", "which", "what", "where", "when", "who", "i", "me",
  "my", "we", "our", "you", "your", "any", "all", "some", "find", "show", "list",
  "give", "want", "looking", "look", "need", "near", "held", "conference",
  "conferences", "meeting", "meetings", "event", "events",
]);

// Common shorthand -> the wording used in the data. A term matches if it or any
// of its expansions matches.
const KEYWORD_SYNONYMS = {
  ai: ["artificial intelligence"],
  ml: ["machine learning"],
  nlp: ["natural language processing"],
  cs: ["computer science"],
  online: ["virtual", "hybrid"],
  remote: ["virtual", "hybrid"],
  big: ["large", "massive"],
  major: ["large", "massive"],
  huge: ["massive"],
  enormous: ["massive"],
  tiny: ["small"],
};

function foldText(s) {
  return String(s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function splitWords(s) {
  return foldText(s).split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

// Optimal-string-alignment edit distance (a transposition costs 1), with an
// early exit once the distance is known to exceed `max`.
function editDistance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev2 = null;
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let rowMin = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      if (prev2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        v = Math.min(v, prev2[j - 2] + 1);
      }
      cur.push(v);
      if (v < rowMin) rowMin = v;
    }
    if (rowMin > max) return max + 1;
    prev2 = prev;
    prev = cur;
  }
  return prev[b.length];
}

function commonPrefixLength(a, b) {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  return i;
}

// How well a single-word term matches one word of the row text, 0 (no match)
// to 1 (the same word).
function wordQuality(term, word, fuzzy) {
  if (term === word) return 1;
  if (term.length < 3) return 0; // "ai", "us": whole-word matches only
  if (word.startsWith(term)) return 0.9; // neuro -> neurology
  const shared = commonPrefixLength(term, word);
  if (shared >= Math.max(4, Math.ceil(0.75 * Math.min(term.length, word.length)))) {
    return 0.8; // radiological ~ radiology, imaging ~ image
  }
  if (term.length >= 4 && word.includes(term)) return 0.7; // informatics in bioinformatics
  // Short words sit too close to unrelated ones ("stats" vs "states"), so typo
  // tolerance starts at six letters.
  if (fuzzy && term.length >= 6) {
    const max = term.length >= 9 ? 2 : 1;
    if (editDistance(term, word, max) <= max) return 0.6; // radiolgy ~ radiology
  }
  return 0;
}

// Per-row tokenized text, computed once per row object.
const KEYWORD_INDEX = new WeakMap();

function keywordIndex(row) {
  let idx = KEYWORD_INDEX.get(row);
  if (!idx) {
    idx = KEYWORD_FIELDS
      .filter(([col]) => isPresent(row[col]) && row[col] !== "")
      .map(([col, weight]) => ({
        weight,
        fuzzy: !KEYWORD_EXACT_ONLY.has(col),
        text: ` ${splitWords(row[col]).join(" ")} `,
        words: [...new Set(splitWords(row[col]))],
      }));
    KEYWORD_INDEX.set(row, idx);
  }
  return idx;
}

// Every field name and alias the boolean grammar accepts; a `field:` prefix in a
// query that fell through to keyword search is dropped rather than searched for.
const KNOWN_FIELD_NAMES = new Set([
  ...Object.keys(TEXT_FIELDS), ...Object.keys(DATE_FIELDS),
  ...Object.keys(INT_FIELDS), ...Object.keys(NUMBER_FIELDS), ...Object.keys(ALIASES),
]);

/** Split a query into keyword terms: quoted phrases stay whole, filler words drop. */
function keywordTerms(query) {
  const terms = [];
  const seen = new Set();
  const add = (t) => {
    if (!seen.has(t)) {
      seen.add(t);
      terms.push(t);
    }
  };
  let rest = String(query || "").replace(/"([^"]*)"/g, (_, phrase) => {
    const words = splitWords(phrase);
    if (words.length) add(words.join(" "));
    return " ";
  });
  rest = rest.replace(/([A-Za-z_]\w*)\s*:/g, (m, name) =>
    KNOWN_FIELD_NAMES.has(name.toLowerCase()) ? " " : ` ${name} `
  );
  for (const word of splitWords(rest)) {
    if (STOPWORDS.has(word)) continue;
    if (/^\d+$/.test(word) && !/^(19|20)\d\d$/.test(word)) continue; // "06" of a date
    if (word.length < 2) continue;
    add(word);
  }
  return terms;
}

// Best weighted match of one term anywhere in the row, 0 if none.
function termScore(term, row) {
  const idx = keywordIndex(row);
  const alternatives = [term, ...(KEYWORD_SYNONYMS[term] || [])];
  let best = 0;
  for (const alt of alternatives) {
    const isPhrase = alt.includes(" ");
    for (const field of idx) {
      if (field.weight <= best) continue; // cannot beat the best hit so far
      let q = 0;
      if (isPhrase) {
        if (field.text.includes(` ${alt} `)) q = 1;
        else if (field.text.includes(alt)) q = 0.8;
      } else {
        for (const word of field.words) {
          q = Math.max(q, wordQuality(alt, word, field.fuzzy));
          if (q === 1) break;
        }
      }
      best = Math.max(best, q * field.weight);
    }
  }
  if (/^(19|20)\d\d$/.test(term) &&
      KEYWORD_DATE_COLUMNS.some((c) => isPresent(row[c]) && String(row[c]).startsWith(term))) {
    best = Math.max(best, 1);
  }
  const month = MONTH_NAMES[term];
  if (month !== undefined && KEYWORD_MONTH_COLUMNS.some((c) => row[c] === month)) {
    best = Math.max(best, 1);
  }
  return best;
}

/**
 * Forgiving keyword ranking over `rows`. Returns `[{row, matched, score}]` for
 * rows matching at least half as many of the query's terms as the best row does,
 * most relevant first (more terms matched, then a higher score). Terms that match
 * few rows weigh more than ones that match many, as in a search engine. Returns
 * an empty array when the query has no usable terms.
 */
function keywordSearch(query, rows) {
  const terms = keywordTerms(query);
  if (terms.length === 0) return [];
  const perRow = rows.map((row) => terms.map((t) => termScore(t, row)));
  const idf = terms.map((_, i) => {
    const df = perRow.filter((scores) => scores[i] > 0).length;
    return df ? Math.log(1 + rows.length / df) : 0;
  });
  const hits = [];
  perRow.forEach((scores, r) => {
    const matched = scores.filter((s) => s > 0).length;
    if (matched === 0) return;
    const score = scores.reduce((sum, s, i) => sum + s * idf[i], 0);
    hits.push({ row: rows[r], matched, score });
  });
  const bestMatched = hits.reduce((m, h) => Math.max(m, h.matched), 0);
  const minMatched = Math.ceil(bestMatched / 2);
  return hits
    .filter((h) => h.matched >= minMatched)
    .sort((a, b) => b.matched - a.matched || b.score - a.score);
}

// Browser global + CommonJS export (for the Node-based tests).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { buildPredicate, sortRows, keywordSearch, keywordTerms, QueryError };
} else {
  window.ConferenceSearch = { buildPredicate, sortRows, keywordSearch, keywordTerms, QueryError };
}
