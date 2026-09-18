# Support Ticket AI System

A small AI system built on top of a customer support ticket dataset (500 rows, `support_tickets.csv`).
It does four things:

1. Loads the CSV and makes it queryable.
2. Answers natural-language questions about the tickets using an LLM (Groq, free tier).
3. Flags anomalies - long resolution times, stale high-priority tickets that are still open.
4. Exposes all of that through a REST API and a simple browser UI, both from the same process.

One command to start it: `uvicorn app.main:app --reload` (or Docker, see below).

---

## Architecture

```
support_tickets.csv
       │
       ├──────────────► pandas DataFrame ──────► anomaly.py (rule-based, no LLM)
       │
       └──────────────► SQLite (in-memory)
                              │
                    NL question
                         │
                         ▼
              LLM #1 (Groq): question → SQL
                         │
                         ▼
               sqlite3.execute(SQL)
                         │
                         ▼
              LLM #2 (Groq): rows → answer
                         │
                         ▼
                    FastAPI REST endpoints
                         │
                         ▼
               Minimal HTML/JS UI (served by FastAPI at /ui)
```

Why go through SQL instead of just dumping the data into a RAG/vector setup? The dataset is small
and structured (500 rows, 10 columns), and most of the questions people actually ask ("which agent
has the lowest rating") are `GROUP BY` + `AVG` + `ORDER BY`, not similarity search. Running it through
SQL means counting/averaging questions get the real answer over all 500 rows every time, instead of
whatever a retrieval step happened to pull back. So: one LLM call turns the question into SQL, SQLite
runs it, a second LLM call turns the (small, exact) result into a sentence. Anomaly detection skips
the LLM entirely and just uses fixed statistical thresholds, because those flags need to come out the
same way every time you run them.

### Main components

| File | Role |
|---|---|
| `app/data_loader.py` | Loads the CSV into a DataFrame and an in-memory SQLite table |
| `app/nl_query.py` | NL → SQL → answer pipeline (two Groq calls: generate SQL, then explain results) |
| `app/anomaly.py` | Rule-based anomaly detection, no LLM involved |
| `app/main.py` | FastAPI app - REST endpoints + serves the UI |
| `templates/index.html`, `static/` | Plain HTML/CSS/JS UI, no build step |

## Model / tools used

- **LLM**: Groq free tier, `openai/gpt-oss-20b` via `langchain_groq.ChatGroq`. You can swap it for
  any other Groq-hosted model with the `GROQ_MODEL_NAME` env var (see
  https://console.groq.com/settings/limits for what's available). One gotcha worth knowing about:
  this is a reasoning model, so it spends part of its token budget thinking before it writes the
  visible answer. On bigger result sets that can eat the whole `max_tokens` budget and leave you
  with an empty response (`finish_reason: "length"`). Fixed here by passing `reasoning_effort="low"`
  at `.invoke()` time rather than the `ChatGroq` constructor - the constructor's field in this
  `langchain_groq` version only accepts `"none"`/`"default"`, and Groq's live API rejects that for
  this model, so it has to go in through invoke() instead.
- **Storage**: SQLite, in-memory, rebuilt from the CSV each time the process starts. No external DB.
- **API**: FastAPI + Uvicorn.
- **UI**: plain HTML/CSS/JS served straight from FastAPI at `/ui`, so the whole thing - API and UI -
  comes up with one `uvicorn` command.

## Setup

### Prerequisites

- Python 3.11+ (skip this if you're using Docker)
- A free [Groq](https://console.groq.com/keys) account for the API key

### 1. Clone the repo

```bash
git clone <this-repo-url>
cd support-ticket-ai-system
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Get a free Groq API key

Sign up at https://console.groq.com/keys (free tier, no card needed) and grab your key.

### 4. Configure environment

`.env` holds the API key and is git-ignored, so it won't come along when someone clones the repo.
Copy the template that is checked in and fill in your own key:

```bash
cp sample.env .env
# then edit .env and paste your key:
# GROQ_API_KEY=gsk_...
```

### 5. Run it

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000/ui for the UI, or http://localhost:8000/docs for the API docs.

### Docker alternative

Needs Docker Desktop (or Docker Engine + the Compose plugin) running. You still need steps 3-4 above
first (Groq key + `.env` file) - Docker just replaces the Python install and the `uvicorn` command.

**Docker Compose (easier):**

```bash
docker compose up --build
```

**Plain Docker:**

```bash
docker build -t ticket-ai .
docker run --env-file .env -p 8000:8000 ticket-ai
```

Either way, http://localhost:8000/ui once it's up. `Ctrl+C` to stop, or `docker compose down` to
also remove the container.

## REST API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check, confirms the dataset loaded |
| POST | `/api/query` | `{"question": "..."}` → NL answer, plus the SQL that was run and the raw rows |
| GET | `/api/anomalies?window=week\|month` | Anomaly report (leave off `window` for all-time) |
| GET | `/api/tickets?status=&priority=&category=&agent_id=&limit=` | Raw filtered ticket rows |

## Example queries and outputs

These are real outputs from the SQL layer against `support_tickets.csv` (the LLM wraps the same rows
into a sentence at query time, so wording will vary a bit run to run since that step isn't pinned to
`temperature=0`):

**"How many tickets are currently open?"**
→ SQL: `SELECT COUNT(*) FROM tickets WHERE status IN ('Open', 'Escalated')` → 173 open tickets.

**"Which agent has the lowest average customer rating?"**
→ a `GROUP BY agent_id` + `AVG(customer_rating)` + `ORDER BY` - the exact per-agent average across
every rated ticket, not a sample.

**"Show all Critical tickets that are not resolved."**
→ 31 Critical tickets currently Open or Escalated, matched via
`WHERE priority = 'Critical' AND status IN ('Open', 'Escalated')`.

**"Hi"**
→ the SQL-generation prompt recognizes greetings/small talk and returns a fixed sentinel query
instead of trying to force it into a data question, so this gets answered directly with a friendly
prompt to ask about the ticket data - no wasted second LLM call.

**"Are there any anomalies in resolution times this week?"**
→ handled by `GET /api/anomalies?window=week`, not the NL-query path - see below.

## Anomaly detection logic

Two independent, deterministic rules, both in `app/anomaly.py`:

1. **Long resolution times** - for each `category`, flags resolved tickets whose
   `resolution_time_hrs` is past `mean + 2×std` for that category. Per-category because a "normal"
   resolution time for Billing isn't the same as for Technical, so one global threshold would
   over-flag one category and miss the other.
2. **Stale unresolved high-priority tickets** - `status` in `(Open, Escalated)`, `priority` in
   `(High, Critical)`, open for more than 24 hours.

**About "this week"/"this month":** the dataset is historical (Jan-Mar 2024), so there's no real
wall-clock "now" to measure against. Both the anomaly rules and the SQL prompt treat `MAX(created_at)`
in the data as "now", and "this week"/"this month" as the 7/30 days before that. That's intentional,
not a bug - a live deployment would just swap it for `datetime.now()`.

## Known limitations

- **Free-tier model accuracy**: `openai/gpt-oss-20b` is fast and free, but smaller than the frontier
  models, so it can occasionally misread ambiguous phrasing (e.g. "not resolved within 12 hours"
  could mean "still open" or "took over 12h to resolve", and the prompt doesn't disambiguate).
  Rephrasing the question usually fixes it.
- **The SQL safety net is keyword-based, not a real parser.** It blocks the obvious mutating
  keywords (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`, etc.) and requires the query to start with
  `SELECT`. Fine for a single-table, read-only, single-user SQLite DB, but not something you'd trust
  as a real permissions layer.
- **No conversation memory** - every question is answered on its own, no multi-turn "and what about
  last month?" follow-ups.
- **SQLite is in-memory** and gets rebuilt from the CSV on every process start (a few hundred ms for
  500 rows). Fine at this size, wouldn't scale to a dataset too big to fit in memory.
- **One CSV, fixed schema** - column names are hard-coded into the SQL prompt and the anomaly rules,
  so a different dataset would need both updated.
- **No fuzzy/semantic search** over free-text fields like `issue_summary` - everything goes through
  exact SQL filters, so "find tickets like this one" isn't something it can do. That's a job for a
  RAG/embedding layer, which just isn't needed for the aggregation-style questions this dataset
  mostly gets asked.
