import argparse
import datetime
import json
import os
import random
import sqlite3

from housebook.migrations.runner import run_migrations

# Constants for the Demo
DB_NAME = "finance.db"
DEMO_WORKSPACE = "demo-workspace"
SEED = 42

# Categories and Merchants (Realistic)
MERCHANTS = {
    "Groceries": [
        ("Whole Foods Market", 120, 250),
        ("Market Basket", 80, 180),
        ("Trader Joe's", 60, 140),
        ("Wegmans", 150, 300),
    ],
    "Dining & Takeout": [
        ("Starbucks", 5, 25),
        ("Chipotle", 15, 45),
        ("Panera Bread", 20, 50),
        ("The Local Grill", 60, 120),
        ("Pizzaria Regina", 30, 70),
        ("Blue Bottle Coffee", 8, 15),
    ],
    "Auto & Fuel": [
        ("Shell Oil", 45, 85),
        ("ExxonMobil", 40, 80),
        ("Sunoco", 40, 75),
        ("Local Car Wash", 20, 40),
    ],
    "Bills & Utilities": [
        ("Eversource Energy", 150, 350),
        ("National Grid", 80, 200),
        ("Verizon Wireless", 120, 180),
        ("Comcast Xfinity", 90, 140),
        ("Google One", 9.99, 9.99),
        ("Netflix", 15.99, 19.99),
    ],
    "Health": [
        ("CVS Pharmacy", 10, 60),
        ("Walgreens", 15, 50),
        ("Riverside Medical Center", 25, 250),
    ],
    "Wellness": [
        ("Maple Fitness", 85, 85),
    ],
}

AMAZON_PRODUCTS = {
    "Groceries": [
        "Kind Bars (12 pack)",
        "Organic Olive Oil",
        "Nespresso Pods",
        "Matcha Powder",
    ],
    "Pets": [
        "Blue Buffalo Life Protection Formula",
        "Greenies Dental Treats",
        "Chuckit! Launcher",
        "Seresto Flea Collar",
    ],
    "Shopping & Retail": [
        "Echo Dot (5th Gen)",
        "Kindle Paperwhite",
        "USB-C Charging Cable",
        "Anker Power Bank",
        "Apple AirTag",
    ],
    "Health": [
        "Advil Liqui-Gels",
    ],
    "Wellness": [
        "Multivitamin Gummy",
        "Electric Toothbrush Heads",
        "Sunscreen SPF 50",
    ],
    "Home & Garden": [
        "LED Light Bulbs (6 pack)",
        "Command Strips",
        "FilterBuy HVAC Filter",
        "Bounty Paper Towels",
    ],
    "Entertainment": [
        "LEGO Star Wars Set",
        "Nintendo Switch Controller",
        "Board Game: Catan",
    ],
}

TRIPS = [
    # 2021
    {
        "name": "The Great Lockdown Escape",
        "pun": True,
        "date": "2021-07-15",
        "duration": 7,
        "type": "Personal",
        "location": "Cape Cod, MA",
    },
    {
        "name": "Q3 Regional Offsite",
        "pun": False,
        "date": "2021-09-10",
        "duration": 3,
        "type": "Work",
        "location": "Austin, TX",
    },
    {
        "name": "Visit to In-laws",
        "pun": False,
        "date": "2021-11-24",
        "duration": 5,
        "type": "Personal",
        "location": "Philadelphia, PA",
    },
    # 2022
    {
        "name": "The Bull Market Beach Bash",
        "pun": True,
        "date": "2022-06-20",
        "duration": 10,
        "type": "Personal",
        "location": "Honolulu, HI",
    },
    {
        "name": "Visit to Grandma",
        "pun": False,
        "date": "2022-11-22",
        "duration": 5,
        "type": "Personal",
        "location": "Baltimore, MD",
    },
    {
        "name": "Risk Summit 2022",
        "pun": False,
        "date": "2022-03-15",
        "duration": 4,
        "type": "Work",
        "location": "Las Vegas, NV",
    },
    # 2023
    {
        "name": "The Great Recession Retreat",
        "pun": True,
        "date": "2023-08-05",
        "duration": 4,
        "type": "Personal",
        "location": "White Mountains, NH",
    },
    {
        "name": "Actuarial World Conference",
        "pun": False,
        "date": "2023-10-15",
        "duration": 3,
        "type": "Work",
        "location": "San Francisco, CA",
    },
    {
        "name": "Fall Foliage Trip",
        "pun": False,
        "date": "2023-10-01",
        "duration": 2,
        "type": "Personal",
        "location": "Vermont",
    },
    # 2024
    {
        "name": "The Halving Holiday",
        "pun": True,
        "date": "2024-04-12",
        "duration": 7,
        "type": "Personal",
        "location": "San Diego, CA",
    },
    {
        "name": "European Sales Tour",
        "pun": False,
        "date": "2024-09-15",
        "duration": 12,
        "type": "Work",
        "location": "London/Paris",
    },
    {
        "name": "Memorial Day Weekend",
        "pun": False,
        "date": "2024-05-24",
        "duration": 3,
        "type": "Personal",
        "location": "Maine",
    },
    # 2025
    {
        "name": "The Dividend Discovery",
        "pun": True,
        "date": "2025-07-01",
        "duration": 14,
        "type": "Personal",
        "location": "Tuscany, Italy",
    },
    {
        "name": "Ski Weekend",
        "pun": False,
        "date": "2025-02-14",
        "duration": 3,
        "type": "Personal",
        "location": "Stowe, VT",
    },
    {
        "name": "Insurance Forum NYC",
        "pun": False,
        "date": "2025-05-10",
        "duration": 2,
        "type": "Work",
        "location": "New York, NY",
    },
    # 2026
    {
        "name": "The Soft Landing Sojourn",
        "pun": True,
        "date": "2026-03-10",
        "duration": 5,
        "type": "Personal",
        "location": "Savannah, GA",
    },
]

