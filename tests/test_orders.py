"""SQL-backed contract tests. Run: python -m unittest discover -s tests -v."""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from store import APIError, analytics, create_order, get_product, list_products


class SQLiteDB:
    """Execute exactly the application's SQL with the production migration."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted((ROOT / "migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))
        rows = [
            ("test-shirt", "Cotton shirt", "men", "Relaxed cotton shirt.", 129900, "/shirt.webp", "Cotton shirt", '["S","M","L"]', '["Blue","White"]', 1),
            ("test-bag", "Everyday tote", "accessories", "A roomy tote for daily use.", 54900, "/tote.webp", "Everyday tote", '[]', '["Natural"]', 0),
            ("test-knit", "Soft knit", "men", "Soft cotton knit.", 89900, "/knit.webp", "Soft knit", '["S","M"]', '["Navy"]', 0),
        ]
        self.connection.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self.connection.commit()

    async def all(self, sql, values=()):
        return [dict(row) for row in self.connection.execute(sql, values).fetchall()]

    async def batch(self, statements):
        results = []
        with self.connection:
            for sql, values in statements:
                results.append([dict(row) for row in self.connection.execute(sql, values).fetchall()])
        return results


def cart(quantity=1):
    return {"items": [{"product_id": "test-shirt", "quantity": quantity, "size": "M", "color": "Blue"}]}


class OrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SQLiteDB()

    def tearDown(self):
        self.db.connection.close()

    async def assert_error(self, body, code="invalid_request", key=None):
        with self.assertRaises(APIError) as caught:
            await create_order(self.db, body, key or str(uuid4()))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((await self.db.all("SELECT COUNT(*) AS n FROM orders"))[0]["n"], 0)
        self.assertEqual((await self.db.all("SELECT COUNT(*) AS n FROM order_items"))[0]["n"], 0)

    async def test_order_uses_database_prices_and_snapshots(self):
        payload = cart(2)
        payload["items"].append({"product_id": "test-bag", "quantity": 3, "color": "Natural"})
        result, status = await create_order(self.db, payload, str(uuid4()))
        self.assertEqual(status, 201)
        self.assertFalse(result["replayed"])
        order = result["order"]
        self.assertEqual(order["total_minor"], 2 * 129900 + 3 * 54900)
        self.assertEqual(order["source"], "visitor")
        self.assertEqual(order["currency"], "INR")
        stored = await self.db.all("SELECT SUM(unit_price_minor*quantity) AS total, COUNT(*) AS n FROM order_items")
        self.assertEqual(stored[0], {"total": order["total_minor"], "n": 2})
        self.assertEqual({line["product_name"] for line in order["items"]}, {"Cotton shirt", "Everyday tote"})

    async def test_invalid_product_cannot_create_partial_order(self):
        payload = cart()
        payload["items"].append({"product_id": "does-not-exist", "quantity": 1})
        await self.assert_error(payload, "invalid_product")

    async def test_rejects_invalid_quantities_and_boolean(self):
        for quantity in (0, -1, 11, 1.5, "2", True, None):
            with self.subTest(quantity=quantity):
                await self.assert_error(cart(quantity))

    async def test_rejects_unknown_or_missing_variants(self):
        for field, value in (("size", "XXXL"), ("size", ""), ("color", "Red"), ("color", "")):
            with self.subTest(field=field, value=value):
                payload = cart()
                payload["items"][0][field] = value
                await self.assert_error(payload, "invalid_variant")
        await self.assert_error({"items": [{"product_id": "test-bag", "quantity": 1, "size": "M", "color": "Natural"}]}, "invalid_variant")

    async def test_rejects_prices_customer_details_and_source_override(self):
        for field in ("price_minor", "total_minor", "email", "source"):
            with self.subTest(field=field):
                payload = cart()
                payload["items"][0][field] = 1
                await self.assert_error(payload)
        await self.assert_error({**cart(), "source": "synthetic"})
        await self.assert_error({**cart(), "email": "visitor@example.com"})

    async def test_malformed_and_oversized_orders(self):
        for payload in (None, [], {}, {"items": []}, {"items": {}}, {"items": [None]}, {"items": [{}]}, {"items": cart()["items"] * 21}):
            with self.subTest(payload=payload):
                await self.assert_error(payload)
        payload = cart(6)
        payload["items"] *= 2
        await self.assert_error(payload)
        # Six valid distinct variants still exceed the whole-cart 50-unit limit.
        payload = {"items": [{"product_id": "test-shirt", "quantity": 10, "size": size, "color": color}
                             for size in ("S", "M", "L") for color in ("Blue", "White")]}
        await self.assert_error(payload)

    async def test_requires_valid_idempotency_key(self):
        for key in ("", "not-a-uuid", "x" * 36):
            with self.subTest(key=key), self.assertRaises(APIError) as caught:
                await create_order(self.db, cart(), key)
            self.assertEqual(caught.exception.code, "invalid_request")

    async def test_identical_retry_replays_original_order_after_catalog_changes(self):
        key = str(uuid4())
        original, status = await create_order(self.db, cart(2), key)
        self.db.connection.execute("UPDATE products SET price_minor=999999,name='Changed product',sizes='[]'")
        self.db.connection.commit()
        replay, status = await create_order(self.db, cart(2), key)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["order"], original["order"])
        self.assertEqual(len(await self.db.all("SELECT * FROM orders")), 1)
        self.assertEqual(len(await self.db.all("SELECT * FROM order_items")), 1)

    async def test_reordered_cart_and_split_lines_share_fingerprint(self):
        key = str(uuid4())
        payload = cart(2)
        payload["items"].append({"product_id": "test-bag", "quantity": 1, "color": "Natural"})
        original, _ = await create_order(self.db, payload, key)
        retry = {"items": [payload["items"][1], *cart()["items"], *cart()["items"]]}
        replay, status = await create_order(self.db, retry, key.upper())
        self.assertEqual(status, 200)
        self.assertEqual(replay["order"], original["order"])

    async def test_key_reused_with_changed_cart_is_conflict(self):
        key = str(uuid4())
        original, _ = await create_order(self.db, cart(), key)
        with self.assertRaises(APIError) as caught:
            await create_order(self.db, cart(2), key)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(len(await self.db.all("SELECT * FROM orders")), 1)
        self.assertEqual((await self.db.all("SELECT total_minor FROM orders"))[0]["total_minor"], original["order"]["total_minor"])

    async def test_statement_failure_rolls_back_header_and_items(self):
        real_batch = self.db.batch

        async def fail_late(statements):
            statements = [*statements, ("INSERT INTO order_items (order_id,line_no) VALUES ('absent',99)", [])]
            return await real_batch(statements)

        self.db.batch = fail_late
        with self.assertRaises(sqlite3.IntegrityError):
            await create_order(self.db, cart(), str(uuid4()))
        self.assertEqual(await self.db.all("SELECT * FROM orders"), [])
        self.assertEqual(await self.db.all("SELECT * FROM order_items"), [])

    async def race(self, payloads):
        """Force both requests to see an absent key before either starts its batch."""
        real_all = self.db.all
        arrived = 0
        both_read = asyncio.Event()

        async def simultaneous_read(sql, values=()):
            nonlocal arrived
            rows = await real_all(sql, values)
            if sql.startswith("SELECT id,request_hash") and arrived < 2:
                arrived += 1
                if arrived == 2:
                    both_read.set()
                await both_read.wait()
            return rows

        self.db.all = simultaneous_read
        key = str(uuid4())
        return await asyncio.gather(*(create_order(self.db, payload, key) for payload in payloads), return_exceptions=True)

    async def test_concurrent_duplicate_loser_replays_atomic_winner(self):
        results = await self.race([cart(2), cart(2)])
        self.assertEqual(sorted(result[1] for result in results), [200, 201])
        self.assertEqual(results[0][0]["order"], results[1][0]["order"])
        self.assertEqual(len(await self.db.all("SELECT * FROM orders")), 1)
        self.assertEqual(len(await self.db.all("SELECT * FROM order_items")), 1)

    async def test_concurrent_different_cart_loser_conflicts(self):
        results = await self.race([cart(), cart(2)])
        errors = [result for result in results if isinstance(result, APIError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "idempotency_conflict")
        self.assertEqual(len(await self.db.all("SELECT * FROM orders")), 1)
        self.assertEqual(len(await self.db.all("SELECT * FROM order_items")), 1)

    async def test_analytics_filters_and_never_double_counts_order_total(self):
        seeded, _ = await create_order(self.db, cart(2), str(uuid4()))
        self.db.connection.execute("UPDATE orders SET source='synthetic',created_at='2026-01-15T12:00:00Z' WHERE id=?", [seeded["order"]["id"]])
        self.db.connection.commit()
        payload = cart(3)
        payload["items"].append({"product_id": "test-bag", "quantity": 2, "color": "Natural"})
        visitor, _ = await create_order(self.db, payload, str(uuid4()))
        total = seeded["order"]["total_minor"] + visitor["order"]["total_minor"]
        combined = await analytics(self.db)
        self.assertEqual(combined["summary"]["orders"], 2)
        self.assertEqual(combined["summary"]["units"], 7)
        self.assertEqual(combined["summary"]["revenue_minor"], total)
        self.assertEqual(combined["source_counts"], {"synthetic": 1, "visitor": 1})
        self.assertEqual(sum(row["revenue_minor"] for row in combined["monthly"]), total)
        self.assertEqual(sum(row["revenue_minor"] for row in combined["categories"]), total)
        self.assertEqual(combined["popular"][0]["product_id"], "test-shirt")
        self.assertEqual(combined["popular"][0]["units"], 5)
        for source, expected, units in (("synthetic", seeded, 2), ("visitor", visitor, 5)):
            report = await analytics(self.db, source)
            self.assertEqual(report["summary"]["orders"], 1)
            self.assertEqual(report["summary"]["units"], units)
            self.assertEqual(report["summary"]["revenue_minor"], expected["order"]["total_minor"])
            self.assertEqual(report["source_counts"], {"synthetic": 1, "visitor": 1})

    async def test_empty_analytics_and_invalid_source(self):
        report = await analytics(self.db)
        self.assertEqual(report["summary"], {"orders": 0, "units": 0, "revenue_minor": 0, "average_order_minor": 0})
        self.assertEqual(report["source_counts"], {"synthetic": 0, "visitor": 0})
        self.assertEqual(report["monthly"], [])
        with self.assertRaises(APIError):
            await analytics(self.db, "real")

    async def test_catalog_search_filters_sort_and_product_detail(self):
        result = await list_products(self.db, {"category": "men", "q": "cotton", "min_price": "80000", "max_price": "150000", "sort": "price_asc"})
        self.assertEqual([product["id"] for product in result["products"]], ["test-knit", "test-shirt"])
        self.assertEqual(result["total"], 2)
        self.assertIsInstance(result["products"][0]["sizes"], list)
        self.assertIsInstance(result["products"][0]["featured"], bool)
        product = await get_product(self.db, "test-shirt")
        self.assertEqual([row["id"] for row in product["related"]], ["test-knit"])
        with self.assertRaises(APIError) as caught:
            await get_product(self.db, "absent")
        self.assertEqual(caught.exception.status, 404)

    async def test_sql_metacharacters_are_literal_and_bad_filters_fail(self):
        for query in ("%", "_", "' OR 1=1 --", "\\"):
            result = await list_products(self.db, {"q": query})
            self.assertEqual(result["total"], 0)
        for filters in ({"sort": "price; DROP TABLE products"}, {"min_price": "1.5"}, {"max_price": "-1"}, {"category": "bad"}, {"min_price": "200", "max_price": "100"}):
            with self.subTest(filters=filters), self.assertRaises(APIError):
                await list_products(self.db, filters)
        self.assertEqual(len(await self.db.all("SELECT * FROM products")), 3)


if __name__ == "__main__":
    unittest.main()
