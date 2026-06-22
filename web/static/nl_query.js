/*
 * Natural-language → boolean-query translation, in the browser.
 *
 * This is the static-site counterpart of `web/nl_query.py`. The Python version
 * runs the translation against a local Ollama server; this version runs a small
 * instruction model entirely in the browser over WebGPU via WebLLM
 * (https://github.com/mlc-ai/web-llm) — no API key, no backend, no network
 * egress after the one-time model download. That keeps the deployed site
 * compute-free and credential-free, matching the rest of the static layer.
 *
 * The translation contract is ported faithfully from `nl_query.py`:
 *   - the system prompt and field reference are identical (the field catalog is
 *     supplied from the same `field_help()` data the snapshot already ships as
 *     `DATA.fields`, so the prompt never drifts from what the parser accepts);
 *   - the model is constrained to emit a JSON object and run at temperature 0;
 *   - its query is validated with the real parser (`ConferenceSearch.buildPredicate`,
 *     the browser port of `build_filter`) before it is returned, with one repair
 *     round that feeds the parse error back — so the search box is never
 *     populated with a query that errors.
 *
 * Exposes `window.ConferenceNL`:
 *   - `isSupported()` → boolean (WebGPU present)
 *   - `translate(text, fields, { onProgress })` → Promise<{query, repaired}>
 * The WebLLM library and model weights load lazily on first translate() call.
 */