# Pre-determined home maintenance events (1-2x/year, irregular amounts)
HOME_MAINTENANCE = [
    ("2021-02-18", "Roto-Rooter Plumbing", 385.00),
    ("2021-07-22", "Gaines HVAC - Summer Tune-up", 225.00),
    ("2022-01-09", "Mr. Electric - Panel Inspection", 310.00),
    ("2022-08-14", "ServiceMaster - Dryer Vent Cleaning", 175.00),
    ("2023-03-05", "Roto-Rooter Plumbing", 520.00),
    ("2023-10-28", "Gaines HVAC - Fall Tune-up", 245.00),
    ("2024-05-11", "Sears Appliance Repair - Refrigerator", 430.00),
    ("2024-11-02", "Mr. Electric - Outlet Replacement", 290.00),
    ("2025-01-19", "Roto-Rooter Plumbing", 610.00),
    ("2025-09-07", "Gaines HVAC - System Replacement", 4200.00),
    ("2026-02-03", "ServiceMaster - Water Damage Remediation", 1850.00),
]

# Pre-determined Amazon returns (~10% return rate, spread across years)
AMAZON_RETURNS = [
    ("2021-04-12", "REFUND: Echo Dot (5th Gen)", -45.99, "Shopping & Retail"),
    ("2021-11-08", "REFUND: USB-C Charging Cable", -12.50, "Shopping & Retail"),
    ("2022-02-20", "REFUND: Electric Toothbrush Heads", -28.00, "Wellness"),
    ("2022-09-14", "REFUND: Nintendo Switch Controller", -62.00, "Entertainment"),
    ("2023-01-30", "REFUND: Anker Power Bank", -35.00, "Shopping & Retail"),
    ("2023-06-18", "REFUND: LEGO Star Wars Set", -89.00, "Entertainment"),
    ("2023-12-28", "REFUND: Apple AirTag", -29.00, "Shopping & Retail"),
    ("2024-03-05", "REFUND: Kindle Paperwhite", -139.00, "Shopping & Retail"),
    ("2024-10-22", "REFUND: LED Light Bulbs (6 pack)", -18.00, "Home & Garden"),
    ("2025-02-11", "REFUND: Board Game: Catan", -44.00, "Entertainment"),
    ("2025-08-30", "REFUND: Seresto Flea Collar", -55.00, "Pets"),
    ("2026-01-15", "REFUND: Multivitamin Gummy", -24.00, "Wellness"),
]

INFLATION_SCALES = {
    2021: 1.0,
    2022: 1.07,
    2023: 1.15,
    2024: 1.20,
    2025: 1.23,
    2026: 1.25,
}


def get_db_path():
    return os.path.join(DEMO_WORKSPACE, "data", DB_NAME)


