"""
FastAPI entrypoint. Run with: uvicorn app.main:app --reload

Routes:
  GET  /                -> redirects to /ui
  GET  /ui              -> chat UI + anomaly dashboard
  GET  /api/health      -> health check
  POST /api/query       -> {"question": "..."} -> NL to SQL to answer
  GET  /api/anomalies   -> ?window=week|month, anomaly report
  GET  /api/tickets     -> ?status=&priority=&category=&agent_id=&limit=, raw rows
"""

from contextlib import asynccontextmanager
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from . import anomaly, nl_query
from .data_loader import load_dataframe

# uvicorn logs "http://0.0.0.0:8000" on startup, which isn't actually clickable
# in a browser. Print the real localhost URL ourselves instead.
_HOST_PORT = 8000


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 60, flush=True)
    print("  Support Ticket AI System is ready!", flush=True)
    print(f"  UI:   http://localhost:{_HOST_PORT}/ui", flush=True)
    print(f"  Docs: http://localhost:{_HOST_PORT}/docs", flush=True)
    print("=" * 60 + "\n", flush=True)
    yield


app = FastAPI(
    title="Support Ticket AI System",
    description="NL querying + anomaly detection over a customer support ticket dataset.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import os

_BASE_DIR = os.path.dirname(__file__)
_STATIC_DIR = os.path.join(_BASE_DIR, "..", "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "..", "templates"))


def _static_version() -> str:
    """Cache-busting ?v= value for style.css/app.js, just the newer file's mtime.
    Without this, browsers happily keep serving a stale cached copy after a deploy."""
    paths = [os.path.join(_STATIC_DIR, "style.css"), os.path.join(_STATIC_DIR, "app.js")]
    try:
        return str(int(max(os.path.getmtime(p) for p in paths)))
    except OSError:
        return "0"


class QueryRequest(BaseModel):
    question: str


@app.get("/")
def root():
    return RedirectResponse(url="/ui")


@app.get("/ui")
def ui(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "static_version": _static_version()}
    )


@app.get("/api/health")
def health():
    try:
        df = load_dataframe()
        return {"status": "ok", "rows_loaded": int(len(df))}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Data failed to load: {exc}")


@app.post("/api/query")
def query(req: QueryRequest):
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="`question` must not be empty.")
    return nl_query.answer_question(req.question.strip())


@app.get("/api/anomalies")
def anomalies(window: Optional[str] = Query(default=None, pattern="^(week|month)$")):
    return anomaly.detect_anomalies(window=window)


@app.get("/api/tickets")
def tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = Query(default=50, le=500),
):
    df = load_dataframe()
    if status:
        df = df[df["status"].str.lower() == status.lower()]
    if priority:
        df = df[df["priority"].str.lower() == priority.lower()]
    if category:
        df = df[df["category"].str.lower() == category.lower()]
    if agent_id:
        df = df[df["agent_id"].str.lower() == agent_id.lower()]
    out = df.head(limit).copy()
    out["created_at"] = out["created_at"].astype(str)
    records = out.to_dict(orient="records")
    # NaN isn't valid JSON, so swap it for None after converting to records
    for record in records:
        for key, value in record.items():
            if isinstance(value, float) and pd.isna(value):
                record[key] = None
    return {"count": int(len(out)), "total_matching": int(len(df)), "rows": records}
