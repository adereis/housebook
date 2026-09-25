"""Project matcher: score unassigned transactions against a project.

The supervised-retrieval counterpart to ``trip_detector``. A trip is
*discovered* by clustering; a project is *declared* by the user, who
stores its matching criteria (keywords + categories) on the row. This
module retrieves candidate transactions for one such target.

Design contract (mirrors ``trip_detector``):
- **Pure read.** Scores and returns candidates; never writes. The agent
  reviews and assigns via ``housebook-audit verify <ids> --project <id>``.
- **High-signal gate.** A candidate must hit a keyword OR a configured
  category to appear at all. Amount and known-vendor only *boost* an
  already-qualifying row — they never qualify one alone. This keeps a
  noisy month of ordinary home spending out of a renovation's list.
- **Necessary but not sufficient.** A match inside the window is a
  *signal*, not a verdict — the agent confirms the charge truly belongs
  to the project (see ``prompts/projects.md``).
"""

import json
import sqlite3
from datetime import date

# Generic buckets that never disqualify a row but never qualify it alone.
# Flagged in `signals` so the agent scrutinizes them.
GENERIC_CATEGORIES = ("Uncategorized", "Miscellaneous", "Shopping & Retail")

# Renovations/events are lumpy — a large charge is weak corroboration.
LARGE_AMOUNT_THRESHOLD = 200.0

# Score weights.
KEYWORD_SCORE = 3
CATEGORY_SCORE = 2
LARGE_AMOUNT_SCORE = 1
KNOWN_VENDOR_SCORE = 1


def match_project(db_path, project_id, min_score=2):
    """Score unassigned transactions against one project's saved criteria.

    Returns ``{"project": {...}, "candidates": [...]}``. Candidates are
    sorted by descending score then date, each carrying a ``signals``
    list explaining why it surfaced. Writes nothing.

    Raises ``ValueError`` if ``project_id`` does not exist.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    proj = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if proj is None:
        conn.close()
        raise ValueError(f"No project with id {project_id}")

    keywords = [
        k.upper() for k in json.loads(proj["match_keywords"] or "[]") if k
    ]
    categories = set(json.loads(proj["match_categories"] or "[]"))
    # Open projects (end_date IS NULL) scan through today. A missing
    # start_date scans from the beginning — `date >= NULL` is NULL in
    # SQL, which silently matched nothing.
    start = proj["start_date"] or "0001-01-01"
    end = proj["end_date"] or date.today().isoformat()

    pool = conn.execute(
        """
        SELECT id, date, description, amount, category, source
        FROM transactions
        WHERE project_id IS NULL
          AND trip_id IS NULL
          AND date >= ?
          AND date <= ?
          AND status != 'RECONCILED'
          AND linked_transaction_id IS NULL
        ORDER BY date ASC
        """,
        (start, end),
    ).fetchall()

    known_merchants = _assigned_merchant_tokens(conn, project_id)
    conn.close()

    candidates = []
    for r in pool:
        desc = (r["description"] or "").upper()
        score = 0
        signals = []

        kw = next((k for k in keywords if k in desc), None)
        if kw is not None:
            score += KEYWORD_SCORE
            signals.append(f"keyword:{kw}")

        cat_hit = r["category"] in categories
        if cat_hit:
            score += CATEGORY_SCORE
            signals.append(f"category:{r['category']}")

        # High-signal gate: keyword OR category is required.
        if kw is None and not cat_hit:
            continue

        if abs(r["amount"]) >= LARGE_AMOUNT_THRESHOLD:
            score += LARGE_AMOUNT_SCORE
            signals.append("large_amount")

        if known_merchants and any(t in desc for t in known_merchants):
            score += KNOWN_VENDOR_SCORE
            signals.append("known_vendor")

        if r["category"] in GENERIC_CATEGORIES:
            signals.append("generic_category")  # note only; no score change

        if score >= min_score:
            candidates.append({
                "id": r["id"],
                "date": r["date"],
                "description": r["description"],
                "amount": r["amount"],
                "category": r["category"],
                "source": r["source"],
                "score": score,
                "signals": signals,
            })

    candidates.sort(key=lambda c: (-c["score"], c["date"]))

    return {
        "project": {
            "id": proj["id"],
            "name": proj["name"],
            "start_date": proj["start_date"],
            "end_date": proj["end_date"],
            "status": proj["status"],
            "keywords": keywords,
            "categories": sorted(categories),
        },
        "candidates": candidates,
    }


def _assigned_merchant_tokens(conn, project_id, min_len=4):
    """Leading merchant tokens from rows already assigned to the project.

    This makes matching *iterative*: once the agent confirms one charge
    from a vendor, later charges from the same vendor score a
    ``known_vendor`` boost. Returns an uppercased set of tokens.
    """
    rows = conn.execute(
        "SELECT description FROM transactions WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    tokens = set()
    for r in rows:
        desc = (r["description"] or "").upper()
        # First two tokens usually carry the merchant name
        # ("HOME DEPOT #1234 ..."); skip short noise tokens.
        for tok in desc.split()[:2]:
            tok = tok.strip(",.#*").strip()
            if len(tok) >= min_len and tok.isalpha():
                tokens.add(tok)
    return tokens