def setup_workspace():
    os.makedirs(os.path.join(DEMO_WORKSPACE, "data"), exist_ok=True)
    os.makedirs(os.path.join(DEMO_WORKSPACE, "config"), exist_ok=True)
    os.makedirs(os.path.join(DEMO_WORKSPACE, "input"), exist_ok=True)

    # Create dummy user profile
    profile = {
        "user_name": "Sterling Ledger",
        "household_members": ["Sterling", "Penny", "Buck", "Ally"],
        "special_notes": (
            "Upper-middle class household with heavy Amazon usage"
            " and periodic work travel."
        ),
        "accounts": [
            {
                "name": "Chase Sapphire Reserve",
                "type": "Credit Card",
                "owner": "Sterling",
            },
            {
                "name": "Amex Blue Cash Preferred",
                "type": "Credit Card",
                "owner": "Penny",
            },
            {"name": "Harbor Trust Checking", "type": "Checking", "owner": "Joint"},
        ],
    }
    with open(os.path.join(DEMO_WORKSPACE, "config", "user_profile.json"), "w") as f:
        json.dump(profile, f, indent=4)


def generate_transactions():
    random.seed(SEED)
    start_date = datetime.date(2021, 1, 1)
    end_date = datetime.date(2026, 4, 3)

    transactions = []
    current_date = start_date

    # Pre-generate trip-specific expenses to ensure they exist
    for trip in TRIPS:
        t_start = datetime.date.fromisoformat(trip["date"])
        year_scale = INFLATION_SCALES.get(t_start.year, 1.25)
        is_work = trip["type"] == "Work"
        flight_cat = "Work (Reimbursable)" if is_work else "Flights"
        lodging_cat = "Work (Reimbursable)" if is_work else "Lodging"
        source = "Chase Sapphire"

        # 1. Airfare (for non-local trips)
        if trip["location"] not in [
            "Cape Cod, MA",
            "White Mountains, NH",
            "Vermont",
            "Maine",
            "Stowe, VT",
        ]:
            amount = random.uniform(400, 1200) * year_scale * (4 if not is_work else 1)
            transactions.append(
                {
                    "date": (
                        t_start - datetime.timedelta(days=random.randint(30, 60))
                    ).isoformat(),
                    "description": f"Delta Air Lines - {trip['name']}",
                    "amount": round(amount, 2),
                    "category": flight_cat,
                    "source": source,
                    "status": "AGENT_VERIFIED",
                    "original_file": "chase_statement.pdf",
                }
            )

        # 2. Hotel
        hotel_rate = random.uniform(200, 450) * year_scale
        amount = hotel_rate * trip["duration"]
        transactions.append(
            {
                "date": (
                    t_start + datetime.timedelta(days=trip["duration"])
                ).isoformat(),
                "description": f"Marriott {trip['location']} - {trip['name']}",
                "amount": round(amount, 2),
                "category": lodging_cat,
                "source": source,
                "status": "AGENT_VERIFIED",
                "original_file": "chase_statement.pdf",
            }
        )

    # Pre-generate home maintenance events
    for date_str, desc, amount in HOME_MAINTENANCE:
        transactions.append(
            {
                "date": date_str,
                "description": desc,
                "amount": amount,
                "category": "Home & Garden",
                "source": "Chase Sapphire",
                "status": "AGENT_VERIFIED",
                "original_file": "chase_statement.pdf",
            }
        )

    # Pre-generate Amazon returns
    for date_str, desc, amount, cat in AMAZON_RETURNS:
        transactions.append(
            {
                "date": date_str,
                "description": desc,
                "amount": amount,
                "category": cat,
                "source": "Amazon CSV",
                "status": "RECONCILED",
                "original_file": "Order_History.csv",
            }
        )

    # Pre-generate kids' swim club registration (semi-annual: March and September)
    for year in range(2021, 2027):
        for month in [3, 9]:
            reg_date = datetime.date(year, month, 5)
            if reg_date > end_date:
                continue
            amount = random.uniform(175, 250)
            transactions.append(
                {
                    "date": reg_date.isoformat(),
                    "description": "Riverside Swim Club - Registration",
                    "amount": round(amount, 2),
                    "category": "Entertainment",
                    "source": "Chase Sapphire",
                    "status": "AGENT_VERIFIED",
                    "original_file": "chase_statement.pdf",
                }
            )

    while current_date <= end_date:
        year_scale = INFLATION_SCALES.get(current_date.year, 1.25)

        # 1. Groceries (Weekly)
        if current_date.weekday() == 6:  # Sunday
            category = "Groceries"
            merchant, min_amt, max_amt = random.choice(MERCHANTS[category])
            amount = random.uniform(min_amt, max_amt) * year_scale
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": merchant,
                    "amount": round(amount, 2),
                    "category": category,
                    "source": "Amex Blue Cash",
                    "status": "AGENT_VERIFIED",
                    "original_file": "amex_statement.pdf",
                }
            )

        # 2. Dining (2-3x/week) — mostly Sterling's Chase, occasionally Penny's Amex
        if random.random() < 0.3:
            category = "Dining & Takeout"
            merchant, min_amt, max_amt = random.choice(MERCHANTS[category])
            amount = random.uniform(min_amt, max_amt) * year_scale
            on_amex = random.random() < 0.25
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": merchant,
                    "amount": round(amount, 2),
                    "category": category,
                    "source": "Amex Blue Cash" if on_amex else "Chase Sapphire",
                    "status": "AGENT_VERIFIED",
                    "original_file": (
                        "amex_statement.pdf" if on_amex else "chase_statement.pdf"
                    ),
                }
            )

        # 3. Fuel (Bi-weekly)
        if current_date.day % 14 == 0:
            category = "Auto & Fuel"
            merchant, min_amt, max_amt = random.choice(MERCHANTS[category])
            amount = random.uniform(min_amt, max_amt) * year_scale
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": merchant,
                    "amount": round(amount, 2),
                    "category": category,
                    "source": "Amex Blue Cash",
                    "status": "AGENT_VERIFIED",
                    "original_file": "amex_statement.pdf",
                }
            )

        # 4. Utilities (Monthly)
        if current_date.day == 5:
            for merchant, min_amt, max_amt in MERCHANTS["Bills & Utilities"]:
                amount = random.uniform(min_amt, max_amt) * (
                    year_scale if min_amt > 20 else 1.0
                )
                transactions.append(
                    {
                        "date": current_date.isoformat(),
                        "description": merchant,
                        "amount": round(amount, 2),
                        "category": "Bills & Utilities",
                        "source": "Harbor Trust Checking",
                        "status": "AGENT_VERIFIED",
                        "original_file": "harbor_trust_stmt.pdf",
                    }
                )

        # 5. Wellness (2-3 times a month)
        if random.random() < 0.09:
            category = "Wellness"
            merchant, min_amt, max_amt = random.choice(MERCHANTS[category])
            amount = random.uniform(min_amt, max_amt) * year_scale
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": merchant,
                    "amount": round(amount, 2),
                    "category": category,
                    "source": "Amex Blue Cash",
                    "status": "AGENT_VERIFIED",
                    "original_file": "amex_statement.pdf",
                }
            )

        # 6. Vet visits (Quarterly — month divisible by 3, day 20)
        if current_date.month % 3 == 0 and current_date.day == 20:
            amount = random.uniform(150, 400) * year_scale
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": "Golden Paw Veterinary Clinic",
                    "amount": round(amount, 2),
                    "category": "Pets",
                    "source": "Chase Sapphire",
                    "status": "AGENT_VERIFIED",
                    "original_file": "chase_statement.pdf",
                }
            )

        # 7. Kids: piano lessons (Monthly from 2022, day 10) — Penny's Amex
        if current_date.year >= 2022 and current_date.day == 10:
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": "Harmony Piano Studio - Lessons",
                    "amount": 120.00,
                    "category": "Entertainment",
                    "source": "Amex Blue Cash",
                    "status": "AGENT_VERIFIED",
                    "original_file": "amex_statement.pdf",
                }
            )

        # 8. Kids: back-to-school shopping (August 20 each year)
        if current_date.month == 8 and current_date.day == 20:
            amount = random.uniform(100, 200) * year_scale
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": "Staples - Back to School",
                    "amount": round(amount, 2),
                    "category": "Shopping & Retail",
                    "source": "Chase Sapphire",
                    "status": "AGENT_VERIFIED",
                    "original_file": "chase_statement.pdf",
                }
            )

        # 9. Amazon Orders (3-4 times a month)
        if random.random() < 0.12:
            num_items = random.randint(1, 4)
            for _ in range(num_items):
                cat = random.choice(list(AMAZON_PRODUCTS.keys()))
                product = random.choice(AMAZON_PRODUCTS[cat])
                amount = random.uniform(10, 80) * year_scale
                transactions.append(
                    {
                        "date": current_date.isoformat(),
                        "description": product,
                        "amount": round(amount, 2),
                        "category": cat,
                        "source": "Amazon CSV",
                        "status": "RECONCILED",
                        "original_file": "Order_History.csv",
                    }
                )

        # 10. Mortgage (Monthly)
        if current_date.day == 1:
            transactions.append(
                {
                    "date": current_date.isoformat(),
                    "description": "Vault Mortgage",
                    "amount": 3850.00,
                    "category": "Housing",
                    "source": "Harbor Trust Checking",
                    "status": "AGENT_VERIFIED",
                    "original_file": "harbor_trust_stmt.pdf",
                }
            )

        current_date += datetime.timedelta(days=1)

    return transactions


