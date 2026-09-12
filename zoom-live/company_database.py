"""Reproducible fictional company data for the voice demo."""
import calendar
import random
import sqlite3
from pathlib import Path

GUIDE = '''# Northstar Analytics — fictional demo company

company.sqlite contains generated data, not real customers or revenue.
Coverage: January–August 2026, complete calendar months. Currency: USD.
For an unspecified sales question, use August 2026 and state that period.
Sales means sum of sales.amount_cents for status='paid', divided by 100.
Refunded invoices do not count as sales. Amounts use integer cents.
customer_months has one row per active customer per month; mrr_cents is recurring revenue.
Monthly logo retention = customers active in both previous and current month /
customers active in previous month. Exclude new customers from the numerator.
Churn = 1 - logo retention. Do not substitute revenue retention for logo retention.

Tables:
- customers(id, name, region, segment)
- sales(id, customer_id, sold_on, amount_cents, status)
- customer_months(customer_id, month, mrr_cents), unique per customer/month

Use sqlite3 -readonly company.sqlite 'SELECT ...' to query. If sqlite3 CLI is
unavailable, use Python sqlite3.connect('file:company.sqlite?mode=ro', uri=True).
Always execute a query for numerical answers. Return the period, metric,
value, currency/unit, and SQL used. Never invent values from memory.
'''


def ensure_database(workspace):
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / 'company.sqlite'
    if not path.exists():
        rng = random.Random(20260912)
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT, segment TEXT);
                CREATE TABLE sales(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                    sold_on TEXT, amount_cents INTEGER, status TEXT);
                CREATE TABLE customer_months(customer_id INTEGER REFERENCES customers(id), month TEXT,
                    mrr_cents INTEGER, PRIMARY KEY(customer_id, month));
            ''')
            invoice = 0
            for customer in range(1, 181):
                db.execute('INSERT INTO customers VALUES(?,?,?,?)', (customer, f'Demo Customer {customer:03}',
                    rng.choice(['North America', 'Europe', 'APAC']), rng.choice(['SMB', 'Mid-market', 'Enterprise'])))
                start = 1 if customer <= 90 else rng.randint(2, 8)
                end = rng.randint(start, 7) if start < 8 and rng.random() < .24 else 8
                price = rng.choice([9900, 29900, 59900, 149900])
                for month in range(start, end + 1):
                    db.execute('INSERT INTO customer_months VALUES(?,?,?)', (customer, f'2026-{month:02}', price))
                    invoice += 1
                    db.execute('INSERT INTO sales VALUES(?,?,?,?,?)', (invoice, customer,
                        f'2026-{month:02}-{rng.randint(1, calendar.monthrange(2026, month)[1]):02}',
                        price, 'refunded' if rng.random() < .025 else 'paid'))
    (workspace / 'DATABASE.md').write_text(GUIDE)
    return path


if __name__ == '__main__':
    print(ensure_database(Path(__file__).resolve().parent / 'codex-workspace'))
