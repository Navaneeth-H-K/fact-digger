# Fact Digger

Important facts are scattered across documents, written differently, supported in one place and
contradicted in another. Fact Digger ingests PDFs, extracts numerical and semantic facts, ties every
fact to a verbatim quote on its source page, and links facts across documents as **corroborating**,
**contradicting**, **superseded**, **explained by context**, or **arithmetically derived**.

**Live:** https://fact-digger.navaneethhk.in (fallback: https://fact-knowledge-layer.vercel.app) ·
**Repo:** https://github.com/Navaneeth-H-K/fact-digger · **Video:** _(to be added)_

The live instance is preloaded with the six starter PDFs: **3,419 facts** and **4,709 cross-document
relations** over **511 pages**. Open **Facts** and click any card to see its page image with the quote
highlighted; open **Relations** and filter by verdict.

## Setup and run

Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt      # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux
copy .env.example .env                                  # cp on macOS/Linux
```

Run the app, then open http://localhost:8765:

```bash
.venv\Scripts\python -m uvicorn app:app --port 8765
```

**Reviewing without an API key.** The full demo run is committed as a record/replay cache, so you can
reproduce it offline. In `.env` set `LLM_MODE=replay` and `LLM_CACHE_DIR=samples/llm_cache`, then either
browse the seeded local layer or rebuild the export:

```bash
.venv\Scripts\python scripts/run_local.py              # uploads data/starter PDFs, processes, links, writes samples/export.json
```

**Running live.** Set `LLM_MODE=live` and a provider. The committed run used Google Gemini:
`LLM_PROVIDER=openai_compat`, `GEMINI_API_KEY=…`, `EXTRACT_MODEL=ADJUDICATE_MODEL=gemini-3.5-flash-lite`.
The provider layer is model-agnostic — Anthropic-compatible endpoints and any OpenAI-compatible server
(Groq, Ollama, vLLM) also work.

**Tests / quality gate** (ruff, ruff format, mypy, pytest):

```bash
bash scripts/check.sh
```

**Environment variables** (names only; see `.env.example`):

| Group | Variables |
|---|---|
| Local | `DATABASE_URL` (SQLite or Postgres), `STORAGE_BACKEND` (`local`/`supabase`), `LOCAL_STORAGE_DIR` |
| LLM | `LLM_MODE` (`off`/`live`/`record`/`replay`), `LLM_PROVIDER` (`anthropic`/`openai_compat`), `LLM_CACHE_DIR`, `EXTRACT_MODEL`, `ADJUDICATE_MODEL`, `GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENAI_COMPAT_BASE_URL` / `AGENTROUTER_API_KEY`, `PAGE_CONCURRENCY`, `BATCH_BUDGET_S`, `FISCAL_YEAR_START_MONTH` |
| Production (Supabase) | `DATABASE_URL` (transaction pooler, port 6543), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_BUCKET` |

## Video demo

_(to be added — ≤3 minutes: a PDF processed live, then the four cases below.)_ Until then, the four cases
are all reachable on the live instance via the **Relations** and **Facts** tabs.

## The four required cases

All from the committed run over the six starter PDFs (`samples/export.json.gz`); each is a real relation
you can open on the live site at `/relations` (or a fact at `/facts/{id}`). Source evidence — the page
image with the quote highlighted — is shown in the UI for the first three.

| Case | What | Evidence | System's reasoning |
|---|---|---|---|
| **1. Corroborated, expressed differently** | Relation **#1** — Delhivery FY24 EBITDA: **₹127 crore** (Q4 deck) vs **₹1,266 million** (annual report) | Verbatim quote on each source page | Normaliser converts both units to ~₹1.27 billion; same period/basis/value → **corroborates** (rule) |
| **2. Genuine contradiction** | Relation **#3450** — India FY24 GDP growth: **8.2%** (Delhivery AR macro section) vs **9.2%** (IMF Article IV, 2023/24) | Both quotes cited in the verdict | Same entity, period and unit, incompatible values → adjudicator confirms **contradicts** (LLM) |
| **3. Apparent contradiction, explained by context** | Relation **#9** — Delhivery EBITDA **₹(452) crore (FY23)** vs **₹1,266 million (FY24)** | Both source pages | Periods differ → **context_explained**, `dimension = period` (rule). This is the largest class (4,490 relations) |
| **4. Extraction/reasoning failure, handled** | The **Failures** tab | Logged with stage, kind, and what the system did | Corrupt text layers: **105** facts whose value was read but whose quote couldn't be matched (kept, flagged *unverified*, relations down-weighted) and **63** read from the page image only (kept, badged *visual evidence*). Nothing is silently dropped |

## Approach