def seed_db():
    db_path = get_db_path()
    if os.path.exists(db_path):
        print(f"Demo database already exists at {db_path}. Skipping seed.")
        return

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 1. Seed Trips
    trip_id_map = {}
    for t_data in TRIPS:
        start_dt = datetime.date.fromisoformat(t_data["date"])
        end_dt = start_dt + datetime.timedelta(days=t_data["duration"])
        c.execute(
            "INSERT INTO trips"
            " (name, start_date, end_date, status, type, location)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                t_data["name"],
                start_dt.isoformat(),
                end_dt.isoformat(),
                "confirmed",
                t_data["type"],
                t_data["location"],
            ),
        )
        trip_id_map[t_data["name"]] = c.lastrowid

    # 2. Seed Transactions
    txns = generate_transactions()

    # Categories that should be linked to trips if they occur during trip dates
    TRAVEL_LINK_CATEGORIES = [
        "Dining & Takeout",
        "Flights",
        "Lodging",
        "Local Transit",
        "Entertainment",
        "Auto & Fuel",
        "Groceries",
        "Work (Reimbursable)",
    ]

    for t in txns:
        # Link to trips if date matches AND category is travel-related
        trip_id = None
        t_dt = datetime.date.fromisoformat(t["date"])

        # Only link if the category is something likely to be part of a trip
        if t["category"] in TRAVEL_LINK_CATEGORIES:
            for t_name, tid in trip_id_map.items():
                # Find the trip data to get start/end
                trip_info = next(item for item in TRIPS if item["name"] == t_name)
                t_start = datetime.date.fromisoformat(trip_info["date"])
                t_end = t_start + datetime.timedelta(days=trip_info["duration"])
                if t_start <= t_dt <= t_end:
                    trip_id = tid
                    # If it's a work trip, override category for dining/travel
                    if trip_info["type"] == "Work" and t["category"] in [
                        "Flights",
                        "Lodging",
                        "Local Transit",
                        "Dining & Takeout",
                    ]:
                        t["category"] = "Work (Reimbursable)"
                    break

        # Recent transactions (last 30 days) should be UNVERIFIED and need review
        is_recent = t_dt > (datetime.date(2026, 4, 3) - datetime.timedelta(days=30))
        status = "UNVERIFIED" if is_recent else t["status"]
        needs_review = 1 if is_recent else 0

        c.execute(
            "INSERT INTO transactions"
            " (date, description, amount, category, source,"
            " status, original_file, needs_review, trip_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t["date"],
                t["description"],
                t["amount"],
                t["category"],
                t["source"],
                status,
                t["original_file"],
                needs_review,
                trip_id,
            ),
        )

    # 2b. Seed a user-initiated Project (open renovation) with a mix of
    # already-assigned charges (so `projects`/`project-summary` show spend
    # vs budget) and unassigned matching charges (so `match-project` has
    # candidates to surface in the demo).
    c.execute(
        "INSERT INTO projects"
        " (name, description, type, location, start_date, end_date,"
        " status, match_keywords, match_categories, budget, created_by)"
        " VALUES (?, ?, ?, ?, ?, NULL, 'open', ?, ?, ?, 'agent')",
        (
            "Sunrise Cottage Bath Refresh",
            "Ledger family master bathroom renovation",
            "renovation",
            "Master Bathroom",
            "2026-01-01",
            json.dumps(["MAPLE HARDWARE", "VAULT PLUMBING", "TILE"]),
            json.dumps(["Home Improvement", "Professional Services"]),
            9000.0,
        ),
    )
    project_id = c.lastrowid

    # An ongoing (open) renovation: earlier charges are already audited and
    # assigned (counted toward the project); the recent ones are left
    # UNVERIFIED so `match-project` surfaces them as candidates in the demo.
    # Assigned dates sit before the 30-day audit window (>2026-03-04), which
    # is intentionally all-UNVERIFIED.
    # (description, amount, category, date, assigned)
    project_txns = [
        ("MAPLE HARDWARE #210", 1240.55, "Home Improvement", "2026-01-15", True),
        ("VAULT PLUMBING CO", 2150.00, "Professional Services", "2026-02-10", True),
        ("MAPLE HARDWARE REFUND", -88.10, "Home Improvement", "2026-02-20", True),
        ("MAPLE HARDWARE #210", 318.40, "Home Improvement", "2026-03-19", False),
        ("STERLING TILE WORKS", 880.00, "Shopping & Retail", "2026-03-22", False),
    ]
    for desc, amt, cat, dt, assigned in project_txns:
        c.execute(
            "INSERT INTO transactions"
            " (date, description, amount, category, source,"
            " status, original_file, needs_review, project_id)"
            " VALUES (?, ?, ?, ?, 'BoA', ?, ?, ?, ?)",
            (
                dt, desc, amt, cat,
                "AGENT_VERIFIED" if assigned else "UNVERIFIED",
                "Demo_BoA_2026-03.pdf",
                0 if assigned else 1,
                project_id if assigned else None,
            ),
        )

    # 3. Seed Manual Expenses
    manual_expenses = [
        ("House Cleaning", 200.0, "Bills & Utilities", "2021-01-15", "monthly"),
        ("Landscaping", 150.0, "Home & Garden", "2021-04-01", "monthly", "2025-11-01"),
        ("Dog Grooming", 85.0, "Pets", "2021-02-10", "monthly"),
        ("Healthy Paws Pet Insurance - Ticker", 62.0, "Pets", "2021-03-01", "monthly"),
    ]
    for desc, amt, cat, start, freq, *end in manual_expenses:
        end_date = end[0] if end else None
        c.execute(
            "INSERT INTO manual_expenses"
            " (description, amount, category, start_date, end_date, frequency)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (desc, amt, cat, start, end_date, freq),
        )

    # 4. Seed Tax Documents
    for year in range(2021, 2026):
        c.execute(
            "INSERT INTO tax_documents"
            " (tax_year, document_type, issuer, category, amount,"
            " original_file, status, needs_review)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                year,
                "W2",
                "Blue Chip Solutions",
                "Income",
                128500.00 + (year - 2021) * 10000,
                f"W2_{year}_Sterling.pdf",
                "AGENT_VERIFIED",
                0,
            ),
        )
        c.execute(
            "INSERT INTO tax_documents"
            " (tax_year, document_type, issuer, category, amount,"
            " original_file, status, needs_review)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                year,
                "1098",
                "Vault Bank",
                "Mortgage Interest",
                6200.00 - (year - 2021) * 500,
                f"1098_{year}.pdf",
                "AGENT_VERIFIED",
                0,
            ),
        )

    conn.commit()
    _print_seed_summary(conn, db_path)
    conn.close()


