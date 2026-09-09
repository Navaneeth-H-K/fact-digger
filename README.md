# Fact Digger

Upload PDFs. Get facts with page-level evidence, and see where documents **corroborate**,
**contradict**, or only *appear* to contradict each other because the period, scope, unit or
data vintage differs. Built for the Superjoin engineering-intern assignment, in the spirit of a
tick-and-tie: every number is traced to its source, tied out against other sources, and every
variance is explained or flagged.

> Live demo: https://fact-knowledge-layer.vercel.app · Video (≤3 min): _(link added at v1)_

## What it does

1. **Discovers facts.** Each page is rendered to an image and sent, together with its text
   layer, to a vision-capable model that returns candidate facts: entity, attribute, value as
   printed, unit, scale, period, estimate type (actual / advance estimate / revised / budget /
   projection / third-party), measurement basis, and a verbatim quote.
2. **Grounds them in code.** The quote is verified against the page text (exact, then
   normalised, then fuzzy). Values are normalised to base units (₹ million and ₹ crore become the
   same number), periods to date intervals (`FY24`, `2023-24`, `2023/24` and "year ended 31 March
   2024" become one key), names to canonical keys. Unverifiable quotes, image-only values,
   implausible numbers and malformed model output all land in a **failure log** that says what
   the system did about each.
3. **Compares them by rules first.** Candidate pairs are found across documents by entity and
   attribute tokens (no attribute names are hard-coded). A deterministic rule engine settles
   what it can: same period and value → *corroborates*; different periods → *explained by
   period*; a later publication revising the same period → *superseded* with the newer fact as
   current; forecast vs measurement → *explained by estimate type*. Only the ambiguous residue,
   such as a same-period value conflict or a basis difference, goes to a second model call that
   returns a verdict and a short explanation citing both quotes.
4. **Ties out arithmetic.** Parts printed in one document that sum to a total printed in
   another become *derived* relations (for example team size + partner agents = workforce).
5. **Shows its work.** A single-page UI lists facts with evidence (page image with the quote
   highlighted in the text layer), relations grouped by verdict with both facts side by side,
   a vintage timeline per fact, the failure log, and the attribute vocabulary discovered so far.

The model reads; the code decides. Every verdict is traceable to a rule or to a stored model
explanation, and every fact to a page.

## Setup and run

Requirements: Python 3.12. An API key is needed only to process new documents live.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt        # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux
copy .env.example .env                                   # then edit
.venv\Scripts\python -m uvicorn app:app --port 8765
```

Open http://127.0.0.1:8765 and drop a PDF on the page.

`.env` settings that matter:

| Variable | Meaning |
|---|---|
| `LLM_MODE` | `live` (call the model), `record` (call and cache responses), `replay` (cache only, no key needed) |
| `AGENTROUTER_API_KEY` / `AGENTROUTER_BASE_URL` | Anthropic-compatible endpoint for Claude (any Anthropic-compatible URL works) |
| `EXTRACT_MODEL` / `ADJUDICATE_MODEL` | model per stage; the committed run used `claude-opus-4-8` for both, the only Claude model routed to the key used |
| `LLM_PROVIDER` | `anthropic` (default) or `openai_compat` to use a local model through Ollama/vLLM (`OPENAI_COMPAT_BASE_URL`) |
| `DATABASE_URL` | SQLite file locally; a Postgres URL in production |
| `STORAGE_BACKEND` | `local` directory or `supabase` bucket |

Reproduce the committed results without a key: keep `LLM_MODE=replay` and
`LLM_CACHE_DIR=./samples/llm_cache`, start the server, then upload the starter PDFs from
`data/starter/` (or run the script below). The cached model responses replay exactly.

End-to-end from the command line:

```bash
.venv\Scripts\python scripts/run_local.py --base http://127.0.0.1:8765 data/starter/delhivery/*.pdf data/starter/india-macroeconomy/*.pdf
```

Tests, lint and types: `bash scripts/check.sh` (ruff, mypy --strict, pytest; 260+ tests run in a
few seconds with SQLite in memory and a fake model).

## Video demo

_(3-minute link: a PDF processed live on the site, then the four cases below.)_

Live instance: **https://fact-knowledge-layer.vercel.app** — already loaded with the six starter
documents (3,419 facts, 4,709 cross-document relations). Open **Relations** and filter by verdict, or
**Facts** and click any row to see its page image with the quote highlighted.

## The four required cases

All four come from the committed run over the six starter PDFs (`samples/export.json.gz`). Each is a
real relation with an id you can open on the live site at `/relations` or `/facts/{id}`.

1. **Corroborated across documents, expressed differently** — relation **#1**. FY24 EBITDA is printed as
   **₹127 crore** in the Q4 earnings deck and **₹1,266.41 million** in the annual report. Different unit
   systems (crore vs million), same quantity: the normaliser converts both to ₹1.27 billion and the rule
   engine marks them *corroborates*. Evidence: the quote on each source page.
2. **Genuine / likely contradiction** — relation **#3450**. India's FY24 GDP growth is stated as
   **8.2%** (quoted in the Delhivery annual report's macro section) and **9.2%** (IMF Article IV, 2023/24).
   Same country, same year, same unit, materially different. Rules flag it as a same-key value conflict
   and the adjudicator confirms *contradicts*, citing both quotes.
3. **Apparent contradiction explained by context** — relation **#9** and thousands like it. EBITDA reads
   **₹(452) crore** and **₹1,266 million**; rather than a conflict, the engine sees the periods differ
   (**FY23** vs **FY24**) and returns *context_explained* with `dimension = period`. The macro set adds
   scope and vintage cases (e.g. first vs second advance estimates), the largest verdict class at 4,490.
4. **An extraction / reasoning failure and how it was handled** — the **Failures** tab. The text layers of
   these PDFs are corrupt: the ₹ glyph is dropped or rendered as a stray letter, table rows shift, some
   pages are pure images. Two honest outcomes are logged: **105 facts** whose value was read but whose
   verbatim quote could not be matched against the mangled text (kept, flagged *unverified*, and their
   relations down-weighted to half confidence); and **63 facts** read from the page image because the
   text layer held nothing (kept and badged *visual evidence*). Nothing is silently dropped.

## Approach

### Architecture

```
browser ──signed PUT──► object storage (Supabase bucket; a local directory in dev)
   │ ticket / finalize / process / link                     ▲ bytes on demand
   ▼                                                        │
FastAPI ──► Postgres or SQLite: documents · pages · facts · relations · failures · llm_cache
   │  POST /documents/{id}/process   time-boxed batch: claim pages → image+text → model → verify → normalise → store
   │  POST /link                     rule pass on new facts → relations; adjudicate pending pairs (time-boxed)
   └─ LLM provider (Anthropic-compatible or OpenAI-compatible) with retry/backoff and record/replay cache
```

Everything is synchronous and resumable. Processing is split into short, idempotent batch calls
because the target host (Vercel) limits a request to a few minutes: each call claims the next
pending pages atomically, runs a few in parallel, commits every page in its own transaction, and
reports progress; the UI or the script simply calls again until nothing is pending. A page that
fails is retried on a later call, up to three times, and every failure is logged.

### Important decisions and trade-offs

- **Page image + text, not text alone.** The starter PDFs are born-digital but their text layers
  are treacherous: table rows shift by one (a CPI row would read 32.3%), two-column pages splice
  sentences across the gutter, the rupee glyph is deleted or rendered as `K`, `J` or `I`, chart
  labels come out in bar-height order, several pages are pure images. The model therefore sees
  the rendered page and is told to trust the image for what a number is and use the text only to
  copy quotes. Cost: ~2.5k image tokens per page. Benefit: tables and charts become readable.
- **Verification in code, not trust.** A fact whose quote cannot be found on the page is kept
  but flagged, its relations get half confidence, and it appears in the failure log. Values read
  from the image alone are badged "visual evidence".
- **A fact key is more than entity + attribute + value.** Period, estimate type, measurement
  basis and unit are part of the key, because in these documents the same attribute legitimately
  carries different values for different periods (quarter vs nine months vs year), different
  vintages (first vs second advance estimates), and different bases (customs vs balance-of-
  payments trade data, capacity vs generation, "authorities' definition" vs staff definition).
- **Rules first, model second.** Most pairs are settled deterministically and cheaply; the
  adjudicator sees only pairs the rules cannot explain, with the rule engine's hypothesis. This
  keeps the interesting verdicts (contradictions) model-confirmed and the bulk cheap and
  auditable.
- **Five verdicts, not two.** `superseded` (same fact, later vintage wins, history kept) and
  `context_explained` (with the differing dimension named) are what turns "apparent
  contradictions" into explanations instead of noise; `unresolved` is an honest third state.
- **Dynamic schema.** No attribute list exists in code. The vocabulary discovered so far is fed
  back into the extraction prompt so later documents reuse earlier names, and `/schema` shows it.
- **Incremental by construction.** Linking pairs only facts not yet paired against the whole
  layer, so adding a document never recomputes existing relations.
- **Reproducible without a key.** A record/replay cache keyed on the request (image bytes
  hashed) is committed for the starter set, so reviewers can replay the exact model outputs.
- **Deliberately not built in v1:** same-document contradictions, entity resolution on
  identifiers (DIN/CIN), footnote attachment, embeddings for pairing recall. See next steps.

### The four required cases

_(Filled from the starter-set run at v1, with fact and relation ids linking into the live UI.)_

| Case | Example | Evidence and reasoning |
|---|---|---|
| Corroborated across documents, expressed differently | _pending_ | |
| Genuine or likely contradiction | _pending_ | |
| Apparent contradiction explained by context | _pending_ | |
| Extraction or reasoning failure and how it was handled | _pending_ | |

### AI tools used

- **Building:** Claude Code (Claude Fable 5.1) as the coding agent, working test-first from a
  written plan; every module was written against a failing test, then linted and type-checked
  before commit.
- **At runtime:** Claude Opus 4.8 for both page extraction and adjudication, reached through
  AgentRouter's Anthropic-compatible endpoint (the models are configurable per stage; Sonnet was
  planned for extraction but is not routed on the key used). Any OpenAI-compatible local model can
  be substituted with `LLM_PROVIDER=openai_compat`.

## Limitations and next steps

**The run and its numbers.** The six starter PDFs (511 pages) were processed with Google
**Gemini 2.5 flash-lite** through its OpenAI-compatible endpoint, producing **3,419 facts** and
**4,709 cross-document relations** (87 corroborates, 5 contradictions, 59 superseded, 4,490
context-explained, 39 arithmetic tie-outs, 29 unresolved) plus **870 logged failures**. The whole
run and the responses are committed (`samples/export.json.gz`, `samples/llm_cache/`), so the results
can be inspected, and the starter PDFs reprocessed in `LLM_MODE=replay`, without an API key.

**Model choice was pragmatic, not ideal.** Frontier vision models (Claude, GPT-4o) read these dense
financial tables best but need paid credit. Among no-cost options, Gemini flash-lite was the only one
that combined good page reading, reliable structured output, and a workable free rate limit; its
newest sibling gives only 20 requests/day, Groq's free tier capped at 200k tokens/day and mishandled
tool calls, and a local model did not fit the available 4 GB GPU. The provider layer is model-agnostic
(`LLM_PROVIDER` + `*_MODEL`), so a better key changes only configuration.

**Precision is looser than recall.** With 3,419 facts, the rule engine finds many true relations but
also some noise: a few arithmetic tie-outs are numeric coincidences, and a handful of pairs compare
related-but-distinct metrics (EBITDA vs Adjusted EBITDA). The five demonstrated cases are hand-picked
clean examples; a v2 evaluation harness would measure and tighten this.

Known limits of v1:

- Only cross-document relations are computed; a document contradicting itself (which the
  Delhivery annual report does, for example two dates for one NCLT order) is not yet reported.
- Entity matching is by name tokens, not identifiers; three spellings of one director's name are
  three entities until the identifier-based resolution planned for v2.
- Footnotes are part of the page text the model sees, but they are not attached to facts as
  structured qualifiers, so some basis differences reach the adjudicator instead of the rules.
- Text and range-valued facts are stored but not paired.
- Arithmetic tie-out only looks for two parts and one total across documents.

Next (v2): qualifier miner for phrases like "excluding X" and "on a pro forma basis";
same-document contradiction detection; identifier-anchored entity resolution; footnote
attachment; a tick-and-tie report per target document exportable to CSV/XLSX; evidence bounding
boxes on the page image; an evaluation harness with a labelled pair set reporting precision and
recall per verdict.

## Deployment

The live instance runs on Vercel's Python runtime with Supabase for Postgres and file storage:

- `app.py` is the entrypoint; dependencies come from `pyproject.toml`; `vercel.json` raises the
  function limit to 120 s and adds a daily cron on `/health` so the free Supabase project never
  pauses for inactivity.
- Uploads never pass through Vercel: the API issues a signed Supabase Storage URL and the browser
  PUTs the file there, so the 4.5 MB request limit does not apply.
- Processing and linking are time-boxed (45 s per call by default) and resumable, so the client
  loops until nothing is pending; a killed function loses at most the pages in flight.
- Environment variables (set in Vercel, never committed): `DATABASE_URL` (Supabase transaction
  pooler, port 6543), `STORAGE_BACKEND=supabase`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `SUPABASE_BUCKET`, `LLM_MODE=live`, `AGENTROUTER_API_KEY`, `AGENTROUTER_BASE_URL`,
  `EXTRACT_MODEL`, `ADJUDICATE_MODEL`.
- `scripts/sync_to_production.py` seeds the live layer from a local run with ids preserved, so
  the committed `samples/export.json` and the live site agree.

## Additional notes

- The starter PDFs under `data/starter/` are public filings and reports; each folder's README
  records provenance and which original pages the excerpts retain. Printed page numbers jump
  and even repeat, so evidence is keyed on the PDF page index and printed labels are shown only
  for display.
- Nothing in the code refers to Delhivery, India, rupees or any specific document. The small
  vocabulary of generic finance acronyms (GDP, CPI, FDI, EBITDA, YoY) in `fkl/normalize.py` is
  the only domain knowledge, and it only improves recall.
- `CONTEXT.md` is the living state-of-record used during development.
