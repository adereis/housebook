import sqlite3
from collections import Counter
from datetime import date, timedelta

# Categories that seed trip clusters (strong travel signals)
ANCHOR_CATEGORIES = ("Flights", "Lodging", "Local Transit", "Work (Reimbursable)")

# Categories pulled into existing clusters but don't seed them
CONTEXTUAL_CATEGORIES = ("Dining & Takeout", "Entertainment")

# Keywords indicating advance bookings (flights, hotels)
ADVANCE_KEYWORDS = (
    "DELTA", "JETBLUE", "UNITED", "AIRWAYS", "AIRLINE", "FLIGHT",
    "MARRIOTT", "HILTON", "HYATT", "BOOKING.COM", "EXPEDIA",
    "HOTEL", "RESIDENCE INN", "NOVOTEL", "EGENCIA", "HERTZ",
    "AVIS", "ENTERPRISE",
)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# ISO 3166-1 alpha-2 codes for common travel destinations.
# CC merchant descriptions often end with 2-letter country codes
# (e.g., "RESTAURANT PARIS FR", "UBER TRIP NL").
INTL_COUNTRY_CODES = {
    "AR", "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "CO",
    "CR", "CZ", "DE", "DK", "DO", "EC", "EG", "ES", "FI", "FR",
    "GB", "GR", "HK", "HR", "HU", "IE", "IL", "IN", "IS", "IT",
    "JM", "JP", "KR", "MX", "MY", "NL", "NO", "NZ", "PA", "PE",
    "PH", "PL", "PT", "RO", "SE", "SG", "TH", "TR", "TW", "UA",
    "UK", "UY", "VN", "ZA",
}

# Codes that overlap with US states are resolved by context:
# if the cluster already has US-state matches, prefer the state
# interpretation; otherwise treat as country.
_AMBIGUOUS_CODES = US_STATES & INTL_COUNTRY_CODES


def detect_trips(db_path, months=12, min_transactions=3, gap_days=3):
    """Detect candidate trip clusters from unassigned transactions.

    Returns a dict with 'candidates' and 'advance_payments' lists.
    """
    cutoff = date.today() - timedelta(days=months * 30)
    all_categories = ANCHOR_CATEGORIES + CONTEXTUAL_CATEGORIES

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        SELECT id, date, description, amount, category, source
        FROM transactions
        WHERE date >= ?
          AND category IN ({})
          AND source != 'Amazon'
          AND category != 'Transfers & Refunds'
          AND trip_id IS NULL
        ORDER BY date ASC
        """.format(",".join("?" for _ in all_categories)),
        (cutoff.isoformat(), *all_categories),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    anchors = [r for r in rows if r["category"] in ANCHOR_CATEGORIES]
    contextual = [r for r in rows if r["category"] in CONTEXTUAL_CATEGORIES]

    clusters = _cluster_by_date(anchors, gap_days)
    clusters = [cl for cl in clusters if len(cl) >= min_transactions]

    candidates = []
    for cluster in clusters:
        candidates.append(_enrich_cluster(cluster, contextual))

    clustered_ids = set()
    for cand in candidates:
        clustered_ids.update(cand["transaction_ids"])

    advance_payments = _detect_advance_payments(
        anchors, clustered_ids, candidates,
    )

    return {"candidates": candidates, "advance_payments": advance_payments}


def _cluster_by_date(anchors, gap_days):
    """Group anchor transactions by date proximity."""
    if not anchors:
        return []

    clusters = []
    current = [anchors[0]]

    for tx in anchors[1:]:
        prev_date = date.fromisoformat(current[-1]["date"])
        tx_date = date.fromisoformat(tx["date"])

        if (tx_date - prev_date).days <= gap_days:
            current.append(tx)
        else:
            clusters.append(current)
            current = [tx]

    clusters.append(current)
    return clusters


def _enrich_cluster(cluster, contextual):
    """Add contextual transactions, location hints, and metadata."""
    start = min(date.fromisoformat(tx["date"]) for tx in cluster)
    end = max(date.fromisoformat(tx["date"]) for tx in cluster)

    window_txs = [
        tx for tx in contextual
        if start <= date.fromisoformat(tx["date"]) <= end
    ]
    all_txs = cluster + window_txs

    location = extract_location_hints(all_txs)
    has_work = any(
        tx["category"] == "Work (Reimbursable)" for tx in all_txs
    )

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "location": location,
        "type": "work" if has_work else "unknown",
        "total_spend": sum(float(tx["amount"]) for tx in all_txs),
        "transaction_count": len(all_txs),
        "transaction_ids": [tx["id"] for tx in all_txs],
        "anchor_count": len(cluster),
        "contextual_count": len(window_txs),
    }


def _detect_advance_payments(anchors, clustered_ids, candidates):
    """Find unclustered travel bookings 30-180 days before a cluster."""
    unclustered = [tx for tx in anchors if tx["id"] not in clustered_ids]

    advance_payments = []
    for tx in unclustered:
        desc_upper = tx["description"].upper()
        if not any(kw in desc_upper for kw in ADVANCE_KEYWORDS):
            continue

        tx_date = date.fromisoformat(tx["date"])
        for i, cand in enumerate(candidates):
            days_before = (
                date.fromisoformat(cand["start_date"]) - tx_date
            ).days
            if 30 <= days_before <= 180:
                advance_payments.append({
                    "transaction_id": tx["id"],
                    "date": tx["date"],
                    "description": tx["description"],
                    "amount": float(tx["amount"]),
                    "candidate_trip_index": i,
                    "days_before_trip": days_before,
                })

    return advance_payments


def extract_location_hints(transactions):
    """Extract likely location from transaction descriptions.

    CC merchants typically end with a 2-letter code: US states for
    domestic transactions, ISO country codes for international.

    Each transaction casts one vote: the rightmost known code among
    its last three tokens ("ACME CO NY" votes NY, not CO too).
    Unambiguous votes decide the context — more country codes than
    US states means an international trip, and ties go international,
    the signal state-only matching used to miss. The most common code
    valid in that context wins, so an ambiguous code ("IN" = Indiana
    or India) is read the way the rest of the trip reads.

    A majority, not mere presence, sets the context: a trip abroad
    that includes parking at the home airport ("BOSTON MA") is still
    abroad, and a domestic trip with one foreign-billed ride ("UBER
    TRIP NL") is still domestic.
    """
    votes = Counter()
    for tx in transactions:
        tokens = tx["description"].upper().split()
        for token in reversed(tokens[-3:]):
            cleaned = token.strip(",.")
            if cleaned in US_STATES or cleaned in INTL_COUNTRY_CODES:
                votes[cleaned] += 1
                break

    if not votes:
        return None

    us_votes = sum(
        n for code, n in votes.items()
        if code in US_STATES and code not in _AMBIGUOUS_CODES
    )
    intl_votes = sum(
        n for code, n in votes.items()
        if code in INTL_COUNTRY_CODES and code not in _AMBIGUOUS_CODES
    )
    if us_votes == intl_votes == 0:
        return votes.most_common(1)[0][0]
    context = INTL_COUNTRY_CODES if intl_votes >= us_votes else US_STATES
    in_context = Counter(
        {code: n for code, n in votes.items() if code in context}
    )
    return in_context.most_common(1)[0][0]
