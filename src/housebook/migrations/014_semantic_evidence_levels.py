"""Migrate evidence_level from 6-level to 4-level semantic system.

Old → New mapping:
  unverified, receipt_only, payment_only, eob_only → stub
  partial → weak
  audit_ready → ready
  (new level: strong — set by agent when 3+ corroborating sources)
"""


def migrate(conn):
    mapping = {
        "unverified": "stub",
        "receipt_only": "stub",
        "payment_only": "stub",
        "eob_only": "stub",
        "partial": "weak",
        "audit_ready": "ready",
    }
    for old, new in mapping.items():
        conn.execute(
            "UPDATE hsa_expenses SET evidence_level = ? "
            "WHERE evidence_level = ?",
            (new, old),
        )

    conn.execute(
        "UPDATE hsa_expenses SET evidence_level = 'stub' "
        "WHERE evidence_level NOT IN ('stub', 'weak', 'ready', 'strong')"
    )

    conn.commit()
