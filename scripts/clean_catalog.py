"""Validate the curated CSV and build deterministic, repeatable D1 seed files.

Only the Python standard library is required. Prices in the source CSV are INR;
the cleaned CSV, SQL, and database use integer paise. Run from any directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {"men", "women", "kids", "accessories", "footwear"}
ALIASES = {
    "men's clothing": "men", "mens": "men", "men's": "men",
    "women's clothing": "women", "womens": "women", "women's": "women",
    "kids wear": "kids", "children": "kids", "kids'": "kids",
    "accessory": "accessories", "shoes": "footwear",
}
REQUIRED = ("id", "name", "category", "description", "price_inr", "image", "alt", "sizes", "colors", "featured")
FIELDS = ("id", "name", "category", "description", "price_minor", "image", "alt", "sizes", "colors", "featured")
MAX_PRICE_MINOR = 100_000_000


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def price_to_minor(value: str) -> int:
    value = value.strip()
    value = re.sub(r"^(?:INR\s*|₹\s*)", "", value, flags=re.IGNORECASE)
    # Accept ungrouped, western grouping, or Indian grouping. Reject exponent
    # notation, malformed separators, negatives and sub-paise precision.
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d{1,2})?", value):
        raise ValueError("price must be a positive INR amount with at most two decimal places")
    amount = int(Decimal(value.replace(",", "")) * 100)
    if not 0 < amount <= MAX_PRICE_MINOR:
        raise ValueError("price must be between INR 0.01 and INR 1000000.00")
    return amount


def options(value: str, field: str) -> list[str]:
    result = []
    seen = set()
    for part in value.split("|"):
        label = normalized_text(part)
        if not label:
            continue
        if len(label) > 40 or any(ord(c) < 32 for c in part.strip()):
            raise ValueError(f"{field} contains an invalid option")
        if label.casefold() not in seen:
            seen.add(label.casefold())
            result.append(label)
    if len(result) > 20:
        raise ValueError(f"{field} must contain at most 20 options")
    return result


def clean_catalog(source: Path, public_root: Path) -> tuple[list[dict], dict]:
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        missing = sorted(set(REQUIRED) - set(headers))
        rows = list(reader)
    report = {
        "schema_version": 1,
        "source": source.name,
        "input_rows": len(rows),
        "accepted_rows": 0,
        "rejected_rows": 0,
        "status": "passed",
        "currency": "INR",
        "money_storage": "integer paise",
        "category_counts": {},
        "normalizations": [],
        "issues": [],
    }
    if missing or len(headers) != len(set(headers)):
        report["status"] = "failed"
        report["rejected_rows"] = len(rows)
        report["issues"].append({"row": 1, "id": "", "errors": ["missing columns: " + ", ".join(missing)] if missing else ["duplicate CSV column names"]})
        return [], report
    identifiers = Counter((row.get("id") or "").strip().lower() for row in rows)
    accepted = []
    image_root = (public_root / "assets" / "images").resolve()
    image_names = {path.name for path in image_root.iterdir() if path.is_file()} if image_root.is_dir() else set()
    for line, raw in enumerate(rows, 2):
        problems = []
        for field in REQUIRED:
            if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in (raw.get(field) or "")):
                problems.append(f"{field} contains an unsupported control character")
        row = {field: normalized_text(raw.get(field) or "") for field in REQUIRED}
        if None in raw or any(raw.get(field) is None for field in REQUIRED):
            problems.append("row has the wrong number of CSV fields")
        product_id = row["id"].lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", product_id) or not 3 <= len(product_id) <= 64:
            problems.append("id must be 3-64 lowercase letters, digits or single hyphens")
        if identifiers[product_id] > 1:
            problems.append("duplicate id; all rows with this id are rejected")
        category = row["category"].lower().replace("’", "'")
        category = ALIASES.get(category, category)
        if category not in CATEGORIES:
            problems.append("category is not one of the five supported categories")
        for field, limit in (("name", 120), ("description", 2000), ("alt", 300)):
            if not row[field] or len(row[field]) > limit:
                problems.append(f"{field} must contain 1-{limit} characters")
        try:
            price = price_to_minor(row["price_inr"])
        except ValueError as exc:
            problems.append(str(exc))
            price = 0
        image_path = row["image"].lower()
        if not re.fullmatch(r"/assets/images/[a-z0-9][a-z0-9_.-]*\.(?:jpg|jpeg|png|webp|avif)", image_path):
            problems.append("image must be a local /assets/images/ filename")
        elif Path(image_path).name not in image_names:
            problems.append("image file is missing (filename case must match exactly)")
        elif not (image_root / Path(image_path).name).resolve().is_relative_to(image_root):
            problems.append("image resolves outside the image directory")
        parsed_options = {}
        for field in ("sizes", "colors"):
            try:
                parsed_options[field] = options(row[field], field)
            except ValueError as exc:
                problems.append(str(exc))
                parsed_options[field] = []
        featured_text = row["featured"].lower()
        if featured_text not in ("0", "1", "true", "false", "yes", "no"):
            problems.append("featured must be 0/1, true/false or yes/no")
        if problems:
            report["issues"].append({"row": line, "id": product_id, "errors": problems})
            continue
        product = {
            "id": product_id, "name": row["name"], "category": category,
            "description": row["description"], "price_minor": price,
            "image": image_path, "alt": row["alt"], **parsed_options,
            "featured": int(featured_text in ("1", "true", "yes")),
        }
        for field in REQUIRED:
            cleaned = str(price) if field == "price_inr" else product.get(field)
            if field in ("sizes", "colors"):
                cleaned = "|".join(product[field])
            if str(cleaned) != (raw.get(field) or ""):
                report["normalizations"].append({"row": line, "id": product_id, "field": field, "result": cleaned})
        accepted.append(product)
    accepted.sort(key=lambda product: product["id"])
    report["accepted_rows"] = len(accepted)
    report["rejected_rows"] = len(rows) - len(accepted)
    report["category_counts"] = dict(sorted(Counter(p["category"] for p in accepted).items()))
    if not rows:
        report["issues"].append({"row": 1, "id": "", "errors": ["catalog must contain at least one product"]})
    if report["issues"]:
        report["status"] = "failed"
    return accepted, report


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "'" + str(value).replace("'", "''") + "'"


def product_seed(products: list[dict]) -> str:
    lines = ["-- Generated by scripts/clean_catalog.py. INR stored as integer paise.",
             "-- Stable IDs and UPSERT make repeated imports safe. No products are deleted."]
    for product in products:
        lines.append("INSERT INTO products (" + ", ".join(FIELDS) + ") VALUES (" +
                     ", ".join(sql_literal(product[field]) for field in FIELDS) +
                     ") ON CONFLICT(id) DO UPDATE SET " +
                     ", ".join(f"{field}=excluded.{field}" for field in FIELDS if field != "id") + ";")
    return "\n".join(lines) + "\n"


def synthetic_seed(products: list[dict]) -> str:
    """36 fixed, synthetic orders across April-September 2026; no random state."""
    lines = ["-- SYNTHETIC portfolio fixtures: no customers, payment, or delivery.",
             "-- Fixed dates and IDs; reruns preserve historical price snapshots."]
    for month, count in enumerate((4, 5, 6, 6, 7, 8), 4):
        for number in range(1, count + 1):
            order_id = f"demo-2026{month:02}-{number:03}"
            items = []
            for offset in range(1 + number % 3):
                product = products[(month * 7 + number * 3 + offset * 5) % len(products)]
                quantity = 1 + (month + number + offset) % 3
                items.append({"product": product, "quantity": quantity})
            total = sum(item["product"]["price_minor"] * item["quantity"] for item in items)
            snapshot = json.dumps(items, sort_keys=True, separators=(",", ":"))
            request_hash = hashlib.sha256(snapshot.encode()).hexdigest()
            created = f"2026-{month:02}-{min(number * 3, 24):02}T12:00:00Z"
            lines.append("INSERT INTO orders (id, idempotency_key, request_hash, source, total_minor, created_at) VALUES (" +
                         ", ".join(sql_literal(value) for value in (order_id, "seed-" + order_id, request_hash, "synthetic", total, created)) +
                         ") ON CONFLICT(id) DO NOTHING;")
            for line_no, item in enumerate(items, 1):
                p = item["product"]
                values = (order_id, line_no, p["id"], p["name"], p["category"], p["price_minor"], item["quantity"],
                          p["sizes"][0] if p["sizes"] else None, p["colors"][0] if p["colors"] else None)
                lines.append("INSERT INTO order_items (order_id, line_no, product_id, product_name, category, unit_price_minor, quantity, size, color) SELECT " +
                             ", ".join(sql_literal(value) for value in values) +
                             f" WHERE EXISTS (SELECT 1 FROM orders WHERE id={sql_literal(order_id)} AND source='synthetic' AND request_hash={sql_literal(request_hash)})" +
                             " ON CONFLICT(order_id, line_no) DO NOTHING;")
    return "\n".join(lines) + "\n"


def cleaned_csv(products: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for product in products:
        writer.writerow({field: json.dumps(product[field], ensure_ascii=False, separators=(",", ":")) if isinstance(product[field], list) else product[field] for field in FIELDS})
    return stream.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "catalog_raw.csv")
    parser.add_argument("--public-root", type=Path, default=ROOT / "public")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--check", action="store_true", help="check committed outputs without changing any files")
    args = parser.parse_args(argv)
    try:
        products, report = clean_catalog(args.input, args.public_root)
    except (OSError, UnicodeError, csv.Error) as exc:
        print(f"Cannot read catalog: {exc}")
        return 1
    report_text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if report["status"] != "passed":
        if not args.check:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "quality_report.json").write_text(report_text, encoding="utf-8", newline="\n")
        print(report_text)
        print("Validation failed. No seed or cleaned CSV was written; previous valid outputs are unchanged.")
        return 1
    outputs = {"quality_report.json": report_text, "catalog_clean.csv": cleaned_csv(products),
               "catalog_seed.sql": product_seed(products), "synthetic_orders.sql": synthetic_seed(products)}
    if args.check:
        mismatches = [name for name, text in outputs.items() if not (args.output_dir / name).is_file() or (args.output_dir / name).read_text(encoding="utf-8") != text]
        if mismatches:
            print("Generated outputs are missing or stale: " + ", ".join(mismatches))
            return 1
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            (args.output_dir / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"PASS: {len(products)} valid products, 0 rejected rows, 36 reproducible synthetic orders; " +
          ("outputs match." if args.check else "outputs written."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