**How it works.** A PDF is uploaded straight to storage and inventoried page by page. For each page the
model receives the **rendered image and the text layer** and returns candidate facts (entity, attribute,
value, period, a verbatim quote). Our code then does the deciding: it **verifies the quote against the
page text** (exact → normalized → fuzzy → image-only → not-found), normalizes value/unit/scale/period into
canonical keys, runs validators, de-duplicates, and stores the fact with its evidence. Linking is
**rules-first**: candidate pairs are blocked by entity/attribute/period, compared deterministically, and
only genuinely ambiguous pairs are sent to the LLM adjudicator with a hypothesis. Arithmetic tie-outs
(sums that reconcile across documents) and a per-fact vintage timeline round out the relations.

**Architecture.** FastAPI backend + a dependency-free vanilla-JS single-page UI, served together.
SQLAlchemy over SQLite (tests/local) or Postgres (Supabase, production); PDFs in local disk or Supabase
Storage; PyMuPDF for inventory and page rendering. Modules under `fkl/`: `normalize`, `verify`, `compare`
(pure, unit-tested logic), `pipeline` (extraction + linking), `llm/*` (provider-agnostic client, cache,
prompts), `api` (routes). Deployed on Vercel; ~260 tests run under `scripts/check.sh`.

**Key decisions and trade-offs.**
- **The LLM reads; our code decides.** Quote verification, normalization, comparison, confidence and the
  failure log are deterministic. The model extracts and adjudicates; it never has the last word on grounding.
- **Rules-first linking** finalizes most pairs without a model call — cheaper, faster, and auditable; the
  LLM is reserved for the genuinely ambiguous minority.
- **Forced tool-use JSON** with one repair round-trip; a bad fact is dropped individually, never the page.
- **Dynamic schema.** No attribute list is hard-coded; the vocabulary grows from the documents and feeds
  back into later extraction (visible on the **Schema** tab). Nothing is filename- or document-specific.
- **Serverless-shaped ingestion:** time-boxed, resumable batches; browser-direct-to-storage upload.

**AI tools used.** Built with **Claude Code**. At runtime, the committed run used **Google Gemini 3.5
Flash-Lite** through its OpenAI-compatible endpoint for **both** page extraction and pair adjudication
(732 cached calls: 514 extract, 212 adjudicate, 6 metadata). The provider layer is model-agnostic.

## Limitations and next steps

- **PDF text layers are corrupt.** The ₹ glyph is dropped or rendered as a stray letter, table rows shift,
  and some pages are pure images. This is the source of the grounding failures in Case 4; reading the page
  image (not just the text) mitigates but does not eliminate it.
- **Flash-Lite is fast and cheap but not a frontier model.** Extraction and adjudication are less accurate
  than a larger model would be. Free-tier rate limits also cause `llm_quota` pauses (302 logged), which the
  pipeline retries rather than failing.
- **`context_explained` dominates (4,490).** Many are legitimately period-differing pairs, but precision
  per verdict is **not formally measured** — there is no evaluation harness yet.
- **Next:** a labelled eval harness with per-verdict precision/recall; a qualifier miner
  (excluding/including/pro-forma/restated); same-document contradiction detection; identifier-anchored
  entity resolution (DIN/CIN); evidence bounding boxes on the page image.

## Additional notes

- **Conserving context during development.** The build ran across many agent sessions. Two things kept it
  coherent and reproducible: a living *state-of-record* document that let each session resume without
  re-reading the whole history, and a **record/replay LLM cache** (committed under `samples/llm_cache`) that
  lets the entire demo be rebuilt with **no API key** (`LLM_MODE=replay`). Tests inject a fake model, so the
  suite is deterministic and offline.
- **Cutting site latency.** The site was slow because the function ran in a US region while the Supabase
  database is in Seoul, so every DB round-trip crossed the Pacific — a bare `SELECT 1` took ~1.7 s. Pinning
  the Vercel function to Seoul (`vercel.json` `regions: ["icn1"]`) cut endpoint latency 3–5× (`/stats`
  ~2.9 s → ~0.25 s). Static assets are versioned and the HTML shell sends `Cache-Control: no-cache`, so a
  redeploy is picked up immediately; a daily cron on `/health` keeps Supabase's free tier from pausing
  after 7 idle days.
- **Platform constraints shaped the design.** Vercel's 4.5 MB request limit → the browser uploads PDFs
  straight to storage; the 300 s function limit → time-boxed, resumable batches; no persistent disk → page
  images are rendered on demand. The Supabase pooler needs `NullPool` and `prepare_threshold=None`.
- **Credentials are kept out of the repository.** The committed export and cache let the project be
  evaluated without any account or key.
