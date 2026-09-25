import os
import sqlite3
import tempfile
import unittest

from housebook.core.trip_detector import (
    detect_trips,
    extract_location_hints,
)
from housebook.migrations.runner import run_migrations


class TestTripDetector(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        run_migrations(self.db_path, verbose=False)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_tx(self, date, description, amount, category,
                   source="Amex", trip_id=None):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, trip_id, needs_review, profile) "
            "VALUES (?, ?, ?, ?, ?, 'UNVERIFIED', 'test.pdf', ?, 1, 'test_profile')",
            (date, description, amount, category, source, trip_id),
        )
        conn.commit()
        conn.close()

    def test_basic_cluster_detection(self):
        self._insert_tx("2025-11-15", "MARRIOTT BOSTON MA", 250, "Lodging")
        self._insert_tx("2025-11-15", "UBER TRIP MA", 35, "Local Transit")
        self._insert_tx("2025-11-16", "DELTA SHUTTLE", 180, "Flights")
        self._insert_tx("2025-11-17", "HILTON BOSTON MA", 200, "Lodging")
        self._insert_tx("2025-11-17", "LOGAN EXPR PARKING", 45, "Local Transit")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["anchor_count"], 5)
        self.assertEqual(result["candidates"][0]["start_date"], "2025-11-15")
        self.assertEqual(result["candidates"][0]["end_date"], "2025-11-17")

    def test_two_separate_trips(self):
        # Trip 1: November
        for day in (15, 16, 17):
            self._insert_tx(
                f"2025-11-{day}", f"HOTEL DAY{day}", 200, "Lodging",
            )
        # Trip 2: December (30 days later)
        for day in (15, 16, 17):
            self._insert_tx(
                f"2025-12-{day}", f"HOTEL DAY{day}", 200, "Lodging",
            )

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 2)

    def test_min_transactions_filter(self):
        self._insert_tx("2025-11-15", "HOTEL BOSTON", 250, "Lodging")
        self._insert_tx("2025-11-16", "UBER TRIP", 35, "Local Transit")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 0)

    def test_amazon_excluded(self):
        self._insert_tx("2025-11-15", "TRAVEL BAG", 50, "Local Transit", "Amazon")
        self._insert_tx("2025-11-16", "LUGGAGE SET", 120, "Local Transit", "Amazon")
        self._insert_tx("2025-11-17", "PASSPORT HOLDER", 15, "Local Transit", "Amazon")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 0)

    def test_contextual_enrichment(self):
        # 3 anchors
        self._insert_tx("2025-11-15", "MARRIOTT MA", 250, "Lodging")
        self._insert_tx("2025-11-16", "UBER TRIP MA", 35, "Local Transit")
        self._insert_tx("2025-11-17", "HOTEL MA", 200, "Lodging")
        # 2 contextual within window
        self._insert_tx("2025-11-16", "LEGAL SEA FOODS", 85, "Dining & Takeout")
        self._insert_tx("2025-11-16", "MUSEUM TICKETS", 40, "Entertainment")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["anchor_count"], 3)
        self.assertEqual(cand["contextual_count"], 2)
        self.assertEqual(cand["transaction_count"], 5)

    def test_contextual_outside_window(self):
        # 3 anchors in November
        self._insert_tx("2025-11-15", "HOTEL", 250, "Lodging")
        self._insert_tx("2025-11-16", "UBER", 35, "Local Transit")
        self._insert_tx("2025-11-17", "RENTAL CAR", 200, "Local Transit")
        # Dining 10 days before — outside window
        self._insert_tx("2025-11-05", "RESTAURANT", 60, "Dining & Takeout")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        cand = result["candidates"][0]
        self.assertEqual(cand["contextual_count"], 0)
        self.assertEqual(cand["transaction_count"], 3)

    def test_advance_payment_detection(self):
        # Flight booked 60 days before trip
        self._insert_tx("2025-09-15", "DELTA AIR LINES", 600, "Flights")
        # Trip cluster in November
        self._insert_tx("2025-11-15", "MARRIOTT", 250, "Lodging")
        self._insert_tx("2025-11-16", "UBER TRIP", 35, "Local Transit")
        self._insert_tx("2025-11-17", "HOTEL", 200, "Lodging")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["advance_payments"]), 1)
        adv = result["advance_payments"][0]
        self.assertIn("DELTA", adv["description"])
        self.assertEqual(adv["candidate_trip_index"], 0)
        self.assertTrue(30 <= adv["days_before_trip"] <= 180)

    def test_advance_too_far(self):
        # Flight booked 200 days before — outside 180-day window
        self._insert_tx("2025-05-01", "DELTA AIR LINES", 600, "Flights")
        # Trip cluster in November
        self._insert_tx("2025-11-15", "MARRIOTT", 250, "Lodging")
        self._insert_tx("2025-11-16", "UBER TRIP", 35, "Local Transit")
        self._insert_tx("2025-11-17", "HOTEL", 200, "Lodging")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["advance_payments"]), 0)

    def test_location_extraction(self):
        txs = [
            {"description": "MARRIOTT BOSTON MA"},
            {"description": "UBER TRIP MA"},
            {"description": "HILTON GARDEN INN MA"},
        ]
        self.assertEqual(extract_location_hints(txs), "MA")

    def test_location_trip_abroad_with_home_airport_parking(self):
        """One home-state charge does not outvote a trip abroad."""
        txs = [{"description": "TRATTORIA DEL CENTRO ROMA IT"}] * 5
        txs.append({"description": "LOGAN AIRPORT PARKING BOSTON MA"})
        self.assertEqual(extract_location_hints(txs), "IT")

    def test_location_domestic_trip_with_foreign_billed_ride(self):
        """One foreign-billed charge does not make a domestic trip abroad."""
        txs = [{"description": "HARBOR GRILL SAN DIEGO CA"}] * 4
        txs.append({"description": "UBER TRIP NL"})
        self.assertEqual(extract_location_hints(txs), "CA")

    def test_location_one_vote_per_transaction(self):
        """A company suffix before the state is not a second location."""
        txs = [{"description": "ACME CO NY"}] * 3
        self.assertEqual(extract_location_hints(txs), "NY")

    def test_location_ambiguous_code_follows_trip_context(self):
        """IN reads as India on a trip abroad, beating the lone FR."""
        txs = [{"description": "HOTEL CHENNAI IN"}] * 3
        txs.append({"description": "CAFE PARIS FR"})
        self.assertEqual(extract_location_hints(txs), "IN")

    def test_location_no_match(self):
        txs = [
            {"description": "SOME RANDOM MERCHANT"},
            {"description": "ANOTHER STORE 12345"},
        ]
        self.assertIsNone(extract_location_hints(txs))

    def test_work_type_detection(self):
        self._insert_tx("2025-11-15", "EGENCIA TRAVEL", 400, "Work (Reimbursable)")
        self._insert_tx("2025-11-16", "MARRIOTT", 250, "Lodging")
        self._insert_tx("2025-11-17", "UBER", 35, "Local Transit")

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(result["candidates"][0]["type"], "work")

    def test_already_assigned_excluded(self):
        # Create a trip first
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trips (name, start_date, end_date) "
            "VALUES ('Existing Trip', '2025-11-15', '2025-11-17')"
        )
        conn.commit()
        conn.close()

        # Insert TXs already assigned to the trip
        self._insert_tx("2025-11-15", "HOTEL", 250, "Lodging", trip_id=1)
        self._insert_tx("2025-11-16", "UBER", 35, "Local Transit", trip_id=1)
        self._insert_tx("2025-11-17", "RENTAL", 200, "Local Transit", trip_id=1)

        result = detect_trips(self.db_path, months=24, min_transactions=3)
        self.assertEqual(len(result["candidates"]), 0)


if __name__ == "__main__":
    unittest.main()
