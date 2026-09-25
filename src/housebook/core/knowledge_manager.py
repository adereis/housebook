import json
import os
import re


def clean_keyword(kw: str) -> str:
    """Simplifies a raw transaction description into a reusable keyword."""
    # Remove common prefixes
    kw = re.sub(r"^Amazon: ", "", kw, flags=re.IGNORECASE)
    kw = re.sub(r"^TST\*\s*", "", kw, flags=re.IGNORECASE)
    kw = re.sub(r"^SQ\s*\*\s*", "", kw, flags=re.IGNORECASE)
    kw = re.sub(r"^PAYPAL\s*\*\s*", "", kw, flags=re.IGNORECASE)

    # Remove common noise (Store numbers, phone numbers, addresses)
    kw = re.sub(r"#\d+.*$", "", kw)  # Anything after a #
    kw = re.sub(r"\d{3,}-\d{3,}.*$", "", kw)  # Phone numbers
    kw = re.sub(r"\s+[A-Z]{2}$", "", kw)  # State codes at the end (e.g., "MA", "NH")
    kw = re.sub(
        r"\s+[A-Z][a-z]+\s+[A-Z]{2}$", "", kw
    )  # City + State (e.g., "Boston MA")
    kw = re.sub(r"\d{4,}.*$", "", kw)  # Long digit strings

    return kw.strip().lower()


def export_db_to_json(db_path: str, json_path: str):
    """Exports learned rules from DB to JSON for portability/backup."""
    import sqlite3

    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        "SELECT category, keyword FROM categorization_rules ORDER BY category, keyword"
    )
    rules_raw = c.fetchall()
    conn.close()

    rules = {}
    for cat, kw in rules_raw:
        if cat not in rules:
            rules[cat] = []
        # Lowercase to reduce noise since matching is case-insensitive
        clean_kw = kw.lower()
        if clean_kw not in rules[cat]:
            rules[cat].append(clean_kw)

    with open(json_path, "w") as f:
        json.dump(rules, f, indent=4)
    print(f"Exported database rules to {json_path}")


def optimize_db_rules(db_path: str, heuristics_path: str):
    """Cleans noisy rules and removes redundancy with heuristics."""
    import sqlite3

    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Load heuristics to avoid redundancy
    heuristics = {}
    if os.path.exists(heuristics_path):
        with open(heuristics_path, "r") as f:
            heuristics = json.load(f)

    all_heuristic_keywords = set()
    for h_keywords in heuristics.values():
        for hk in h_keywords:
            all_heuristic_keywords.add(hk.lower())

    c.execute("SELECT category, keyword FROM categorization_rules")
    old_rules = c.fetchall()

    optimized_count = 0
    deleted_count = 0

    for cat, kw in old_rules:
        # Lowercase and clean
        new_kw = clean_keyword(kw).lower()

        # Skip if covered by heuristics, too short, or manual
        is_heuristic = new_kw in all_heuristic_keywords
        is_too_short = len(new_kw) < 3
        is_manual = new_kw.startswith("manual_")

        if (is_heuristic or is_too_short) and not is_manual:
            c.execute("DELETE FROM categorization_rules WHERE keyword = ?", (kw,))
            deleted_count += 1
            continue

        if new_kw != kw:
            # Try to update to cleaned/lowercased version
            try:
                c.execute(
                    "UPDATE categorization_rules SET keyword = ? WHERE keyword = ?",
                    (new_kw, kw),
                )
                optimized_count += 1
            except sqlite3.IntegrityError:
                # If lowercased version already exists, just delete the noisy one
                c.execute("DELETE FROM categorization_rules WHERE keyword = ?", (kw,))
                deleted_count += 1

    # 3. Remove redundant substrings within the same category
    # If we have "starbucks" and "starbucks store #123", the latter is redundant.
    c.execute(
        "SELECT category, keyword FROM categorization_rules "
        "ORDER BY category, length(keyword) ASC"
    )
    all_rules = c.fetchall()

    # Group by category
    cat_map = {}
    for cat, kw in all_rules:
        if cat not in cat_map:
            cat_map[cat] = []
        cat_map[cat].append(kw)

    for cat, keywords in cat_map.items():
        # Keywords are sorted by length (shortest first)
        for i, short_kw in enumerate(keywords):
            for long_kw in keywords[i + 1 :]:
                # If short_kw is a substring of long_kw, long_kw is redundant
                if short_kw in long_kw:
                    c.execute(
                        "DELETE FROM categorization_rules "
                        "WHERE category = ? AND keyword = ?",
                        (cat, long_kw),
                    )
                    deleted_count += 1

    conn.commit()
    conn.close()
    print(
        f"Optimization complete: {optimized_count} rules cleaned, "
        f"{deleted_count} redundant rules removed."
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Knowledge Manager")
    parser.add_argument(
        "--export", action="store_true", help="Export DB rules to config/rules.json"
    )
    parser.add_argument(
        "--optimize", action="store_true", help="Clean and deduplicate DB rules"
    )
    args = parser.parse_args()

    from housebook.config.settings import DB_PATH, HEURISTICS_JSON, RULES_JSON

    if args.export:
        export_db_to_json(DB_PATH, RULES_JSON)
    elif args.optimize:
        optimize_db_rules(DB_PATH, HEURISTICS_JSON)
    else:
        # Default behavior: run optimization
        print("Running database rule optimization...")
        optimize_db_rules(DB_PATH, HEURISTICS_JSON)
