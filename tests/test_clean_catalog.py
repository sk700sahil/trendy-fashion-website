"""Data quality and SQLite integrity checks; no third-party packages or services."""
import copy
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.clean_catalog import (
    REQUIRED, ROOT, clean_catalog, cleaned_csv, main, price_to_minor,
    product_seed, synthetic_seed,
)


class CatalogCleaningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "public" / "assets" / "images"
        self.images.mkdir(parents=True)
        (self.images / "shirt.webp").write_bytes(b"image fixture")
        self.source = self.root / "source.csv"
        self.row = {
            "id": " test-shirt ", "name": "  Everyday   Shirt  ",
            "category": " Men’s Clothing ", "description": "An everyday shirt.",
            "price_inr": "₹1,234.50", "image": "/assets/images/SHIRT.webp",
            "alt": "A blue shirt", "sizes": " S | M | m | L ",
            "colors": "Blue|blue", "featured": "yes",
        }

    def write_rows(self, rows, fields=REQUIRED):
        with self.source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def clean(self, *rows):
        self.write_rows(rows or [self.row])
        return clean_catalog(self.source, self.root / "public")

    def test_normalizes_categories_whitespace_case_and_options(self):
        products, report = self.clean()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(products[0]["id"], "test-shirt")
        self.assertEqual(products[0]["name"], "Everyday Shirt")
        self.assertEqual(products[0]["category"], "men")
        self.assertEqual(products[0]["price_minor"], 123450)
        self.assertEqual(products[0]["sizes"], ["S", "M", "L"])
        self.assertEqual(products[0]["colors"], ["Blue"])
        self.assertEqual(products[0]["image"], "/assets/images/shirt.webp")
        self.assertGreater(len(report["normalizations"]), 0)

    def test_prices_are_exact_minor_units(self):
        for price, expected in [("0.01", 1), ("19.99", 1999), ("INR 1,23,456.78", 12345678), ("₹1,000,000.00", 100000000)]:
            with self.subTest(price=price):
                self.assertEqual(price_to_minor(price), expected)

    def test_rejects_invalid_prices(self):
        for price in ("", "0", "-1", "NaN", "Infinity", "1e3", "1.005", "12,34", "1,000.001", "1000000.01", "1 USD", "1 299"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                price_to_minor(price)

    def test_rejects_every_duplicate_id_including_normalized_case(self):
        duplicate = dict(self.row, id="TEST-SHIRT")
        products, report = self.clean(self.row, duplicate)
        self.assertEqual(products, [])
        self.assertEqual(report["rejected_rows"], 2)
        self.assertEqual(len(report["issues"]), 2)
        self.assertIn("duplicate id", report["issues"][0]["errors"][0])

    def test_rejects_missing_images_remote_urls_and_traversal(self):
        for image in ("/assets/images/missing.webp", "https://example.com/shirt.webp", "/assets/images/../shirt.webp", "shirt.webp"):
            with self.subTest(image=image):
                products, report = self.clean(dict(self.row, image=image))
                self.assertEqual(products, [])
                self.assertEqual(report["status"], "failed")

    def test_rejects_actual_filename_case_mismatch_even_on_windows(self):
        (self.images / "other.webp").write_bytes(b"image fixture")
        (self.images / "other.webp").rename(self.images / "OTHER.webp")
        products, report = self.clean(dict(self.row, image="/assets/images/other.webp"))
        self.assertEqual(products, [])
        self.assertIn("case", report["issues"][0]["errors"][0])

    def test_rejects_unknown_category_missing_description_and_bad_featured(self):
        for field, value in (("category", "appliances"), ("description", " "), ("alt", ""), ("name", ""), ("featured", "maybe"), ("id", "bad_id")):
            with self.subTest(field=field):
                products, report = self.clean(dict(self.row, **{field: value}))
                self.assertEqual(products, [])
                self.assertEqual(report["rejected_rows"], 1)

    def test_rejects_missing_header_and_empty_catalog(self):
        self.write_rows([], fields=["id", "name"])
        self.assertEqual(clean_catalog(self.source, self.root / "public")[1]["status"], "failed")
        self.write_rows([])
        self.assertEqual(clean_catalog(self.source, self.root / "public")[1]["status"], "failed")

    def test_rejects_control_characters_before_generating_sql(self):
        for field in ("name", "description", "alt", "sizes"):
            for char in ("\x00", "\x01", "\x7f"):
                with self.subTest(field=field, char=repr(char)):
                    self.write_rows([dict(self.row, **{field: "BadCONTROLvalue"})])
                    content = self.source.read_text(encoding="utf-8").replace("CONTROL", char)
                    self.source.write_text(content, encoding="utf-8")
                    # Python 3.10 rejects NUL while reading CSV; newer versions
                    # reach our row validation. Both must refuse import output.
                    with redirect_stdout(io.StringIO()):
                        result = main(["--input", str(self.source), "--public-root", str(self.root / "public"),
                                       "--output-dir", str(self.root / "output")])
                    self.assertEqual(result, 1)
                    self.assertFalse((self.root / "output" / "catalog_seed.sql").exists())

    def test_failed_run_preserves_previous_valid_seed(self):
        products, _ = self.clean()
        output = self.root / "output"
        output.mkdir()
        old_seed = product_seed(products)
        (output / "catalog_seed.sql").write_text(old_seed, encoding="utf-8")
        self.write_rows([dict(self.row, price_inr="-1")])
        with redirect_stdout(io.StringIO()):
            result = main(["--input", str(self.source), "--public-root", str(self.root / "public"), "--output-dir", str(output)])
        self.assertEqual(result, 1)
        self.assertEqual((output / "catalog_seed.sql").read_text(encoding="utf-8"), old_seed)
        self.assertEqual(json.loads((output / "quality_report.json").read_text())["status"], "failed")

    def test_deterministic_check_detects_stale_output(self):
        self.write_rows([self.row])
        args = ["--input", str(self.source), "--public-root", str(self.root / "public"), "--output-dir", str(self.root / "output")]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args + ["--check"]), 0)
            (self.root / "output" / "catalog_seed.sql").write_text("stale", encoding="utf-8")
            self.assertEqual(main(args + ["--check"]), 1)

    def test_sql_escaping_preserves_quotes_and_blocks_statement_injection(self):
        malicious = "Kid's shirt'); DROP TABLE products; --"
        products, _ = self.clean(dict(self.row, name=malicious))
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript((ROOT / "migrations" / "0001_catalog_and_orders.sql").read_text())
        db.executescript(product_seed(products))
        self.assertEqual(db.execute("SELECT name FROM products").fetchone()[0], malicious)


