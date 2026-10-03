```markdown
# de-job-intelligence: Autonomous Job Market Intelligence & Agentic Synthesis Platform

An enterprise-grade, event-driven data platform and agentic workflow engine designed to monitor technical job markets, evaluate technical alignment through deterministic RAG frameworks, and orchestrate automated document and email synthesis via the **Model Context Protocol (MCP)**, Google Workspace APIs, and automated WhatsApp alert workers.

Built with Python 3.12, Starlette, PostgreSQL (Psycopg 3 with connection pooling), and Google Gemini, this platform replaces brittle prompt engineering with a decoupled, bounded-context architecture featuring strict schema validation, defensive synthesis fallbacks, and micro-batch worker pipelines.

---

## 🏛 System Architecture

The platform decouples transient web extraction, persistence, deterministic retrieval-augmented generation (RAG), and external agent protocols into clean bounded contexts:


```

```
                      ┌──────────────────────────┐
                      │ External Job APIs (Dice) │
                      └─────────────┬────────────┘
                                    │
                                    ▼
                      ┌──────────────────────────┐
                      │   src/ingestion/         │
                      │   (REST Ingestion Client)│
                      └─────────────┬────────────┘
                                    │ (Idempotent Upsert)
                                    ▼

```

┌─────────────────────────────────────────────────────────────────────────────┐
│                       PostgreSQL 16 Storage (saved_jobs)                    │
│                 Managed via psycopg_pool.ConnectionPool                     │
└───────┬───────────────────────────────┬──────────────────────────────┬──────┘
        │                               │                              │
