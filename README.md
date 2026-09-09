# Fact Digger

**Live** https://fact-digger.navaneethhk.in · **Repo** https://github.com/Navaneeth-H-K/fact-digger · **Video** _(to be added)_

Extracts facts from PDFs, grounds each in a verbatim page quote, and links them across documents (corroborates / contradicts / superseded / context-explained / derived).

**Sections**
- **Setup and run** — install, run, test; key-free vs live modes; env vars.
- **The four required cases** — one demonstrated example of each, with evidence.
- **Approach** — pipeline, architecture, key decisions, AI tools used.
- **Limitations and next steps** — known weaknesses and roadmap.
- **Additional notes** — context conservation, latency fix, platform constraints.

Live instance is preloaded with the six starter PDFs: 3,419 facts, 4,709 relations, 511 pages.

## Setup and run

Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt      # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux
copy .env.example .env                                  # cp on macOS/Linux
.venv\Scripts\python -m uvicorn app:app --port 8765     # → http://localhost:8765
bash scripts/check.sh                                   # ruff + mypy + pytest
```

- **Key-free (reviewers):** `LLM_MODE=replay`, `LLM_CACHE_DIR=samples/llm_cache` replays the committed run. `python scripts/run_local.py` rebuilds `samples/export.json` from cache — no API key. See [`samples/preview.json`](samples/preview.json) for a readable snapshot (counts + the four cases); the full run is `samples/export.json.gz`.
- **Live:** `LLM_MODE=live`, `LLM_PROVIDER=openai_compat`, `GEMINI_API_KEY=…`, `EXTRACT_MODEL=ADJUDICATE_MODEL=gemini-3.5-flash-lite`. Provider layer is model-agnostic (Anthropic / Groq / Ollama / vLLM also work).

| Env group | Variables (names only) |
|---|---|
| Local | `DATABASE_URL`, `STORAGE_BACKEND`, `LOCAL_STORAGE_DIR` |
| LLM | `LLM_MODE`, `LLM_PROVIDER`, `LLM_CACHE_DIR`, `EXTRACT_MODEL`, `ADJUDICATE_MODEL`, `GEMINI_API_KEY`/`GROQ_API_KEY`/`AGENTROUTER_API_KEY`, `PAGE_CONCURRENCY`, `BATCH_BUDGET_S`, `FISCAL_YEAR_START_MONTH` |
| Production (Supabase) | `DATABASE_URL` (pooler:6543), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_BUCKET` |

## Video demo

_(to be added — ≤3 min: a PDF processed live, then the four cases.)_ Meanwhile, all four are on the live site under **Relations** and **Facts**.

## The four required cases

From the committed run (`samples/export.json.gz`); open each on the live site. Evidence (page image + highlighted quote) is shown in-app for cases 1–3.

| Case | Fact | Verdict |
|---|---|---|
| **Corroborated, expressed differently** | Rel **#1** — Delhivery FY24 EBITDA: ₹127 crore (deck) vs ₹1,266 million (annual report) | Both normalize to ~₹1.27 bn → **corroborates** (rule) |
| **Genuine contradiction** | Rel **#3450** — India FY24 GDP growth: 8.2% (Delhivery AR) vs 9.2% (IMF) | Same entity/period/unit, incompatible → **contradicts** (LLM) |
| **Explained by context** | Rel **#9** — Delhivery EBITDA: ₹(452) cr FY23 vs ₹1,266 mn FY24 | Different periods → **context_explained** (`dimension=period`) |
| **Handled failure** | Failures tab | 105 quotes unmatched (kept, flagged *unverified*); 63 image-only (badged *visual evidence*). Never silently dropped |

## Approach

**Pipeline**
1. **Extract** — each page's image + text → model → candidate facts (entity, attribute, value, period, quote).
2. **Ground** — verify the quote against page text in code: exact → normalized → fuzzy → image-only → not-found.
3. **Normalize** — value/unit/scale/period → canonical keys; validate; de-duplicate; store with evidence.
4. **Link** — rules-first pairing; only ambiguous pairs go to the LLM adjudicator (with a hypothesis); plus arithmetic tie-outs and a per-fact vintage timeline.

**Architecture**
- FastAPI + dependency-free vanilla-JS UI, served together; deployed on Vercel (Seoul region).
- SQLAlchemy over SQLite (tests/local) or Postgres/Supabase (prod); PyMuPDF for inventory + page rendering; storage in local dir or Supabase.
- `fkl/`: `normalize`, `verify`, `compare` (pure logic) · `pipeline` · `llm/*` (provider-agnostic client, cache, prompts) · `api`. ~260 tests via `scripts/check.sh`.

**Key decisions**
- The LLM reads; our code decides — grounding, comparison, confidence and the failure log are deterministic.
- Rules-first linking finalizes most pairs without a model call; the LLM handles only the ambiguous minority.
- Forced tool-use JSON with one repair; a bad fact drops alone, never the page.
- Dynamic schema grown from the documents — nothing filename- or document-specific.
- Serverless-shaped ingestion: time-boxed resumable batches; browser-direct-to-storage upload.

**AI tools** — Built with Claude Code. Runtime: Google Gemini 3.5 Flash-Lite for both extraction and adjudication (OpenAI-compatible endpoint; 732 cached calls). Provider layer is model-agnostic.

## Limitations and next steps

- Corrupt PDF text layers (dropped ₹ glyph, shifted rows, image-only pages) cause the Case-4 grounding failures; reading the page image mitigates but doesn't fix it.
- Flash-Lite is fast and cheap but less accurate than a frontier model; free-tier rate limits cause `llm_quota` pauses (302 logged, retried).
- `context_explained` dominates (4,490); per-verdict precision is not formally measured — no eval harness yet.
- Next: labelled eval harness (precision/recall per verdict); qualifier miner; same-document contradictions; identifier-anchored entity resolution; evidence bounding boxes.

## Additional notes

- **Context conservation.** A living state-of-record doc let agent sessions resume without re-reading everything; a committed **record/replay LLM cache** makes the whole demo reproducible with no API key. Tests inject a fake model — deterministic and offline.
- **Latency fix.** Function ran in the US, Supabase DB in Seoul → every round-trip crossed the Pacific (~1.7 s for `SELECT 1`). Pinning the function to Seoul (`regions: ["icn1"]`) cut endpoint latency 3–5× (`/stats` ~2.9 s → ~0.25 s). Versioned assets + `no-cache` HTML shell = instant redeploys; a daily `/health` cron keeps Supabase from pausing.
- **Platform constraints.** Vercel 4.5 MB body → browser uploads straight to storage; 300 s limit → time-boxed batches; no disk → page images rendered on demand; Supabase pooler → `NullPool` + `prepare_threshold=None`.
- Credentials kept out of the repo; committed samples + cache allow evaluation without an account.