class RepositoryDataTests(unittest.TestCase):
    def setUp(self):
        self.products, self.report = clean_catalog(ROOT / "data" / "catalog_raw.csv", ROOT / "public")
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript((ROOT / "migrations" / "0001_catalog_and_orders.sql").read_text())

    def seed(self):
        self.db.executescript(product_seed(self.products))
        self.db.executescript(synthetic_seed(self.products))

    def test_committed_catalog_and_outputs_are_valid_and_reproducible(self):
        self.assertEqual(self.report["status"], "passed")
        self.assertEqual(len(self.products), 25)
        self.assertEqual(len(self.report["category_counts"]), 5)
        self.assertEqual(product_seed(self.products), (ROOT / "data" / "catalog_seed.sql").read_text(encoding="utf-8"))
        self.assertEqual(synthetic_seed(self.products), (ROOT / "data" / "synthetic_orders.sql").read_text(encoding="utf-8"))
        self.assertEqual(cleaned_csv(self.products), (ROOT / "data" / "catalog_clean.csv").read_text(encoding="utf-8"))

    def test_repeated_seed_creates_no_duplicates_and_totals_match_lines(self):
        self.seed()
        before = list(self.db.execute("SELECT * FROM orders ORDER BY id"))
        self.seed()
        self.assertEqual(self.db.execute("SELECT count(*) FROM products").fetchone()[0], 25)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 36)
        self.assertEqual(list(self.db.execute("SELECT * FROM orders ORDER BY id")), before)
        mismatches = self.db.execute("SELECT o.id FROM orders o JOIN order_items i ON o.id=i.order_id GROUP BY o.id HAVING o.total_minor != SUM(i.unit_price_minor*i.quantity)").fetchall()
        self.assertEqual(mismatches, [])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        months = self.db.execute("SELECT DISTINCT substr(created_at,1,7) FROM orders ORDER BY 1").fetchall()
        self.assertEqual([m[0] for m in months], [f"2026-{m:02}" for m in range(4, 10)])
        self.assertEqual(self.db.execute("SELECT DISTINCT source FROM orders").fetchall(), [("synthetic",)])

    def test_reimport_updates_catalog_but_preserves_order_price_snapshots(self):
        self.seed()
        snapshots = list(self.db.execute("SELECT * FROM order_items ORDER BY order_id,line_no"))
        changed = copy.deepcopy(self.products)
        changed[0]["price_minor"] += 1000
        self.db.executescript(product_seed(changed))
        self.db.executescript(synthetic_seed(changed))
        self.assertEqual(self.db.execute("SELECT price_minor FROM products WHERE id=?", (changed[0]["id"],)).fetchone()[0], changed[0]["price_minor"])
        self.assertEqual(list(self.db.execute("SELECT * FROM order_items ORDER BY order_id,line_no")), snapshots)

    def test_schema_enforces_foreign_keys_quantity_and_price_constraints(self):
        self.seed()
        row = self.db.execute("SELECT order_id,line_no FROM order_items LIMIT 1").fetchone()
        for quantity in (0, -1, 11, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("UPDATE order_items SET quantity=? WHERE order_id=? AND line_no=?", (quantity, *row))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE order_items SET product_id='does-not-exist'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE products SET price_minor=1.5")


if __name__ == "__main__":
    unittest.main()
