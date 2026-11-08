<div align="center">

# Yunhai Ruiping · AI_Grade

**Weeks of performance-evaluation paperwork, compressed into one conversation**

A multi-step LangGraph agent that turns public-sector performance evaluation — *read the
files, design the indicators, score every item, write the report* — into an auditable,
resumable, human-supervised pipeline.

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.0.20%2B-1C3C3C?logo=langchain&logoColor=white)](https://github.com/langchain-ai/langgraph)
[![MySQL](https://img.shields.io/badge/MySQL-8.0-4479A1?logo=mysql&logoColor=white)](https://www.mysql.com/)
[![Tests](https://img.shields.io/badge/tests-274%20passed-brightgreen)](#tests)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

[English](./README.md) · [简体中文](./README.zh-CN.md)

[Quick start](#quick-start) · [Runtime flow](#runtime-flow) · [API reference](./docs/API.md) · [Contributing](./CONTRIBUTING.md) · [Security](./SECURITY.md)

<img src="docs/assets/workflow.svg" alt="Animated runtime flow: materials are uploaded, FastAPI streams a LangGraph state graph of 15 nodes, three steps pause for human confirmation, pure-program guardrails check the output, and the report is exported to Word or PDF" width="760">

<sub>The sweep above is the real execution order — 15 nodes, 3 human gates, and a guardrail layer that never calls a model.</sub>

</div>

---

## What this is

**Yunhai Ruiping** targets performance evaluation in finance, audit and legislative settings:
project expenditure, whole-of-department, sub-national government operations, special-purpose
bonds, social-security funds. Upload the source material and the system produces the document
checklist, core-information extraction, a three-level indicator system, score allocation,
scoring criteria, per-item scores, problem diagnosis, recommendations and a comprehensive
conclusion — assembled into an exportable Word / PDF report.

It is not a wrapper that asks a model to "write a report". The effort here goes into
**marking the untrustworthy parts of the output instead of smoothing them over**:

- every score must land on a specific line, section, or retrieved evidence — otherwise it is
  listed separately and kept **out** of the total;
- every JSON output passes a shape check, gets one repair attempt, and falls back to
  **manual review** — never to a silent empty result;
- every document set declares *how much text it had, how much was actually used, and what was
  left out* — instead of pretending it read everything;
- every request carries a `request_id`, returned to the user on failure, so one log line
  locates the whole chain.

---

## Runtime flow

Fifteen nodes: an initialisation node plus fourteen evaluation steps. Every node writes into a
shared, typed state, and every step can be re-run or hand-edited on its own.

| # | Step | Output |
|:--:|------|--------|
| 0 | Initialise | Parsed PDF / Word / Excel / images, per-file line counts |
| 1 | Required documents | Checklist of what the evaluation needs |
| 2 | Core information | Extracted facts, categorised for later retrieval |
| 3 | Indicator system | Three-level indicators — ◆ human gate |
| 4 | Score allocation | Point budget per indicator |
| 5 | Scoring criteria | What full / partial / zero credit means |
| 6 | Indicator table | The rendered evaluation table — ◆ human gate |
| 7 | Scores & analysis | Per-indicator score + reason + citation anchor |
| 8 | Lessons learned | What worked, with supporting evidence |
| 9 | Problems & causes | Issues, root causes, evidence |
| 10 | Recommendations | Mapped from problem IDs to actions |
| 11 | Comprehensive conclusion | Overall rating — ◆ human gate |
| 12 | Basic information | Project background section |
| 13 | Work profile | How the evaluation was carried out |
| 14 | Final report | Assembled document, consistency-checked |

◆ marks the three `interrupt()` checkpoints. The `/workflow/approve` endpoint is wired and the
graph structure supports pausing, but the current build runs straight through by default —
turning a gate on does not require touching the flow.

Each step's result is stored in `step_results` with its body text, `structured_status`, the
pre-repair `raw_content` (if any) and that step's evidence set — so any step can be inspected,
rewritten by the model, or exported individually.

---

## Core capabilities

| Capability | What it does |
|---|---|
| **15-node state graph** | LangGraph `StateGraph`, typed shared state, per-step re-run and manual rewrite |
| **Six indicator types** | Project expenditure / whole-of-department / sub-national government / special bond / social-security fund / custom — the type actually changes generation |
| **Explainable scoring** | Six citation-anchor classes, seven disposition states; an anchor that resolves to a line *not* in this step's evidence becomes weak and is **excluded from the total** |
| **Structured-output fallback** | Shape check → one repair → `manual_review`; a repair may not shrink the result, and the original body is archived |
| **Long-document coverage** | Locator-aware chunking, head/tail budget, coverage statement, compressed summary of unused text; `used + omitted == total` reconciles |
| **SSE streaming** | Token-level deltas, `seq` de-duplication, `Last-Event-ID` resume, comment-frame heartbeat |
| **Cross-step consistency** | Pure program, zero tokens: score totals, rating-vs-score contradictions, self-reported scores that disagree with computed ones |
| **Real export** | Word via `python-docx`, PDF via `reportlab` with native CJK; font, size, spacing and margin options |
| **Restart-safe** | Session in MySQL plus a `state_backups/*.json` disk projection — past reports stay viewable, exportable, editable |
| **No build step** | Plain HTML/CSS/JS with shared modules: no npm, no bundler, no `node_modules` |

---

## What the program guarantees — and what it does not

This distinction is in the README so a reader knows which claims can be cited and which must be
reviewed by a human.

**Program-guaranteed (enforced by code, covered by tests)**

- Resource ownership: workflows, reports, questionnaires and exports are checked against
  `user_id` — anonymous gets 401, cross-user gets 404;
- Idempotent start: one `session_id` registers once, a repeat start returns 409;
- Scoring discipline: model output must pass ID / range / evidence checks before entering the
  structured result; anything missing is marked pending;
- Evidence integrity: an evidence item must appear verbatim in the source; a citation must
  resolve to this step's evidence pool or a locator in the source;
- Transport: every SSE frame carries `seq`, `Last-Event-ID` resumes, heartbeats are comment
  frames;
- Traceability: every response carries `X-Request-ID`, error bodies echo `request_id`, and the
  third log segment is that ID;
- Export idempotency: identical content and layout render once per 10 minutes, `reused=true`;
- Structured output: one repair, otherwise `manual_review` with the original text preserved;
- Coverage arithmetic: `used_chars + omitted_chars == total_chars`, and the declared numbers
  match what was actually put into the prompt.

**Model-dependent (the program can flag, not guarantee)**

- Factual accuracy of prose: amounts, dates and outputs match the source only through evidence
  citation plus human review;
- Professional judgement in indicator design: levels and weights are domain calls;
- Feasibility of recommendations: the program checks the `issue_id → suggestion_ids` coverage,
  not whether the advice is any good;
- Sufficiency of scoring rationale: the program checks that evidence exists, not that the
  argument holds;
- Full coverage of very long documents: beyond the budget, text is still truncated —
  map-reduce is not implemented.

**Deployment semantics**

- Progress and token deltas live inside the process that accepted the request — **single
  worker**;
- Checkpoints are an in-process `MemorySaver`: an unfinished run cannot be resumed after a
  restart (completed steps and manual edits are recovered from MySQL / the disk projection).

---

## Structured output: validate → repair → manual review

Steps 3, 4 and 7 require JSON. All three share one path:

1. **Parse** the code block, or the first complete object, out of the model response.
2. **Shape-check** the top-level structure, container types and whether any usable entry exists.
3. **Repair once**, feeding back the specific error plus the original body — never "make it
   shorter".
4. **Shrink gate**: the repaired version must not contain fewer entries, nor fewer numeric
   scores, than the original. A repair that shrinks is rejected and the original is kept for
   manual review.
5. **Manual review** otherwise: the full original output is preserved with a warning. No regex
   improvisation, no silent empty result.

Two failure modes were measured and closed here, because both were real:

- A repair prompt that told the model "rather write fewer entries than invent one" was observed
  cutting 20 entries down to 5 — the instruction now forbids dropping anything;
- Echoing the original body back was capped at 6,000 characters, so a normal ~12,000-character
  step-7 output let the model see only 11 of 20 indicators. The cap is now per-step, and the
  shrink gate compares against the original JSON rather than against body length: one real
  case went 12,608 → 2,745 characters with all 26 entries and all 6 scores intact.

---

## Scoring: why a score lands or does not

Model-written citations are extremely varied, and a binary accept/reject would both reject valid
citations and admit fabricated ones. So resolution is graded, and disposition is recorded per
indicator:

| Anchor class | Resolves against | Disposition |
|---|---|---|
| `evidence_id` | this step's evidence pool | `accepted` — counted |
| `material_id` | an ID issued in the prompt | `accepted` — counted |
| `evidence_locator` | file name + line number from retrieval | `accepted` — counted |
| `section_ordinal` | "part N / chapter N" mapped back to the source | `accepted` — counted |
| `heading` | quoted heading text found in the source | `accepted` — counted |
| `material_locator` | a line number inside the source, but not in this step's evidence | `anchor_weak` — visible, **not counted** |

| Disposition | Meaning | Counted |
|---|---|:--:|
| `accepted` | Citation resolves, entry usable | ✅ |
| `model_pending` | Model itself says "insufficient material / to be verified" | ❌ |
| `anchor_weak` | Resolves to a source line that is not in this step's evidence | ❌ |
| `anchor_unresolved` | Citation cannot be resolved at all | ❌ |
| `score_invalid` | Score is not a legal number (e.g. "very high") | ❌ |
| `reason_missing` | No scoring rationale | ❌ |
| `not_mentioned` | Model never mentioned this indicator | ❌ |

Why keep weak anchors out of the total? In one measured run, 3 of 4 "paragraph 1" citations
carried the model's own reason "cannot evaluate, provisionally scored at the 1.00 baseline".
Letting them through would have raised the total from 30.0 to 33.0 — a better-looking number
that would have been false. They get their own identity instead: **visible, checkable, not
pretended.**

---

## Streaming

- Every frame carries an auto-incrementing `id`; the client de-duplicates and drops
  out-of-order frames.
- The in-process `progress_bus` keeps the last 400 frames for replay; heartbeats are comment
  frames so proxies do not time the connection out.
- Reconnect with `GET /workflow/{id}/stream` and `Last-Event-ID: n` to receive frames from
  `n+1` onwards.
- Because the bus is in-process, the service is **single-worker** today; moving to Redis pub/sub
  keeps the wire protocol unchanged.

---

## Quick start

### Docker

```bash
cp .env.example .env                  # at minimum: one LLM API key + MySQL connection
docker build -t ai-grade .
docker run -d --name ai-grade -p 8000:8000 --env-file .env ai-grade
```

`MYSQL_HOST` in `.env` must be reachable **from inside the container**: use
`host.docker.internal` on Windows / macOS, or the host's LAN IP (or `--network host`) on Linux.

Open <http://localhost:8000>. Default administrator is `admin` / `Admin@123456` — **change it
immediately**.

> The app does not need Redis today; `docker-entrypoint.sh` only waits for Redis when
> `REDIS_URL` is explicitly set.

### Local

```bash
# 1. Python 3.9+ (the image uses 3.11) and MySQL 5.7+/8.0
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt

# 2. Configure
cp .env.example .env            # at minimum one *_API_KEY and the MySQL settings

# 3. Create the database (tables are created on first start)
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS ai_grade_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 4. Run
python -m backend.main          # or .\start_backend.ps1
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"healthy","service":"云海睿评 - AI绩效评价报告生成系统","version":"1.0.0"}
```

### Pages

| Page | Path | Notes |
|------|------|-------|
| Landing | `/` | Product overview and entry points |
| Login / register | `/login` | Supports staying signed in |
| Client workspace | `/client` | Evaluations, assistant, reports, questionnaires, settings |
| Admin | `/admin` | Users, models, prompts, knowledge base, monitoring, audit |
| Standalone workflow | `/workflow` | Minimal workflow page |
| Report editor | `/report?session=<id>` | Preview and edit in place |
| Questionnaire | `/questionnaire?session=<id>` | Standalone form |

---

## Configuration

The full list lives in [`.env.example`](./.env.example); the important ones:

| Variable | Meaning | Default |
|---|---|---|
| `MYSQL_HOST` / `PORT` / `USER` / `PASSWORD` / `DATABASE` | Database connection | `localhost` / `3306` / `root` / — / `ai_grade_db` |
| `JWT_SECRET_KEY` | Token signing key, **must change in production** | placeholder |
| `JWT_ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Token policy | `HS256` / `1440` / `30` |
| `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM keys (at least one) | — |
| `DEFAULT_MODEL` | Model used when none is chosen | `deepseek-chat` |
| `TEMPERATURE` / `TOP_P` / `MAX_TOKENS` | Generation parameters | `0.7` / `0.9` / `4000` |
| `ALLOWED_ORIGINS` | CORS allow-list (comma separated) | `localhost:3000,5173,8000` |

> The model list only shows providers with a **valid configured key**; `your_*` placeholders
> are filtered out. An empty model list almost always means the key is not filled in.

---

## Project layout

<details>
<summary>Expand the tree</summary>

```
AI_Grade/
├── backend/
│   ├── agents/
│   │   ├── performance_agent.py      # 15-node workflow (LangGraph)
│   │   ├── states/performance_state.py
│   │   ├── tools/
│   │   │   ├── structured_output.py   # shape check + one repair + shrink gate
│   │   │   ├── content_indexer.py     # content index
│   │   │   ├── score_validator.py     # four-axis validation
│   │   │   └── export_service.py      # Word / PDF export
│   │   └── prompts/                   # DB first, built-in fallback
│   ├── routes/                        # agent / auth / client / admin (135 endpoints)
│   ├── services/
│   │   ├── evidence_anchor.py         # six citation-anchor classes
│   │   ├── retrieval.py               # BM25 + Chinese bigrams
│   │   ├── doc_coverage.py            # long-document coverage
│   │   ├── consistency.py             # cross-step consistency
│   │   ├── progress_bus.py            # in-process progress bus
│   │   └── export_cache.py            # export idempotency cache
│   ├── middleware/                    # auth / request_id / rate limit
│   ├── models/  utils/  config.py  database.py  main.py
├── frontend/                          # no build: plain HTML/CSS/JS
│   ├── shared/                        # config / api / store / ui / workflow-controller
│   ├── client/  admin/  workflow/  report/  questionnaire/  login/
├── tests/                             # pytest + node:test, 274 checks
│   └── rag_eval/                      # retrieval baseline (corpus + samples + threshold)
├── docs/
│   ├── API.md                         # full API reference (Chinese)
│   └── assets/workflow.svg            # the animated diagram above
├── Dockerfile  docker-entrypoint.sh
├── requirements.txt  start_backend.bat  start_backend.ps1
└── wf_e2e.py  frontend_e2e_check.py    # end-to-end verification scripts
```

</details>

---

## Tests

```bash
# Python unit + regression (no server, no model)
.venv/Scripts/python -m pytest tests/ -q --ignore=tests/rag_eval    # 274 passed

# Shared frontend layer / SSE protocol (node:test, no server)
node --test tests/test_shared_frontend.cjs tests/test_workflow_sse.cjs \
            tests/test_client_modules.cjs tests/test_error_routing.cjs \
            tests/test_score_disposition_badge.cjs

# Retrieval baseline (no server, no model)
.venv/Scripts/python -m tests.rag_eval.run --check    # exits 1 if below baseline.json
```

Checks that need a real model:

```bash
.venv/Scripts/python frontend_e2e_check.py   # front-end connectivity (72 checks)
.venv/Scripts/python wf_e2e.py               # real 15-node run (47 checks, 4–5 min)
.venv/Scripts/python verify_coverage_live.py # 42k-character coverage verification
```

Current retrieval baseline: Recall@3 = **0.448**, MRR = **0.391**, precision = **0.161**
(7 documents / 271 chunks / 9,925 characters, 29 samples). Run it before and after touching
retrieval — argue with numbers, not adjectives.

> Behind an HTTP proxy, scripts that call the local service need
> `NO_PROXY=127.0.0.1,localhost`.

---

## Deployment notes

- **Single worker.** The progress bus and the checkpointer are in-process; `uvicorn --workers N`
  splits the state. Replace `progress_bus` and `MemorySaver` before going above one.
- **Reverse proxy.** SSE needs `proxy_buffering off;` and a long `proxy_read_timeout`; the
  server already sends `X-Accel-Buffering: no`.
- **Data retention.** Uploads are kept 90 days and cleaned at 03:00 daily; the admin page can
  preview and trigger the cleanup.
- **Backups.** Besides MySQL, `state_backups/*.json` is the only source for restoring
  historical reports — back it up with the database.

---

## Roadmap

- [ ] Chunked map-reduce for genuinely complete coverage of very long documents
- [ ] Redis pub/sub + pluggable checkpointer: multiple workers and real resume
- [ ] `critique-revise` dual-model review; turn side-by-side comparison into selection
- [ ] A hard gate on `issue_id → suggestion_ids` coverage
- [ ] `/metrics` and structured logging
- [ ] CI (lint + pytest + node:test) and dependency locking

---

## Contributing

Issues and PRs are welcome. Read [CONTRIBUTING.md](./CONTRIBUTING.md) first; participating means
agreeing to the [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).

Please do not open a public issue for a security problem — report it as described in
[SECURITY.md](./SECURITY.md).

## License

[MIT](./LICENSE).

---

<div align="center">
<sub>If this project is useful to you, a star is appreciated.</sub>
</div>