(function () {
  "use strict";

  // WebLLM ES module + the in-browser model. Qwen2.5-1.5B is small (~1 GB
  // download, cached by the browser afterward) yet handles this narrow,
  // validated translation task well. The id must be one of WebLLM's prebuilt
  // models; q4f16 needs WebGPU shader-f16 (standard in current Chrome/Edge).
  const WEBLLM_URL = "https://esm.run/@mlc-ai/web-llm";
  const MODEL_ID = "Qwen2.5-1.5B-Instruct-q4f16_1-MLC";

  // --- Prompt (ported verbatim from web/nl_query.py) -----------------------

  // A compact field reference built from the same field_help() data the Python
  // prompt uses (each entry's `type` already carries the controlled vocabulary
  // after "cat:"), so the two prompts list identical fields and values.
  function fieldReference(fields) {
    return (fields || []).map((e) => `- ${e.field} (${e.type})`).join("\n");
  }

  function systemPrompt(fields) {
    return `You translate a user's plain-English description of conferences they want into a single boolean search query. Output ONLY JSON: {"query": "<query>"}.

The query language (mirrors the table's columns):
- Scoped match: field:value (case-insensitive substring). Quote multi-word values: subcategory:"machine learning".
- Boolean operators: AND, OR, NOT, with parentheses for grouping. Adjacent terms are implicitly AND-ed.
- Date fields accept YYYY, YYYY-MM, or YYYY-MM-DD with operators > >= < <= = attached after the colon: abstract_due:>=2026-06, conference_dates:2027.
- Month fields take 1-12 or a month name: conference_month:november, abstract_month:>=6.
- Presence test: field:* means the field is set; NOT field:* means it is unset.

Queryable fields (categorical fields list their allowed values after "cat:"):
${fieldReference(fields)}

Guidance:
- Use \`subcategory\` for a specific field (radiology, oncology, "machine learning", genomics, ...). Use \`category\` only for the broad buckets listed for it (medicine, computer science, artificial intelligence, ...).
- Map size words to the \`size\` vocabulary: big/large→large, mid-size→medium, small→small.
- Map attendance/format/remote words to their controlled vocabularies.
- "deadline" alone means abstract_due. "no/without X" means NOT X:*.
- Time of year WITHOUT a specific year — "in November", "in the fall", "any year", a month range like "September through January" — uses a MONTH field (conference_month / abstract_month / paper_month), never a date field. Use a date field (conference_dates / abstract_due / paper_due) ONLY when a specific year is named ("in 2027", "after June 2026"). Never invent a year that the request did not state.
- A range is two bounds. A range that stays within the year — "March to June" — uses AND: (conference_month:>=3 AND conference_month:<=6). A range that wraps past December — "September through January" — uses OR: (conference_month:>=9 OR conference_month:<=1).
- Use only the fields and values listed above. If the request names no usable filter, return {"query": ""} (an empty query matches everything).

Examples:
Request: big radiology conferences that are virtual
{"query": "subcategory:radiology AND size:large AND remote:virtual"}
Request: machine learning conferences with an abstract deadline after June 2026
{"query": "subcategory:\\"machine learning\\" AND abstract_due:>=2026-06"}
Request: cardiology or oncology meetings happening in 2027
{"query": "(subcategory:cardiology OR subcategory:oncology) AND conference_dates:2027"}
Request: genomics conferences in November with no paper deadline
{"query": "subcategory:genomics AND conference_month:november AND NOT paper_due:*"}
Request: big radiology conferences between September and January of any year
{"query": "size:large AND subcategory:radiology AND (conference_month:>=9 OR conference_month:<=1)"}
Request: oncology conferences held from March to June
{"query": "subcategory:oncology AND (conference_month:>=3 AND conference_month:<=6)"}`;
  }

  function buildMessages(text, fields, priorError) {
    const messages = [
      { role: "system", content: systemPrompt(fields) },
      { role: "user", content: `Request: ${text}` },
    ];
    if (priorError) {
      // One repair round: hand the model its own bad query's parse error.
      messages.push({
        role: "user",
        content:
          `That query failed to parse with error: ${priorError}. ` +
          "Return corrected JSON using only the listed fields and values.",
      });
    }
    return messages;
  }

  // --- WebLLM engine (lazy, loaded once) -----------------------------------

  let enginePromise = null;

  function isSupported() {
    return typeof navigator !== "undefined" && !!navigator.gpu;
  }

  // Load WebLLM and create the engine, reusing it across calls. `onProgress` is
  // forwarded the model-download progress (mostly relevant on the first call,
  // which fetches the weights).
  function getEngine(onProgress) {
    if (!enginePromise) {
      enginePromise = (async () => {
        const webllm = await import(WEBLLM_URL);
        return webllm.CreateMLCEngine(MODEL_ID, {
          initProgressCallback: (report) => {
            if (onProgress) onProgress(report.text || "", report.progress);
          },
        });
      })().catch((err) => {
        enginePromise = null; // allow a retry on a later click
        throw err;
      });
    }
    return enginePromise;
  }

  // We deliberately do NOT use `response_format`/json_object here. In the
  // esm.run web-llm build, that path runs the WebGPU grammar compiler
  // (GrammarCompiler.CompileJSONSchema) inside a worker, which throws
  // "Cannot pass non-string to std::string" and surfaces as an *uncaught*
  // promise rejection — it escapes the awaited call, so a try/catch around
  // create() cannot recover it. Constraining output was only ever a nicety:
  // the system prompt already demands `{"query": "..."}` and `extractQuery`
  // strips fences / scans for the JSON object, so unconstrained generation is
  // both reliable and sufficient.
  async function createCompletion(engine, messages) {
    return engine.chat.completions.create({ messages, temperature: 0 });
  }

  async function chat(engine, messages) {
    const reply = await createCompletion(engine, messages);
    const content = reply && reply.choices && reply.choices[0] && reply.choices[0].message
      ? reply.choices[0].message.content
      : "";
    if (!content) throw new Error("The local model returned an empty response.");
    return content;
  }

  // Pull the first JSON object out of the model's reply. Small instruct models
  // often wrap JSON in ```json fences or add a sentence of preamble, so we strip
  // fences and, failing a clean parse, scan for the first balanced {...} block.
  function parseJsonObject(content) {
    const text = String(content || "").trim();
    // Strip a leading/trailing markdown code fence if present.
    const unfenced = text
      .replace(/^```(?:json)?\s*/i, "")
      .replace(/\s*```$/i, "")
      .trim();
    try {
      return JSON.parse(unfenced);
    } catch (e) {
      // Fall through to substring scan.
    }
    const start = unfenced.indexOf("{");
    if (start !== -1) {
      // Find the matching close brace, respecting strings/escapes.
      let depth = 0;
      let inStr = false;
      let esc = false;
      for (let i = start; i < unfenced.length; i++) {
        const ch = unfenced[i];
        if (esc) { esc = false; continue; }
        if (ch === "\\") { esc = true; continue; }
        if (ch === '"') { inStr = !inStr; continue; }
        if (inStr) continue;
        if (ch === "{") depth++;
        else if (ch === "}") {
          depth--;
          if (depth === 0) {
            const candidate = unfenced.slice(start, i + 1);
            try {
              return JSON.parse(candidate);
            } catch (e) {
              break;
            }
          }
        }
      }
    }
    throw new Error(`Model did not return valid JSON: ${text}`);
  }

  function extractQuery(content) {
    const obj = parseJsonObject(content);
    const query = obj.query == null ? "" : obj.query;
    if (typeof query !== "string") {
      throw new Error(`Model returned a non-string query: ${JSON.stringify(query)}`);
    }
    return query.trim();
  }

  // Validate against the real parser (the browser port of build_filter). An
  // empty query is valid (matches everything). Returns the parse-error message
  // on failure, or null on success.
  function validationError(query) {
    if (!query) return null;
    try {
      ConferenceSearch.buildPredicate(query);
      return null;
    } catch (e) {
      return e && e.message ? e.message : String(e);
    }
  }

  /**
   * Translate `text` into a validated boolean query.
   * Resolves to `{ query, repaired }`. Rejects if the model is unavailable
   * (e.g. WebGPU missing) or its output cannot be coerced into a valid query
   * after one repair round.
   */
  async function translate(text, fields, opts) {
    const options = opts || {};
    const request = (text || "").trim();
    if (!request) return { query: "", repaired: false };

    if (!isSupported()) {
      throw new Error(
        "AI search needs WebGPU, which this browser does not expose. " +
          "Try a recent Chrome or Edge, or use the boolean search box."
      );
    }

    const engine = await getEngine(options.onProgress);

    let content = await chat(engine, buildMessages(request, fields));
    let query = extractQuery(content);
    let err = validationError(query);
    if (!err) return { query, repaired: false };

    // One repair round: feed the parse error back to the model.
    content = await chat(engine, buildMessages(request, fields, err));
    query = extractQuery(content);
    err = validationError(query);
    if (err) {
      throw new Error(`Could not produce a valid query (last error: ${err}).`);
    }
    return { query, repaired: true };
  }

  window.ConferenceNL = { isSupported, translate, MODEL_ID };
})();
