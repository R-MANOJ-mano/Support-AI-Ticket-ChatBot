"""
Question -> SQL -> answer, over the tickets table.

question --(LLM: NL to SQL)--> SQL query --(sqlite3)--> rows --(LLM: rows to
answer)--> reply.

Two LLM calls per question, both through Groq's free tier. Going through SQL
instead of just asking the LLM to eyeball the data means counting/averaging
questions get the actual number, not a guess.
"""

import json
import os
import re
import sqlite3

from langchain_groq import ChatGroq

from .data_loader import SCHEMA_DESCRIPTION, TABLE_NAME, get_connection

GROQ_MODEL_NAME = os.environ.get("GROQ_MODEL_NAME", "openai/gpt-oss-20b")

# The LLM's SQL is untrusted - only SELECT gets through.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|PRAGMA|REPLACE)\b",
    re.IGNORECASE,
)

SQL_SYSTEM_PROMPT = f"""You are a SQL generator for a single SQLite table.

{SCHEMA_DESCRIPTION}

Rules:
- Output ONLY a single SQLite SELECT statement. No markdown fences, no explanation, no semicolon-separated multiple statements.
- Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, or PRAGMA.
- Use the table name `{TABLE_NAME}`.
- "unresolved" means status IN ('Open', 'Escalated').
- "this week" / "this month" have no fixed wall-clock meaning in this historical dataset; interpret them relative to the MAX(created_at) value in the table (e.g. "this week" = created_at >= (SELECT datetime(MAX(created_at), '-7 days') FROM {TABLE_NAME})).
- If the message is a greeting or small talk (e.g. "hi", "hello", "hey", "good morning", "thanks") rather than a question about ticket data, output exactly: SELECT 'GREETING' AS message;
- If the question cannot be answered from this schema, output exactly: SELECT 'UNSUPPORTED' AS error;
"""

GREETING_REPLY = (
    "Hi! How can I help you? Ask me about ticket counts, priorities, statuses, agents, "
    "ratings, or anomalies -- e.g. \"How many tickets are open?\" or \"Which agent has the "
    "lowest average rating?\""
)

ANSWER_SYSTEM_PROMPT = """You are a support-operations analyst. You are given the user's question, \
the SQL query that was run, and the resulting rows (as JSON). Write a short, direct natural-language \
answer to the question using only this data. Cite concrete numbers from the rows. If the rows are \
empty, say so plainly. Do not mention SQL or the word "query" in your answer -- just answer as if you \
looked it up yourself. Keep it to 2-4 sentences unless the question asks for a list.
"""


def _get_llm(temperature: float = 0.0, max_tokens: int = 1024) -> ChatGroq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
            "and put it in your .env file."
        )
    return ChatGroq(model=GROQ_MODEL_NAME, temperature=temperature, max_tokens=max_tokens, api_key=api_key)


def _extract_sql(raw: str) -> str:
    """Strip markdown fences etc, the model adds them sometimes even when told not to."""
    text = raw.strip()
    text = re.sub(r"^```(?:sql)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    # in case it tacked on an explanation after the query
    if ";" in text:
        text = text.split(";")[0].strip() + ";"
    return text


def question_to_sql(question: str) -> str:
    llm = _get_llm(temperature=0.0)
    # reasoning_effort has to go through invoke(), not the ChatGroq constructor.
    # The constructor's field only accepts "none"/"default" in this langchain_groq
    # version, and Groq's API rejects that for this model - passing it at invoke
    # time skips the validation and just works. "low" keeps latency reasonable.
    response = llm.invoke(
        [("system", SQL_SYSTEM_PROMPT), ("human", question)], reasoning_effort="low"
    )
    sql = _extract_sql(response.content)
    if not sql.lstrip().upper().startswith("SELECT"):
        raise ValueError(f"Model did not return a SELECT statement: {sql!r}")
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError(f"Generated SQL contains a forbidden keyword: {sql!r}")
    return sql


def run_sql(sql: str, limit: int = 200) -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(limit)
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rows_to_answer(question: str, sql: str, rows: list[dict]) -> str:
    # This model burns part of max_tokens on hidden reasoning before it writes
    # the actual reply, so a small budget plus a big result set = empty answer
    # (finish_reason "length"). Give it enough headroom, and cap reasoning_effort
    # so it doesn't run away with the whole budget.
    llm = _get_llm(temperature=0.3, max_tokens=1024)
    payload = json.dumps(rows, default=str)[:6000]  # don't blow up the prompt on a big result set
    human_msg = f"Question: {question}\nSQL used: {sql}\nResult rows (JSON): {payload}"
    response = llm.invoke(
        [("system", ANSWER_SYSTEM_PROMPT), ("human", human_msg)], reasoning_effort="low"
    )
    return response.content.strip()


def answer_question(question: str) -> dict:
    """Runs the full pipeline and returns the answer along with the SQL/rows behind it."""
    try:
        sql = question_to_sql(question)
    except Exception as exc:
        return {
            "question": question,
            "sql": None,
            "rows": [],
            "answer": f"I couldn't turn that question into a safe query ({exc}). "
            "Try rephrasing it, e.g. referencing category, priority, status, or agent_id.",
        }

    try:
        rows = run_sql(sql)
    except Exception as exc:
        return {
            "question": question,
            "sql": sql,
            "rows": [],
            "answer": f"The generated query failed to run ({exc}). Try rephrasing the question.",
        }

    if rows == [{"message": "GREETING"}]:
        return {"question": question, "sql": sql, "rows": [], "answer": GREETING_REPLY}

    if rows == [{"error": "UNSUPPORTED"}]:
        return {
            "question": question,
            "sql": sql,
            "rows": [],
            "answer": "That question isn't answerable from the ticket dataset "
            "(columns: category, priority, status, agent_id, timestamps, response/resolution "
            "times, customer rating, issue summary).",
        }

    answer = rows_to_answer(question, sql, rows)
    return {"question": question, "sql": sql, "rows": rows, "answer": answer}
