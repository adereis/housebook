"""Read models for the Spending and Tax dashboard APIs.

HTTP handlers should translate request parameters and status codes; this
module owns the deterministic database projection shared by the legacy
``/api/data`` route and the focused dashboard endpoints.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3

from dateutil.relativedelta import relativedelta

from housebook.core.models import (
    CATEGORY_CC_PAYMENT,
    CATEGORY_TRANSFERS_REFUNDS,
)
from housebook.core.spending import spend_filter

SPEND_FILTER_T = spend_filter("t")


class DashboardQueryError(ValueError):
    """Raised when dashboard request bounds are incomplete or reversed."""


def load_spending_dashboard(
    db_path: str,
    rules_json: str,
    *,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    trip_id: int | None = None,
    include_tax_docs: bool = False,
    today: datetime.date | None = None,
) -> dict:
    """Build the canonical dashboard projection for one request."""
    _validate_range(start_date, end_date)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        transactions = _load_transactions(
            conn, start_date=start_date,
            end_date=end_date, trip_id=trip_id,
        )
        transactions.extend(
            _load_manual_occurrences(
                conn,
                start_date=start_date,
                end_date=end_date,
                trip_id=trip_id,
                today=today or datetime.date.today(),
            )
        )

        result = {
            "transactions": transactions,
            "categories": _load_categories(conn, rules_json),
            "trips": _load_trips(conn),
            "trip_summaries": _load_trip_summaries(conn, work=False),
            "work_trip_summaries": _load_trip_summaries(conn, work=True),
        }
        if include_tax_docs:
            result["tax_docs"] = _load_tax_documents(conn)
        return result
    finally:
        conn.close()


def load_trip_detail(
    db_path: str,
    rules_json: str,
    trip_id: int,
    *,
    today: datetime.date | None = None,
) -> dict | None:
    """Build the trip page's projection, or None for an unknown trip.

    The page's total, per-day cost and charts are computed from these
    rows, so they must be the same rows the trip summary sums: spending
    transactions on the trip plus its linked manual expenses.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        trip = conn.execute(
            "SELECT id, name, start_date, end_date, "
            "status, type, location "
            "FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()
        if trip is None:
            return None
        transactions = [
            dict(row) for row in conn.execute(
                f"""
                SELECT t.id, t.date, t.category, t.description,
                       t.amount, t.source, t.needs_review
                FROM transactions t
                WHERE t.trip_id = ?
                  AND {SPEND_FILTER_T}
                """,
                (trip_id,),
            ).fetchall()
        ]
        transactions.extend(
            _load_manual_occurrences(
                conn, start_date=None, end_date=None, trip_id=trip_id,
                today=today or datetime.date.today(),
            )
        )
        transactions.sort(key=lambda tx: tx["date"])
        return {
            "trip": dict(trip),
            "transactions": transactions,
            "categories": _load_categories(conn, rules_json),
        }
    finally:
        conn.close()


def load_tax_documents(db_path: str) -> list[dict]:
    """Return the dashboard's tax-document projection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _load_tax_documents(conn)
    finally:
        conn.close()


def _validate_range(
    start_date: datetime.date | None,
    end_date: datetime.date | None,
) -> None:
    if (start_date is None) != (end_date is None):
        raise DashboardQueryError(
            "start_date and end_date must be provided together"
        )
    if start_date and end_date and start_date > end_date:
        raise DashboardQueryError("start_date must not be after end_date")


def _load_transactions(
    conn: sqlite3.Connection,
    *,
    start_date: datetime.date | None,
    end_date: datetime.date | None,
    trip_id: int | None,
) -> list[dict]:
    params: list = []
    filter_clause = ""
    if trip_id is not None:
        filter_clause = " AND t.trip_id = ?"
        params.append(trip_id)
    elif start_date and end_date:
        filter_clause = " AND t.date BETWEEN ? AND ?"
        params.extend((start_date.isoformat(), end_date.isoformat()))

    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.category, t.description,
               t.amount, t.source, tr.name AS trip_name,
               t.trip_id, t.needs_review
        FROM transactions t
        LEFT JOIN trips tr ON t.trip_id = tr.id
        WHERE {SPEND_FILTER_T}
          {filter_clause}
        ORDER BY t.date DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _load_categories(
    conn: sqlite3.Connection,
    rules_json: str,
) -> list[str]:
    if os.path.exists(rules_json):
        with open(rules_json) as f:
            rules_data = json.load(f)
        return sorted(
            category for category in rules_data
            if category not in (
                CATEGORY_TRANSFERS_REFUNDS,
                CATEGORY_CC_PAYMENT,
            )
        )

    rows = conn.execute(
        "SELECT DISTINCT category FROM transactions "
        "WHERE category IS NOT NULL "
        "AND category NOT IN (?, ?) "
        "ORDER BY category",
        (CATEGORY_CC_PAYMENT, CATEGORY_TRANSFERS_REFUNDS),
    ).fetchall()
    return [row[0] for row in rows]


def _load_trips(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, start_date, end_date, "
        "status, type, location, created_by "
        "FROM trips ORDER BY start_date DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def _load_manual_occurrences(
    conn: sqlite3.Connection,
    *,
    start_date: datetime.date | None,
    end_date: datetime.date | None,
    trip_id: int | None,
    today: datetime.date,
) -> list[dict]:
    """Expand manual templates into dated rows.

    A trip-filtered request takes only the expenses linked to that
    trip, regardless of date, just as trip transactions are selected
    by membership. Injecting every recurrence there once made a trip
    view include unrelated household expenses.
    """
    query = (
        "SELECT m.id, m.description, m.amount, m.category, "
        "m.start_date, m.end_date, m.frequency, "
        "m.trip_id, tr.name AS trip_name "
        "FROM manual_expenses m "
        "LEFT JOIN trips tr ON m.trip_id = tr.id"
    )
    params: tuple = ()
    if trip_id is not None:
        query += " WHERE m.trip_id = ?"
        params = (trip_id,)
        start_date = end_date = None
    rows = conn.execute(query, params).fetchall()
    transactions: list[dict] = []
    for row in rows:
        manual = dict(row)
        current = datetime.date.fromisoformat(manual["start_date"])
        limit = (
            datetime.date.fromisoformat(manual["end_date"])
            if manual["end_date"] else today
        )
        if end_date:
            limit = min(limit, end_date)

        # Fast-forward close to the requested window while preserving
        # relativedelta's end-of-month stepping semantics.
        if start_date and current < start_date:
            if manual["frequency"] == "monthly":
                months = (
                    (start_date.year - current.year) * 12
                    + start_date.month - current.month
                )
                current += relativedelta(months=max(0, months - 1))
            elif manual["frequency"] == "yearly":
                years = start_date.year - current.year
                current += relativedelta(years=max(0, years - 1))

        while current <= limit:
            if not start_date or current >= start_date:
                transactions.append({
                    "id": f"manual_{manual['id']}_{current.isoformat()}",
                    "date": current.isoformat(),
                    "category": manual["category"],
                    "description": manual["description"],
                    "amount": manual["amount"],
                    "source": "MANUAL",
                    "trip_name": manual["trip_name"],
                    "trip_id": manual["trip_id"],
                    "needs_review": False,
                })
            if manual["frequency"] == "monthly":
                current += relativedelta(months=1)
            elif manual["frequency"] == "yearly":
                current += relativedelta(years=1)
            else:
                break
    return transactions


def _load_trip_summaries(
    conn: sqlite3.Connection,
    *,
    work: bool,
) -> list[dict]:
    membership = "IN" if work else "NOT IN"
    # A trip's cost is its spending transactions plus the one-time
    # manual expenses linked to it (cash, transfers, parcels billed on
    # statements that are not imported).
    rows = conn.execute(f"""
        WITH spend AS (
            SELECT t.trip_id, t.amount, t.date
            FROM transactions t
            WHERE t.trip_id IS NOT NULL AND {SPEND_FILTER_T}
            UNION ALL
            SELECT m.trip_id, m.amount, m.start_date
            FROM manual_expenses m
            WHERE m.trip_id IS NOT NULL
        )
        SELECT tr.id, tr.name, tr.start_date, tr.end_date,
               SUM(s.amount) AS total,
               MIN(s.date) AS earliest_tx_date
        FROM trips tr
        JOIN spend s ON s.trip_id = tr.id
        WHERE tr.id {membership} (
            SELECT DISTINCT t2.trip_id
            FROM transactions t2
            WHERE t2.category = 'Work (Reimbursable)'
              AND t2.trip_id IS NOT NULL
        )
        GROUP BY tr.id
        ORDER BY tr.start_date ASC
    """).fetchall()
    return [dict(row) for row in rows]


def _load_tax_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, tax_year, document_type, issuer, "
        "category, amount, status, needs_review, raw_data "
        "FROM tax_documents "
        "ORDER BY tax_year DESC, document_type"
    ).fetchall()
    return [dict(row) for row in rows]
