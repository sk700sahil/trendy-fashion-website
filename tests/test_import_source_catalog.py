"""Contract tests for imported retailer rows and verified-price checkout gating."""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
from scripts.import_source_catalog import main, normalize_url, seed_sql, validate_and_map
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

    def test_committed_strict_import_keeps_only_complete_verified_products(self):
        rows = self.db.connection.execute(
            "SELECT category,COUNT(*) FROM catalog_products GROUP BY category ORDER BY category"
        ).fetchall()
        self.assertEqual(sum(dict(rows).values()), 58)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 58)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM source_products").fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM retired_products").fetchone()[0], 28)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM products WHERE id NOT LIKE 'source-%'").fetchone()[0], 25)
        total, unique_ids = self.db.connection.execute("SELECT COUNT(*),COUNT(DISTINCT id) FROM catalog_products").fetchone()
        self.assertEqual((total, unique_ids), (58, 58))
        invalid_price_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM products WHERE typeof(price_minor) != 'integer' OR price_minor <= 0"
        ).fetchone()[0]
        self.assertEqual(invalid_price_count, 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 36)
        self.assertEqual(self.db.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_source_seed_is_repeatable_and_generated_report_is_current(self):
        before = self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.db.connection.executescript((ROOT / "data" / "source_products_seed.sql").read_text(encoding="utf-8"))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM catalog_products").fetchone()[0], 58)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM source_products").fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM retired_products").fetchone()[0], 28)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], before)
        report = json.loads((ROOT / "data" / "source_import_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["urls_received"], 61)
        self.assertEqual(report["already_existed"], 33)
        self.assertEqual(report["rejected"], 28)
        self.assertEqual(report["final_catalog_count"], 58)
        self.assertEqual(main(["--check"]), 0)

    async def test_rejected_imports_are_absent_from_search_filters_and_detail(self):
        result = await list_products(self.db, {"q": "HIGHLANDER"})
        self.assertEqual(result["products"], [])
        with self.assertRaises(APIError) as caught:
            await get_product(self.db, "source-men-001")
        self.assertEqual(caught.exception.status, 404)
        footwear = await list_products(self.db, {"category": "footwear"})
        self.assertNotIn("source-footwear-004", [item["id"] for item in footwear["products"]])
        asc = await list_products(self.db, {"sort": "price_asc"})
        self.assertTrue(all(item["price_minor"] is not None for item in asc["products"]))
        filtered = await list_products(self.db, {"min_price": "10000"})
        self.assertTrue(all(item["price_minor"] is not None for item in filtered["products"]))

    async def test_verified_imported_price_can_order_but_unknown_price_cannot(self):
        priced = next(product for product in (await list_products(self.db, {}))["products"]
                      if product["id"].startswith("source-") and product["price_minor"] is not None)
        result, status = await create_order(
            self.db,
            {"items": [{"product_id": priced["id"], "quantity": 1,
                        "size": (priced["sizes"] or [""])[0], "color": (priced["colors"] or [""])[0]}]},
            str(uuid4()),
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["order"]["total_minor"], priced["price_minor"])
        retired = [row[0] for row in self.db.connection.execute("SELECT product_id FROM retired_products")]
        for product_id in retired:
            with self.subTest(product_id=product_id), self.assertRaises(APIError) as caught:
                await create_order(self.db, {"items": [{"product_id": product_id, "quantity": 1}]}, str(uuid4()))
            self.assertEqual(caught.exception.code, "invalid_product")
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM orders WHERE source='visitor'").fetchone()[0], 1)

    async def test_retired_product_keeps_history_but_disappears_from_catalog_and_checkout(self):
        product_id = "source-legacy-bad"
        self.db.connection.execute(
            "INSERT INTO products SELECT ?,name,category,description,48500,image,alt,sizes,colors,featured,brand,subcategory,mrp_minor,discount_percent,material,fit,rating_value,rating_scale,rating_count,NULL,NULL,NULL,verification_status,missing_fields FROM products WHERE id='source-men-002'",
            (product_id,),
        )
        self.db.connection.execute(
            "INSERT INTO orders (id,idempotency_key,request_hash,source,total_minor,created_at) VALUES ('old-order','old-key','old-hash','visitor',48500,'2026-01-01T00:00:00Z')"
        )
        self.db.connection.execute(
            "INSERT INTO order_items (order_id,line_no,product_id,product_name,category,unit_price_minor,quantity,size,color) VALUES ('old-order',1,?,'HIGHLANDER Regular Fit Shirt','men',48500,1,'39','Grey & black tartan checks')",
            (product_id,),
        )
        self.db.connection.commit()
        self.db.connection.executescript(seed_sql(list(json.loads((ROOT / "data" / "active_import_ledger.json").read_text(encoding="utf-8"))["products"]), [
            {"product_id": product_id, "title": "Old Shirt", "source_url": "https://example.com/p/old", "failure_reason": "DETAILS_INSUFFICIENT", "failure_reasons": ["DETAILS_INSUFFICIENT"]}
        ]))
        row = self.db.connection.execute("SELECT id FROM catalog_products WHERE id=?", (product_id,)).fetchone()
        self.assertIsNone(row)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM products WHERE id=?", (product_id,)).fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute("SELECT total_minor FROM orders WHERE id='old-order'").fetchone()[0], 48500)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM order_items WHERE order_id='old-order'").fetchone()[0], 1)
        with self.assertRaises(APIError) as caught:
            await create_order(self.db, {"items": [{"product_id": product_id, "quantity": 1}]}, str(uuid4()))
        self.assertEqual(caught.exception.code, "invalid_product")

    def test_verified_image_hosts_are_https_allowlisted_and_seed_is_idempotent(self):
        before = self.db.connection.execute("SELECT image FROM products WHERE image LIKE 'https://%' LIMIT 1").fetchone()[0]
        self.assertTrue(before.startswith("https://"))
        self.db.connection.executescript((ROOT / "data" / "source_products_seed.sql").read_text(encoding="utf-8"))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM catalog_products").fetchone()[0], 58)
        self.assertEqual(self.db.connection.execute("SELECT image FROM products WHERE image LIKE 'https://%' LIMIT 1").fetchone()[0], before)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM catalog_products WHERE image='/assets/images/product-placeholder.svg'").fetchone()[0], 0)

    def _candidate_fixture(self):
        payload = json.loads((ROOT / "data" / "trendy_threads_products_source.json").read_text(encoding="utf-8"))
        item = next(row for row in payload["products"] if row["id"] == "MEN-002")
        page = next(row for row in json.loads((ROOT / "data" / "source_page_checks.json").read_text(encoding="utf-8"))["checks"] if row["id"] == "MEN-002")
        image = next(row for row in json.loads((ROOT / "data" / "source_image_checks.json").read_text(encoding="utf-8"))["checks"] if row["product_id"] == "MEN-002")
        return {"products": [json.loads(json.dumps(item))]}, {"checks": [json.loads(json.dumps(page))]}, {"checks": [json.loads(json.dumps(image))]}

    def test_complete_product_is_accepted_only_with_source_and_image_evidence(self):
        payload, pages, images = self._candidate_fixture()
        mapped, report = validate_and_map(payload, [], pages, images)
        self.assertEqual(len(mapped), 1)
        self.assertEqual(report["products"][0]["status"], "ACCEPTED")
        self.assertTrue(report["products"][0]["validation"]["meaningful_source_verified_details"])

    def test_missing_or_broken_image_is_rejected(self):
        payload, pages, images = self._candidate_fixture()
        payload["products"][0]["image_urls"] = []
        images["checks"] = []
        mapped, report = validate_and_map(payload, [], pages, images)
        self.assertEqual(mapped, [])
        self.assertIn("IMAGE_MISSING", report["products"][0]["failure_reasons"])
        payload, pages, images = self._candidate_fixture()
        images["checks"][0].update(status="rejected", http_status=404, content_type="text/html", rendered=False)
        mapped, report = validate_and_map(payload, [], pages, images)
        self.assertEqual(mapped, [])
        self.assertIn("IMAGE_BROKEN", report["products"][0]["failure_reasons"])

    def test_missing_price_or_meaningful_details_is_rejected(self):
        payload, pages, images = self._candidate_fixture()
        payload["products"][0]["price_inr"] = None
        pages["checks"][0]["verified_fields"].remove("price")
        self.assertEqual(validate_and_map(payload, [], pages, images)[0], [])
        payload, pages, images = self._candidate_fixture()
        row = payload["products"][0]
        row["description"] = None
        row["material"] = row["fit"] = row["color"] = row["sizes"] = row["subcategory"] = None
        pages["checks"][0]["verified_fields"] = ["image", "price"]
        mapped, report = validate_and_map(payload, [], pages, images)
        self.assertEqual(mapped, [])
        self.assertIn("DESCRIPTION_MISSING", report["products"][0]["failure_reasons"])

    def test_inaccessible_and_mismatched_sources_are_rejected(self):
        payload, pages, images = self._candidate_fixture()
        pages["checks"][0].update(status="inaccessible", http_status=None, identity_match=False)
        self.assertIn("SOURCE_INACCESSIBLE", validate_and_map(payload, [], pages, images)[1]["products"][0]["failure_reasons"])
        payload, pages, images = self._candidate_fixture()
        pages["checks"][0].update(status="redirected_mismatch", identity_match=False)
        self.assertIn("REDIRECTED_TO_DIFFERENT_PRODUCT", validate_and_map(payload, [], pages, images)[1]["products"][0]["failure_reasons"])

    def test_tracking_parameter_urls_are_duplicates_and_second_run_is_existing(self):
        payload, pages, images = self._candidate_fixture()
        duplicate = json.loads(json.dumps(payload["products"][0]))
        duplicate["id"] = "MEN-002-DUPLICATE"
        duplicate["canonical_url"] += "?utm_source=feed"
        duplicate_page = json.loads(json.dumps(pages["checks"][0])); duplicate_page["id"] = duplicate["id"]; duplicate_page["source_url"] = duplicate["canonical_url"]
        duplicate_image = json.loads(json.dumps(images["checks"][0])); duplicate_image["product_id"] = duplicate["id"]; duplicate_image["source_url"] = duplicate["canonical_url"]
        payload["products"].append(duplicate); pages["checks"].append(duplicate_page); images["checks"].append(duplicate_image)
        mapped, report = validate_and_map(payload, [], pages, images)
        self.assertEqual(len(mapped), 1)
        self.assertEqual(report["products"][1]["status"], "DUPLICATE")
        self.assertEqual(report["products"][1]["duplicate_of"], mapped[0]["id"])
        self.assertEqual(normalize_url(payload["products"][0]["canonical_url"]), normalize_url(duplicate["canonical_url"]))
        _, second = validate_and_map({"products": [payload["products"][0]]}, mapped, {"checks": [pages["checks"][0]]}, {"checks": [images["checks"][0]]})
        self.assertEqual(second["products"][0]["status"], "EXISTING")

    def test_rejected_product_seed_never_activates_it(self):
        sql = seed_sql([], [{"product_id": "source-bad", "title": "Bad", "source_url": "https://example.com/bad", "failure_reason": "IMAGE_MISSING", "failure_reasons": ["IMAGE_MISSING"]}])
        self.assertNotIn("INSERT INTO products", sql)
        self.assertIn("INSERT INTO retired_products", sql)

    def test_redirected_different_asin_is_rejected(self):
        payload = json.loads((ROOT / "data" / "trendy_threads_products_source.json").read_text(encoding="utf-8"))
        product = next(item for item in payload["products"] if item["id"] == "FOOTWEAR-004")
        product["price_inr"] = 100
        checks = json.loads((ROOT / "data" / "source_page_checks.json").read_text(encoding="utf-8"))
        images = json.loads((ROOT / "data" / "source_image_checks.json").read_text(encoding="utf-8"))
        result = validate_and_map({"products": [product]}, [], checks, images)[1]["products"][0]
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("REDIRECTED_TO_DIFFERENT_PRODUCT", result["failure_reasons"])


if __name__ == "__main__":
    unittest.main()