▼ (Queue: PENDING/FAILED)       ▼ (Audit & Queries)            ▼ (Job Fetching)
┌──────────────────────┐    ┌───────────────────────────┐    ┌────────────────────────┐
│  src/workers/        │    │  src/dashboard/           │    │  src/engine/           │
│  - backfill_worker   │    │  - Starlette UI (Port 5001│    │  - matcher.py (Scorer) │
│  - batch_ingestion   │    │  - Tailwind CSS + Jinja2  │    │  - prompt_builder.py   │
│  - Gemini LLM Pacing │    │  - Skill Gap Audit Matrix │    │  - analyzer.py         │
└──────────────────────┘    └───────────────────────────┘    └───────────┬────────────┘
                                                                         │
                                                                         ▼
                                                          ┌────────────────────────────────┐
                                                          │  Model Context Protocol (MCP)  │
                                                          │  src/protocols/ (Port 8000)    │
                                                          │  - JSON-RPC 2.0 Dispatcher     │
                                                          │  - Non-blocking asyncio.to_thr │
                                                          └───────────────┬────────────────┘
                                                                          │
                                          ┌────────────────────────────────┴────────────────┐
                                          │ Tool 1: Context RAG                             │ Tool 2: Document & Draft Synthesis
                                          ▼                                                 ▼
                                          ┌─────────────────────────────┐                  ┌──────────────────────────────┐
                                          │ 5-Phase Knowledge Graph     │                  │  src/synthesis/              │
                                          │ - Dynamic Phase Weighting   │                  │  - resume_mapper.py          │
                                          │ - Core DE Phase Suppression │                  │  - gdrive_docs.py            │
                                          │ - Static Tool Bridging      │                  │  - gmail_client.py           │
                                          └─────────────────────────────┘                  └──────────────┬───────────────┘
                                                                                                          │ (OAuth 2.0 API)
                                                                                                          ▼
                                                                                                          ┌──────────────────────────────┐
                                                                                                          │ Production Drafts & Resumes  │
                                                                                                          │ (Gmail Drafts & G-Docs URL)  │
                                                                                                          └──────────────────────────────┘

```

---

## 💡 Core Engineering Highlights

### 1. Deterministic RAG vs. Generative Hallucination

Standard LLM resume customization frequently invents metrics, hallucinates tool proficiencies, or shifts focus away from core backend responsibilities. This platform enforces **deterministic grounding**:

* **5-Phase Architectural Knowledge Graph (`company_frameworks.json`)**: Every career tenure is mapped across Ingestion & Streaming, Lakehouse Landing, Distributed Transformations (Spark/Databricks), Orchestration & Data Modeling, and Semantic Serving.
* **Dynamic Phase Weighting Policy**: Backend evaluation detects the target archetype. For Core Data Engineering roles, the engine strictly suppresses Phase 5 (BI/dashboards) and forces 85%+ of generated content into distributed compute, partition pruning, memory tuning, and CDC patterns.
* **Strict Tool Bridging**: Maps unmentioned candidate tools to their exact architectural equivalents (e.g., Kafka maps strictly to Kinesis/Databricks Streaming; dbt maps strictly to SQL transformation layers) without inventing experience.

### 2. Model Context Protocol (MCP) Integration

Production-grade MCP server built as a layered JSON-RPC 2.0 service (see [MCP Server Architecture](#-mcp-server-architecture)): bearer-token auth middleware, declarative tool registry, spec-correct lifecycle handling. Exposes operational primitives directly to AI agents:

* `prepare_job_tailoring_prompt`: Ingests PostgreSQL job context, executes heuristic scoring, applies dynamic phase suppression, and packages full prompt context.
* `export_tailored_resume`: Consumes structured JSON from the reasoning model, validates profile invariant truths, executes Google Docs API batch updates, and returns a verified document link.
* `get_job_details`: Exposes metadata and raw text for targeted inspection.
* `retry_job_evaluations`: Coordinates rate-paced background evaluations for pending queues.

### 3. Automated Recruiter Intelligence & Rich HTML Gmail Drafts

* **Intelligent Recruiter Extraction (`email_analyzer.py`)**: Sanitizes incoming recruiter names to extract first names only (e.g., converting "Roshini D" or "Mishra.Neha@..." into clean, human salutations like "Hi Roshini,").
* **Section-Aware HTML Client (`gmail_client.py`)**: Renders rich MIME alternative messages with:
  * **Technical Alignment Bullets**: Indented bullet lists with automatically bolded subheadings before colons.
  * **Candidate Summary Block**: Cleanly indented metadata rows (`Total Experience`, `Work Authorization`, `Role Focus`, etc.) with bold keys and zero bullet dots.
  * **Pristine Signature Spacing**: Tightened vertical spacing omitting the generic job title per user preference, with helper addresses safely mapped to the CC field.

### 4. High-Throughput Asynchronous Concurrency

* **Database Connection Pooling**: PostgreSQL connections managed through `psycopg_pool.ConnectionPool` (`min_size=1, max_size=10`), eliminating connection overhead and memory exhaustion.
* **Non-Blocking Thread Delegation**: Heavy external network calls (Google Docs API batch calls, database cursors, file I/O) are systematically offloaded via `asyncio.to_thread`, keeping Starlette’s event loop available for inbound RPC traffic.
* **Rate-Paced Worker Queues**: LLM backfill workers utilize deterministic pacing delays and state-machine transitions (`PENDING` $\rightarrow$ `PROCESSED` $\rightarrow$ `FAILED`) to stay within Google Gemini API quota limits without stalling pipelines.

---

## 🏛 MCP Server Architecture

The MCP server (`src/protocols/`) is a layered, production-grade JSON-RPC 2.0 service following the patterns used by production remote MCP deployments (bearer-token middleware in front of the ASGI app, declarative tool registry, spec-correct protocol lifecycle):

```
Client (AI agent / IDE / automation)
        │  Authorization: Bearer <MCP_AUTH_TOKEN>
        ▼
┌──────────────────────────┐
│ BearerAuthMiddleware     │  RFC 6750 bearer tokens, constant-time compare
│ (src/protocols/auth.py)  │  Fail-closed: no token → server refuses to start
└────────────┬─────────────┘  GET /health exempt (load-balancer probes)
             ▼
┌──────────────────────────┐
│ Starlette ASGI app       │  JSON-RPC 2.0 transport (protocolVersion 2024-11-05)
│ (src/protocols/app.py)   │  initialize · notifications/initialized ·
└────────────┬─────────────┘  tools/list · tools/call
             ▼
┌──────────────────────────┐
│ Tool registry            │  TOOL_HANDLERS: tool name → async handler
│ (dispatcher.py)          │  No if/elif chains; JSON Schema manifests in
└────────────┬─────────────┘  schemas.py describe every tool's inputs
             ▼
┌───────────────────────────────────────────────────┐
│ Business logic: engine/ · workers/ · synthesis/    │
│ PostgreSQL (pooled) · Gemini · Google Workspace   │
└───────────────────────────────────────────────────┘
```

### Protocol compliance

| Concern | Implementation |
| --- | --- |
| Handshake | `initialize` negotiates `protocolVersion` and advertises capabilities |
| Lifecycle | `notifications/initialized` answered with `202 Accepted` (spec-correct: notifications carry no response body) |
| Discovery | `tools/list` serves versioned JSON Schema manifests for every tool |
| Execution | `tools/call` returns MCP content blocks; results are JSON-serialized (never Python `str()`) so agents receive parseable payloads |
| Errors | Standard JSON-RPC codes: `-32700` parse, `-32601` method not found, `-32602` invalid params, `-32603` internal |

### Security model

* **Authentication**: RFC 6750 bearer tokens via `MCP_AUTH_TOKEN`, compared in constant time (`hmac.compare_digest`) to resist timing attacks.
* **Fail-closed startup**: the process exits instead of serving an open endpoint when no token is configured.
* **Health checks**: `GET /health` is intentionally unauthenticated so load balancers and uptime monitors keep working.
* **Secret hygiene**: tokens live in the environment / `.env`, never in git; rotate with `openssl rand -hex 32`.
* **No import-time clients**: heavyweight SDK clients (Gemini, Google Workspace) initialize lazily on first use, so a missing key degrades one tool instead of crashing the server at import.

### Concurrency

* **Async-first**: blocking I/O (DB cursors, Google API calls, file reads) is offloaded via `asyncio.to_thread`, keeping the event loop free for inbound RPC traffic.
* **Pooled persistence**: PostgreSQL connections via `psycopg_pool` (`min_size=1, max_size=10`).
* **Atomic queue claims**: the apply worker claims jobs with `SELECT … FOR UPDATE SKIP LOCKED`, so the MCP-triggered worker and the daemon worker can never double-process a job.

---

## 🛠 Tech Stack

| Domain | Technology | Purpose |
| --- | --- | --- |
| **Runtime & Language** | Python 3.12 | Core platform language |
| **API & Protocol Server** | Starlette, Uvicorn | High-performance ASGI runtime, JSON-RPC 2.0 MCP server |
| **Database & Pooling** | PostgreSQL 16, Psycopg 3 (`psycopg_pool`) | Relational persistence, JSONB metadata, thread-safe pooling |
| **LLM & Heuristics** | Google Gemini API (`gemini-2.0-flash`), Regex | Heuristic regex matching and generative evaluation |
| **External Synthesis** | Google Workspace APIs (Docs, Drive v3, Gmail) | OAuth 2.0 authenticated document cloning and rich HTML draft composition |
| **UI & Observability** | Jinja2, Tailwind CSS | Live pipeline dashboard and AI skill gap audit interface |
| **Testing & CI/CD** | Pytest, AnyIO, Ruff, GitHub Actions | Integration/unit test suites and automated pull-request validation |
| **Containerization** | Docker, Docker Compose | Multi-stage container builds and microservice orchestration |

---

## 📂 Repository Structure


```

de-job-intelligence/
├── .github/
│   └── workflows/
│       └── ci.yml               # Automated Pytest and Ruff linting pipeline
├── config/
│   ├── company_frameworks.json  # 5-Phase data engineering lifecycle knowledge graphs
│   ├── resumes/
│   │   └── resume_de.md         # Candidate master profile context
│   └── roles/
│       └── data_engineering.json# Skill schemas, phase weighting policies, heuristics
├── logs/                        # Server output logs and daemon PID files
├── src/
│   ├── core/
│   │   ├── config.py            # Pydantic Settings with .env loading
│   │   ├── database.py          # Psycopg 3 connection pool and atomic query helpers
│   │   └── utils.py             # File system and profile cache loaders
│   ├── dashboard/
│   │   ├── templates/
│   │   │   ├── dashboard.html   # Real-time job ingestion ledger and filter UI
│   │   │   └── audit.html       # Skill matrix & gap audit interface
│   │   └── app.py               # Starlette dashboard backend & /api/jobs REST router
│   ├── engine/
│   │   ├── analyzer.py          # Google Gemini structured evaluation client
│   │   ├── alert_pipeline.py    # WhatsApp alert metadata and synthesis pipeline
│   │   ├── email_analyzer.py    # Recruiter first-name and contact extractor
│   │   ├── matcher.py           # Heuristic regex skill scoring engine
│   │   └── prompt_builder.py    # Deterministic RAG builder with phase weighting
│   ├── ingestion/
│   │   ├── dice_client.py       # REST API client for job search normalization
│   │   └── scraper.py           # HTML sanitization & DOM text extraction
│   ├── protocols/
│   │   ├── app.py               # MCP JSON-RPC 2.0 server: app factory (tools/list, tools/call)
│   │   ├── auth.py              # BearerAuthMiddleware: RFC 6750 token gate, fail-closed
│   │   ├── dispatcher.py        # TOOL_HANDLERS registry: tool name → async handler
│   │   └── schemas.py           # JSON Schema manifests for all MCP tools
│   ├── synthesis/
│   │   ├── gdrive_docs.py       # Google Drive copy & Docs batchUpdate engine
│   │   ├── gmail_client.py      # Rich HTML MIME draft builder with smart styling
│   │   └── resume_mapper.py     # Defensive fallback injector & token replacer
│   └── workers/
│       ├── backfill_worker.py   # Paced queue worker for LLM scoring backfills
│       ├── batch_ingestion.py   # Job ingestion & heuristic upsert pipeline
│       └── pipeline_utils.py    # Database state-machine update operations
├── tests/
│   ├── conftest.py            # Test-session bootstrap (MCP_AUTH_TOKEN for fail-closed app)
│   ├── test_ingestion.py        # Mock tests for REST client and persistence
│   ├── test_mcp_auth.py         # Bearer-token gate: 401s, /health bypass, fail-closed
│   ├── test_prompt_builder.py   # RAG framework and token caching validation
│   ├── test_protocol.py         # Starlette JSON-RPC handshake & tool dispatch tests
│   ├── test_synthesis.py        # Fallback resilience & token sanitization tests
│   └── test_workers.py          # Worker queue and database mock verification
├── Dockerfile                   # Multi-stage lean Python 3.12 container definition
├── docker-compose.yml           # Multi-service orchestration (Postgres, MCP, Dashboard)
├── pyproject.toml               # Poetry/pip standard build dependencies & pytest config
├── start_server.sh              # macOS/Linux daemon lifecycle startup script
└── stop_server.sh               # Clean PID termination and fallback port release script

```

---

## 📋 MCP Protocol Specification

The MCP Server implements JSON-RPC 2.0 at `http://localhost:8000/rpc` (protocolVersion `2024-11-05`). All RPC routes require `Authorization: Bearer <MCP_AUTH_TOKEN>`; see [MCP Server Architecture](#-mcp-server-architecture). Below are the primary tool contracts exposed to reasoning agents:

### 1. `prepare_job_tailoring_prompt`

Fetches a target job record from PostgreSQL, calculates baseline skill alignment, checks for BI/reporting keywords to trigger Phase 5 suppression, and packages company frameworks.

* **Arguments:** `{"job_id": "string"}`
* **Response:**
```json
{
  "job_metadata": { "job_id": "dice_123", "title": "Senior Data Engineer", "company": "Acme" },
  "job_description": "...",
  "base_resume": "...",
  "tailoring_guidelines": {
    "dynamic_phase_weighting_policy": "SUPPRESS Phase 5 (BI) unless explicitly demanded...",
    "company_architectural_frameworks": { ... }
  }
}

```

### 2. `export_tailored_resume`

Ingests the LLM’s tailored JSON payload, validates each role against master profile facts, fills any dropped bullets using the defensive fallback engine, clones the Google Doc template, and returns an edit link.

* **Arguments:**

```json
{
  "document_title": "Sri Omkar Dumpa - Lead Data Engineer",
  "llm_payload": {
    "professional_summary": "Data Engineer with 8+ years experience...",
    "technical_skills": { "cloud": "AWS, Azure", "bigdata": "Spark, Databricks" },
    "experience": {
      "herc": [
        "Optimized Spark SQL jobs, reducing runtime by 30% through partition pruning."
      ]
    }
  }
}

```

* **Response:**

```json
{
  "status": "success",
  "document_url": "[https://docs.google.com/document/d/1bIC23.../edit](https://docs.google.com/document/d/1bIC23.../edit)",
  "message": "Resume successfully generated and formatted in Google Docs."
}

```

---

## ⚡ Quickstart Guide

### 1. Clone & Environment Configuration

```bash
git clone [https://github.com/your-username/de-job-intelligence.git](https://github.com/your-username/de-job-intelligence.git)
cd de-job-intelligence

python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

```

### 2. Environment Variables Setup

Create `.env` in the project root:

```env
# Database Settings
DB_HOST=localhost
DB_PORT=5432
DB_NAME=job_scout_db
DB_USER=postgres
DB_PASSWORD=your_password

# Google Gemini API
GEMINI_API_KEY=your_gemini_api_key

# Google Workspace Integration
GOOGLE_DOCS_TEMPLATE_ID=your_google_doc_template_id

# MCP server authentication (required -- the server refuses to start without it)
# Generate with: openssl rand -hex 32
MCP_AUTH_TOKEN=your_strong_random_token

# Server Ports
MCP_SERVER_PORT=8000
DASHBOARD_PORT=5001

```

Place your authorized `credentials.json` and generated `token.json` in the project root to enable Google Drive, Google Docs, and Gmail API draft creation.

### 3. Verify Test Suite

Execute the complete test suite verifying ingestion, prompting, synthesis fallbacks, and protocol routing:

```bash
python -m pytest tests/ -v

```

---

## 🚀 Execution & Operations

### Local Daemon Management

The platform provides production process management via shell scripts that handle PID tracking and port allocation:

```bash
# Start MCP Protocol Server (Port 8000) & Observability Dashboard (Port 5001)
./start_server.sh

# Check Service Health (no auth required)
curl http://localhost:8000/health
# {"status":"healthy","service":"de-job-intelligence-mcp","version":"1.0.0"}

# Call the RPC endpoint (bearer token required)
curl -X POST http://localhost:8000/rpc \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Stop all background services cleanly
./stop_server.sh

```

---

## 📊 Live Observability & Skill Audit

The dashboard (`src/dashboard/`) provides real-time visibility into your job search pipeline:

1. **Active Ledger View (`/`)**: Displays categorized jobs, heuristic fit scores, date posted, employment type, visa sponsorship status, and direct application links.
2. **Dynamic Client Filtering**: Filter real-time records by post date, database ingestion timestamp, and source.
3. **AI Skill Gap Audit (`/audit?category=data_engineering`)**: Evaluates market demand across all ingested listings against candidate resume skills, categorizing technologies into matched, missing, and AI-extracted skills.

---

## 🔒 Security & Data Governance

* **Authenticated MCP surface**: RFC 6750 bearer-token middleware guards every JSON-RPC route; the server fails closed (refuses to start) when `MCP_AUTH_TOKEN` is unset. `GET /health` stays open for load-balancer probes.
* **Zero Secret Leakage**: Strict `.gitignore` boundaries protect `.env`, `credentials.json`, `token.json`, and process logs from version control tracking.
* **Deterministic Profile Invariants**: The synthesis mapper protects candidate name, email, phone number, education, and company tenures against LLM rewriting or hallucination.
* **Idempotent Storage Patterns**: Ingestion pipelines use PostgreSQL `ON CONFLICT (job_id) DO NOTHING` constraints to prevent duplicate writes and race conditions during high-volume ingestion sweeps.

```

```