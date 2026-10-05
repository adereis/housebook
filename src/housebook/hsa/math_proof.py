"""Canonical consolidated-payment proof for HSA expenses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MATH_TOLERANCE = 0.01


@dataclass(frozen=True)
class ProposedExpense:
    """One expense's post-update values, projected before any write."""

    expense_id: int
    amount: float
    unclaimed: float = 0.0


@dataclass(frozen=True)
class PaymentMathProof:
    transaction_id: int
    transaction_amount: float | None
    expense_total: float
    expense_ids: tuple[int, ...]
    # Parts of the charge the linked expenses declare but do not claim
    # (migration 026), such as supplies with no itemized receipt.
    unclaimed_total: float = 0.0

    @property
    def transaction_exists(self) -> bool:
        return self.transaction_amount is not None

    @property
    def accounted_total(self) -> float:
        """Claimed amounts plus declared unclaimed remainders."""
        return self.expense_total + self.unclaimed_total

    @property
    def accounted_label(self) -> str:
        """``$120.00``, or ``$120.00 + unclaimed $3.45``."""
        label = f"${self.expense_total:.2f}"
        if self.unclaimed_total:
            label += f" + unclaimed ${self.unclaimed_total:.2f}"
        return label

    @property
    def balanced(self) -> bool:
        return (
            self.transaction_amount is not None
            and abs(self.transaction_amount - self.accounted_total)
            <= MATH_TOLERANCE
        )


def calculate_payment_math(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    proposed_expense: ProposedExpense | None = None,
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
        params.append(proposed_expense.expense_id)
    rows = conn.execute(
        "SELECT id, patient_responsibility, unclaimed_amount "
        "FROM hsa_expenses "
        "WHERE transaction_id = ? AND status != 'DELETED'"
        + exclusion
        + " ORDER BY id",
        params,
    ).fetchall()

    expense_ids = [row[0] for row in rows]
    expense_total = sum((row[1] or 0) for row in rows)
    unclaimed_total = sum((row[2] or 0) for row in rows)
    if proposed_expense is not None:
        expense_ids.append(proposed_expense.expense_id)
        expense_total += proposed_expense.amount
        unclaimed_total += proposed_expense.unclaimed

    return PaymentMathProof(
        transaction_id=transaction_id,
        transaction_amount=(
            None if transaction is None else transaction[0]
        ),
        expense_total=expense_total,
        expense_ids=tuple(expense_ids),
        unclaimed_total=unclaimed_total,
    )