def _print_seed_summary(conn, db_path):
    c = conn.cursor()

    c.execute("SELECT MIN(date), MAX(date) FROM transactions")
    min_date, max_date = c.fetchone()

    c.execute("SELECT COUNT(*) FROM trips WHERE type = 'Personal'")
    personal_trips = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trips WHERE type = 'Work'")
    work_trips = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM transactions WHERE status = 'UNVERIFIED'")
    unverified = c.fetchone()[0]

    c.execute(
        "SELECT category, COUNT(*) as n"
        " FROM transactions GROUP BY category ORDER BY n DESC"
    )
    categories = c.fetchall()

    total = sum(n for _, n in categories)

    print(f"\nDemo database seeded: {db_path}")
    print(f"  Date range : {min_date} → {max_date}")
    print(f"  Trips      : {personal_trips} personal, {work_trips} work")
    print(f"  UNVERIFIED : {unverified} transactions (audit demo window)")
    print(f"\n  {'Category':<32} {'Count':>5}")
    print(f"  {'-'*32} {'-'*5}")
    for cat, count in categories:
        print(f"  {cat:<32} {count:>5}")
    print(f"  {'TOTAL':<32} {total:>5}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Seed the housebook demo database."
            " Safe to re-run; skips if DB already exists."
        )
    )
    parser.parse_args()

    setup_workspace()
    # The demo database belongs to the demo workspace. Migrations that
    # read settings (006 scans the Amazon profiles, 008 relativizes
    # paths) must see it, not whatever workspace the caller's shell
    # points at. Settings is not imported yet, so this takes effect.
    os.environ["HOUSEBOOK_WORKSPACE_DIR"] = os.path.abspath(
        DEMO_WORKSPACE
    )
    seed_db()


if __name__ == "__main__":
    main()
