import sqlite3
from decimal import Decimal

from housebook.config.settings import DB_PATH


def generate_category_report():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    query = """
        SELECT category, SUM(amount) as total, COUNT(*) as count
        FROM transactions
        WHERE category NOT IN ('Excluded', 'Work (Reimbursable)')
        GROUP BY category
        ORDER BY total DESC
    """

    c.execute(query)
    rows = c.fetchall()

    print("\n" + "=" * 50)
    print(f"{'CATEGORY':<30} | {'TOTAL':>10} | {'ITEMS':>5}")
    print("-" * 50)

    grand_total = Decimal("0")
    for category, total, count in rows:
        total_val = Decimal(str(total)) if total else Decimal("0")
        print(f"{category:<30} | ${total_val:>9.2f} | {count:>5}")
        grand_total += total_val

    print("-" * 50)
    print(f"{'GRAND TOTAL':<30} | ${grand_total:>9.2f} |")
    print("=" * 50 + "\n")

    conn.close()


def main():
    generate_category_report()


if __name__ == "__main__":
    main()
