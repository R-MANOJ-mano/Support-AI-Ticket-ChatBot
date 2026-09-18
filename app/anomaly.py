"""
Rule-based anomaly detection over the ticket dataset.

No LLM here on purpose - anomaly flags need to be the same every time you run
them, and rules give you that for free while an LLM call doesn't. Two checks:

1. Long resolution times: resolved tickets where resolution_time_hrs is more
   than mean + 2*std, computed per category (a "normal" resolution time for
   Billing isn't the same as for Technical).
2. Stale unresolved high-priority: Open/Escalated tickets, High or Critical
   priority, open for more than 24h relative to the dataset's reference "now"
   (see data_loader.py).
"""

from datetime import timedelta

import pandas as pd

from .data_loader import dataset_reference_now, load_dataframe

STALE_PRIORITIES = {"High", "Critical"}
STALE_THRESHOLD_HOURS = 24
OUTLIER_STD_MULTIPLIER = 2


def _long_resolution_outliers(df: pd.DataFrame) -> pd.DataFrame:
    resolved = df[df["resolution_time_hrs"].notna()].copy()
    stats = resolved.groupby("category")["resolution_time_hrs"].agg(["mean", "std"]).rename(
        columns={"mean": "cat_mean", "std": "cat_std"}
    )
    resolved = resolved.join(stats, on="category")
    resolved["threshold"] = resolved["cat_mean"] + OUTLIER_STD_MULTIPLIER * resolved["cat_std"]
    flagged = resolved[resolved["resolution_time_hrs"] > resolved["threshold"]].copy()
    # Round here, before building the reason string, so the number in the text
    # matches the rounded value to_records() puts in the table later. Rounding
    # the same float twice in two places (:.1f here vs .round() there) can land
    # on different sides of a .x5 boundary due to float rounding quirks.
    flagged["resolution_time_hrs"] = flagged["resolution_time_hrs"].round(1)
    flagged["cat_mean"] = flagged["cat_mean"].round(1)
    flagged["threshold"] = flagged["threshold"].round(1)
    flagged["anomaly_type"] = "long_resolution_time"
    flagged["reason"] = flagged.apply(
        lambda r: (
            f"resolution took {r['resolution_time_hrs']:.1f}h, vs a "
            f"{r['category']} average of {r['cat_mean']:.1f}h (threshold {r['threshold']:.1f}h)"
        ),
        axis=1,
    )
    return flagged[
        [
            "ticket_id",
            "category",
            "priority",
            "status",
            "resolution_time_hrs",
            "agent_id",
            "issue_summary",
            "anomaly_type",
            "reason",
        ]
    ]


def _stale_unresolved(df: pd.DataFrame, reference_now: pd.Timestamp, since: pd.Timestamp | None = None) -> pd.DataFrame:
    unresolved = df[df["status"].isin(["Open", "Escalated"])].copy()
    unresolved = unresolved[unresolved["priority"].isin(STALE_PRIORITIES)]
    unresolved["age_hrs"] = (reference_now - unresolved["created_at"]).dt.total_seconds() / 3600
    flagged = unresolved[unresolved["age_hrs"] > STALE_THRESHOLD_HOURS].copy()
    if since is not None:
        flagged = flagged[flagged["created_at"] >= since]
    # same rounding-order reasoning as _long_resolution_outliers above
    flagged["age_hrs"] = flagged["age_hrs"].round(1)
    flagged["anomaly_type"] = "stale_unresolved_high_priority"
    flagged["reason"] = flagged.apply(
        lambda r: f"{r['priority']} priority, {r['status']}, open for {r['age_hrs']:.1f}h (> {STALE_THRESHOLD_HOURS}h threshold)",
        axis=1,
    )
    return flagged[
        ["ticket_id", "category", "priority", "status", "age_hrs", "agent_id", "issue_summary", "anomaly_type", "reason"]
    ]


def detect_anomalies(window: str | None = None) -> dict:
    """window="week"/"month" limits both checks to tickets created in that
    trailing window before the dataset's reference "now". None means all data."""
    df = load_dataframe()
    reference_now = dataset_reference_now()

    since = None
    if window == "week":
        since = reference_now - timedelta(days=7)
    elif window == "month":
        since = reference_now - timedelta(days=30)

    long_res = _long_resolution_outliers(df)
    if since is not None:
        long_res = long_res.merge(df[["ticket_id", "created_at"]], on="ticket_id")
        long_res = long_res[long_res["created_at"] >= since].drop(columns=["created_at"])

    stale = _stale_unresolved(df, reference_now, since=since)

    def to_records(frame: pd.DataFrame) -> list[dict]:
        return frame.round(1).to_dict(orient="records") if not frame.empty else []

    return {
        "reference_now": str(reference_now),
        "window": window or "all",
        "long_resolution_time_count": len(long_res),
        "stale_unresolved_count": len(stale),
        "long_resolution_time": to_records(long_res.sort_values("resolution_time_hrs", ascending=False)),
        "stale_unresolved": to_records(stale.sort_values("age_hrs", ascending=False)),
    }
