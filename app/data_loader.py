"""
Loads support_tickets.csv into a pandas DataFrame (for the anomaly checks)
and an in-memory SQLite table (for the NL-to-SQL query engine).

500 rows fits fine in memory so there's no real DB server here. If that
ever needs to change, get_connection() is the only place to touch.
"""

import os
import sqlite3
from functools import lru_cache

import pandas as pd

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "support_tickets.csv")
TABLE_NAME = "tickets"

# Same column list the LLM prompt in nl_query.py gets, so keep this in sync
# with the CSV if columns ever change.
SCHEMA_DESCRIPTION = """
Table: tickets
Columns:
  ticket_id            TEXT    -- unique ticket identifier, e.g. 'TKT-001'
  created_at           TEXT    -- ISO-ish timestamp 'YYYY-MM-DD HH:MM', when the ticket was created
  category             TEXT    -- one of: 'General', 'Billing', 'Technical'
  priority             TEXT    -- one of: 'Low', 'Medium', 'High', 'Critical'
  status               TEXT    -- one of: 'Resolved', 'Open', 'Escalated'
  response_time_hrs    REAL    -- hours until the first agent response
  resolution_time_hrs  REAL    -- hours until the ticket was resolved (NULL if not resolved)
  agent_id             TEXT    -- e.g. 'AGT-03'
  customer_rating      REAL    -- 1-5 star rating (NULL if not yet rated / not resolved)
  issue_summary        TEXT    -- free-text one-line summary of the issue
""".strip()


@lru_cache(maxsize=1)
def load_dataframe() -> pd.DataFrame:
    """Read + clean the CSV once and cache it, the file doesn't change at runtime."""
    df = pd.read_csv(CSV_PATH)
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    for col in ("response_time_hrs", "resolution_time_hrs", "customer_rating"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_connection() -> sqlite3.Connection:
    """Fresh in-memory sqlite connection with the tickets table loaded.

    New connection per call instead of one shared one - sqlite3 connections
    aren't thread-safe by default and FastAPI can handle requests on different
    threads.
    """
    conn = sqlite3.connect(":memory:")
    df = load_dataframe()
    # SQLite's date functions expect text, not pandas datetime objects.
    df_for_sql = df.copy()
    df_for_sql["created_at"] = df_for_sql["created_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df_for_sql.to_sql(TABLE_NAME, conn, index=False, if_exists="replace")
    return conn


def dataset_reference_now() -> pd.Timestamp:
    """What counts as "now" for age-based checks like the 24h stale-ticket rule.

    Dataset only covers Jan-Mar 2024, so using the real wall clock would make
    everything look ancient. We use the latest created_at in the data instead.
    """
    return load_dataframe()["created_at"].max()
