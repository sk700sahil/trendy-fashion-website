"""Contract tests for imported retailer rows and verified-price checkout gating."""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
from scripts.import_source_catalog import main, validate_and_map
from src.store import APIError, create_order, get_product, list_products


class CatalogDB:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted((ROOT / "migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))
        for filename in ("catalog_seed.sql", "source_products_seed.sql", "synthetic_orders.sql"):
            self.connection.executescript((ROOT / "data" / filename).read_text(encoding="utf-8"))

    async def all(self, sql, values=()):
        return [dict(row) for row in self.connection.execute(sql, values).fetchall()]

    async def batch(self, statements):
        results = []
        with self.connection:
            for sql, values in statements:
                results.append([dict(row) for row in self.connection.execute(sql, values).fetchall()])
        return results


class SourceImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = CatalogDB()

    def tearDown(self):
        self.db.connection.close()

    def test_committed_import_preserves_all_rows_and_category_counts(self):
        rows = self.db.connection.execute(
            "SELECT category,COUNT(*) FROM catalog_products GROUP BY category ORDER BY category"
        ).fetchall()
        self.assertEqual(dict(rows), {"accessories": 14, "footwear": 15, "kids": 16, "men": 21, "women": 20})
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 42)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM source_products").fetchone()[0], 44)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 36)
        self.assertEqual(self.db.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_source_seed_is_repeatable_and_generated_report_is_current(self):
        before = self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.db.connection.executescript((ROOT / "data" / "source_products_seed.sql").read_text(encoding="utf-8"))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM catalog_products").fetchone()[0], 86)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM source_products").fetchone()[0], 44)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], before)
        report = json.loads((ROOT / "data" / "source_import_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["imported_source_rows"], 61)
        self.assertEqual(report["source_priced_and_orderable"], 17)
        self.assertEqual(report["source_visible_price_unavailable"], 44)
        self.assertEqual(main(["--check"]), 0)

    async def test_catalog_search_detail_and_price_sort_include_unpriced_items(self):
        result = await list_products(self.db, {"q": "HIGHLANDER"})
        self.assertEqual([item["id"] for item in result["products"]], ["source-men-001"])
        self.assertEqual(result["products"][0]["price_minor"], 48500)
        self.assertEqual(result["products"][0]["brand"], "HIGHLANDER")
        detail = await get_product(self.db, "source-men-002")
        self.assertIsNone(detail["product"]["price_minor"])
        self.assertEqual(detail["product"]["image"], "/assets/images/product-placeholder.svg")
        asc = await list_products(self.db, {"sort": "price_asc"})
        self.assertTrue(all(item["price_minor"] is not None for item in asc["products"][:-44]))
        self.assertTrue(all(item["price_minor"] is None for item in asc["products"][-44:]))
        filtered = await list_products(self.db, {"min_price": "10000"})
        self.assertTrue(all(item["price_minor"] is not None for item in filtered["products"]))

    async def test_verified_imported_price_can_order_but_unknown_price_cannot(self):
        result, status = await create_order(
            self.db,
            {"items": [{"product_id": "source-men-001", "quantity": 1, "size": "39", "color": "Grey & black tartan checks"}]},
            str(uuid4()),
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["order"]["total_minor"], 48500)
        with self.assertRaises(APIError) as caught:
            await create_order(self.db, {"items": [{"product_id": "source-men-002", "quantity": 1}]}, str(uuid4()))
        self.assertEqual(caught.exception.code, "invalid_product")
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders WHERE source='visitor'").fetchone()[0], 1)

    def test_duplicate_url_fails_instead_of_silently_dropping_a_row(self):
        payload = json.loads((ROOT / "data" / "trendy_threads_products_source.json").read_text(encoding="utf-8"))
        payload["products"][1]["canonical_url"] = payload["products"][0]["canonical_url"]
        with self.assertRaises(ValueError):
            validate_and_map(payload, [{"id": "existing", "name": "Existing"}], {"checks": []})


if __name__ == "__main__":
    unittest.main()
