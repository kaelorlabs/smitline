import sqlite3
import tempfile
import unittest
from pathlib import Path
from company_database import ensure_database


class CompanyDatabaseTests(unittest.TestCase):
    def test_seed_is_repeatable_and_metrics_are_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = ensure_database(directory)
            ensure_database(directory)
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 180)
                self.assertEqual(db.execute("SELECT SUM(amount_cents) FROM sales WHERE status='paid' AND sold_on LIKE '2026-08-%'").fetchone()[0], 7856700)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM customer_months a JOIN customer_months b USING(customer_id) WHERE a.month='2026-07' AND b.month='2026-08'").fetchone()[0], 122)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM customer_months WHERE month='2026-07'").fetchone()[0], 132)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
