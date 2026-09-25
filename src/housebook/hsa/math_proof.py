"""Canonical consolidated-payment proof for HSA expenses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MATH_TOLERANCE = 0.01


@dataclass(frozen=True)
class PaymentMathProof:
    transaction_id: int
    transaction_amount: float | None
    expense_total: float
    expense_ids: tuple[int, ...]

    @property
    def transaction_exists(self) -> bool:
        return self.transaction_amount is not None

    @property
    def balanced(self) -> bool:
        return (
            self.transaction_amount is not None
            and abs(self.transaction_amount - self.expense_total)
            <= MATH_TOLERANCE
        )


def calculate_payment_math(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    proposed_expense: tuple[int, float] | None = None,
) -> PaymentMathProof:
    """Calculate one CC transaction's linked HSA expense total.

    ``proposed_expense`` replaces that expense ID in the linked set and
    also handles a new link not yet written. This lets ``verify`` prove
    the post-update state before making any audit-log or ledger writes.
    """
    transaction = conn.execute(
        "SELECT amount FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()

    params: list = [transaction_id]
    exclusion = ""
    if proposed_expense is not None:
        exclusion = " AND id != ?"
        params.append(proposed_expense[0])
    rows = conn.execute(
        "SELECT id, patient_responsibility FROM hsa_expenses "
        "WHERE transaction_id = ? AND status != 'DELETED'"
        + exclusion
        + " ORDER BY id",
        params,
    ).fetchall()

    expense_ids = [row[0] for row in rows]
    expense_total = sum(
        (row[1] or 0) for row in rows
    )
    if proposed_expense is not None:
        expense_ids.append(proposed_expense[0])
        expense_total += proposed_expense[1]

    return PaymentMathProof(
        transaction_id=transaction_id,
        transaction_amount=(
            None if transaction is None else transaction[0]
        ),
        expense_total=expense_total,
        expense_ids=tuple(expense_ids),
    )
