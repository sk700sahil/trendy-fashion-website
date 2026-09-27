"""Strictly validate retailer products before they enter the active catalog.

Every input URL must have matching page and image evidence. Invalid listings
are reported and excluded from the active seed; historical order rows survive.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from clean_catalog import clean_catalog, sql_literal  # noqa: E402

CATEGORIES = {"men": "men", "women": "women", "kids": "kids", "footwear": "footwear", "accessories": "accessories"}
FIELDS = (
    "price_inr", "description", "color", "sizes", "brand", "subcategory", "mrp_inr",
    "discount_percent", "material", "fit", "rating", "source_product_id",
)
IMAGE_HOSTS = {
    "cdn.fcglcdn.com", "cdn.shopify.com", "cdn2.clevup.in", "images-eu.ssl-images-amazon.com",
    "indigodreams.in", "m.media-amazon.com", "mymilestones.in", "rukmini1.flixcart.com",
    "rukminim2.flixcart.com", "sreeleathersonline.com", "sunglassescraft.com", "uspoloassn.in",
    "walkwayshoes.com", "www.9shineslabel.com", "www.beloreslims.com", "www.montecarlo.in",
}
PRODUCT_COLUMNS = (
    "id", "name", "category", "description", "price_minor", "image", "alt", "sizes", "colors", "featured",
    "brand", "subcategory", "mrp_minor", "discount_percent", "material", "fit", "rating_value", "rating_scale",
    "rating_count", "source_store", "source_product_id", "canonical_url", "verification_status", "missing_fields",
)


def minor(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an INR amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be an INR amount") from exc
    if not amount.is_finite() or amount <= 0 or amount > 1_000_000 or amount * 100 != (amount * 100).to_integral_value():
        raise ValueError(f"{field} must be a positive INR amount with at most two decimals")
    return int(amount * 100)


def stable_id(value):
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    if not slug or len(slug) > 72:
        raise ValueError("source id cannot be converted to a stable product id")
    return "source-" + slug


def normalize_url(value):
    """Normalize tracking-only URL differences without dropping product identity parameters."""
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    tracking = {"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "tag", "linkcode", "srsltid"}
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered in tracking or lowered.startswith("utm_") or lowered.startswith("ref_"):
            continue
        query.append((key, value))
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(sorted(query)), ""))


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def meaningful_description(value, title):
    text = " ".join(str(value or "").split())
    normalized = normalize_text(text)
    if len(text) < 40 or not normalized or normalized == normalize_text(title):
        return False
    generic = ("no description available", "description not available", "product details unavailable",
               "shop now", "best price online", "buy online", "category")
    if any(normalized == phrase or normalized.startswith(phrase + " ") for phrase in generic):
        return False
    return True


DETAIL_FIELDS = {
    "description": "description", "material": "material", "fit": "fit", "color": "color",
    "sizes": "sizes", "subcategory": "subcategory", "style": "style", "features": "features",
    "product_type": "product_type", "specifications": "specifications", "variants": "variants",
}


def verified_details(item, page_check):
    fields = set(page_check.get("verified_fields", []))
    present = []
    for field, source_key in DETAIL_FIELDS.items():
        value = item.get(source_key)
        if field not in fields or value in (None, "", []):
            continue
        if field == "description" and not meaningful_description(value, item.get("title")):
            continue
        present.append(field)
    return present


def _source_failure(page_check):
    status = page_check.get("status")
    if status == "blocked":
        return "SOURCE_BLOCKED"
    if status == "inaccessible":
        return "SOURCE_INACCESSIBLE"
    if status == "no_product_content":
        return "SOURCE_NOT_PRODUCT"
    if status == "redirected_mismatch":
        return "REDIRECTED_TO_DIFFERENT_PRODUCT"
    if status != "accessible":
        return "SOURCE_INACCESSIBLE"
    if not page_check.get("identity_match"):
        return "PRODUCT_MISMATCH"
    return None


def _duplicate_of(product, existing_rows, seen_urls, seen_identity, seen_product_keys):
    url = normalize_url(product.get("canonical_url"))
    source_key = (str(product.get("source_store") or "").casefold(),
                  str(product.get("source_product_id") or "").casefold())
    if url and url in seen_urls:
        return seen_urls[url], "same normalized canonical source URL"
    if source_key[0] and source_key[1] and source_key in seen_identity:
        return seen_identity[source_key], "same retailer and retailer product ID"
    name = normalize_text(product.get("title"))
    brand = normalize_text(product.get("brand"))
    category = CATEGORIES.get(str(product.get("category") or "").strip().lower(), "")
    variants = tuple(sorted(normalize_text(v) for v in (product.get("sizes") or []) if normalize_text(v)))
    variants += tuple(sorted(normalize_text(v) for v in ([product.get("color")] if product.get("color") else [])))
    image = normalize_url((product.get("image_urls") or [None])[0])
    key = (name, brand, category, variants, image)
    if name and brand and category and image and key in seen_product_keys:
        return seen_product_keys[key], "same normalized title, brand, category, variants and image"
    for row in existing_rows:
        existing_id = row.get("id") or row.get("product_id")
        canonical = row.get("canonical_url") or row.get("source_url")
        if url and canonical and normalize_url(canonical) == url:
            return existing_id, "same normalized canonical source URL"
        old_store = str(row.get("source_store") or "").casefold()
        old_source_id = str(row.get("source_product_id") or "").casefold()
        if source_key[0] and source_key[1] and source_key == (old_store, old_source_id):
            return existing_id, "same retailer and retailer product ID"
        old_name = normalize_text(row.get("name") or row.get("title"))
        old_brand = normalize_text(row.get("brand"))
        old_category = str(row.get("category") or "").casefold()
        old_sizes = row.get("sizes") or []
        if isinstance(old_sizes, str):
            try:
                old_sizes = json.loads(old_sizes)
            except json.JSONDecodeError:
                old_sizes = []
        old_colors = row.get("colors") or []
        if isinstance(old_colors, str):
            try:
                old_colors = json.loads(old_colors)
            except json.JSONDecodeError:
                old_colors = []
        old_variants = tuple(sorted(normalize_text(v) for v in old_sizes if normalize_text(v)))
        old_variants += tuple(sorted(normalize_text(v) for v in old_colors if normalize_text(v)))
        old_image = normalize_url(row.get("image"))
        if name and brand and name == old_name and brand == old_brand and category == old_category and variants == old_variants and image and image == old_image:
            return existing_id, "same normalized title, brand, category, variants and image"
    return None, None


def validate_and_map(payload, existing_rows, page_checks, image_checks=None):
    """Validate every URL independently; return only complete products for seeding."""
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ValueError("source JSON must contain a products array")
    source = payload["products"]
    checks_by_id = {check.get("id"): check for check in page_checks.get("checks", [])}
    images_by_id = {check.get("product_id"): check for check in (image_checks or {}).get("checks", [])}
    existing_by_id = {row.get("id") or row.get("product_id"): row for row in existing_rows if row.get("id") or row.get("product_id")}
    seen_urls, seen_identity, seen_product_keys = {}, {}, {}
    accepted, results = [], []
    counts = Counter()

    for item in source:
        if not isinstance(item, dict):
            results.append({"product_id": None, "title": None, "source_url": None, "status": "REJECTED",
                            "validation": {}, "duplicate_of": None, "failure_reason": "VALIDATION_FAILED",
                            "failure_reasons": ["VALIDATION_FAILED"], "action_taken": "not inserted; input must be a product object"})
            counts["REJECTED"] += 1
            continue
        code = str(item.get("id") or "").strip()
        try:
            product_id = stable_id(code)
        except ValueError:
            product_id = None
        page = checks_by_id.get(code, {})
        image_check = images_by_id.get(code, {})
        source_url = item.get("canonical_url") or item.get("provided_url")
        title = " ".join(str(item.get("title") or "").split())
        details = verified_details(item, page)
        failures = []
        source_failure = _source_failure(page)
        if page.get("status") == "accessible" and page.get("http_status") != 200:
            source_failure = "SOURCE_INACCESSIBLE"
        url_valid = bool(isinstance(source_url, str) and urlparse(source_url).scheme == "https" and urlparse(source_url).hostname)
        if not url_valid:
            failures.append("SOURCE_URL_INVALID")
        elif normalize_url(page.get("source_url")) != normalize_url(source_url):
            failures.append("SOURCE_URL_INVALID")
        if source_failure:
            failures.append(source_failure)
        if not title or len(title) > 120:
            failures.append("VALIDATION_FAILED")
        category = CATEGORIES.get(str(item.get("category") or "").strip().lower())
        if not category:
            failures.append("INVALID_CATEGORY")
        if item.get("currency") != "INR":
            failures.append("PRICE_INVALID")
        try:
            price = minor(item.get("price_inr"), "price_inr")
        except ValueError:
            price = None
            failures.append("PRICE_INVALID")
        if price is None:
            failures.append("PRICE_MISSING")
        if "price" not in page.get("verified_fields", []):
            failures.append("PRICE_UNVERIFIED")
        try:
            mrp = minor(item.get("mrp_inr"), "mrp_inr")
        except ValueError:
            mrp = None
            failures.append("PRICE_INVALID")
        discount = item.get("discount_percent")
        if discount is not None and (type(discount) is not int or not 0 <= discount <= 100):
            discount = None
            failures.append("VALIDATION_FAILED")
        sizes = item.get("sizes") or []
        if not isinstance(sizes, list) or any(not isinstance(size, str) or not size.strip() or len(size) > 40 for size in sizes):
            sizes = []
            failures.append("VALIDATION_FAILED")
        color = item.get("color")
        if color is not None and (not isinstance(color, str) or len(color) > 100):
            color = None
            failures.append("VALIDATION_FAILED")
        rating = item.get("rating")
        if rating is not None and not isinstance(rating, dict):
            rating = None
            failures.append("VALIDATION_FAILED")
        if not details:
            description = item.get("description")
            failures.append("DESCRIPTION_MISSING" if not description else "DETAILS_INSUFFICIENT")
        image_urls = item.get("image_urls") or []
        if not isinstance(image_urls, list) or not image_urls:
            failures.append("IMAGE_MISSING")
            image_urls = []
        image_url = image_urls[0] if image_urls and isinstance(image_urls[0], str) else ""
        parsed_image = urlparse(image_url)
        if not image_url or parsed_image.scheme != "https" or parsed_image.hostname not in IMAGE_HOSTS:
            failures.append("IMAGE_BROKEN" if image_url else "IMAGE_MISSING")
        evidence_ok = bool(
            image_check.get("status") == "verified" and image_check.get("image_url") == image_url and
            normalize_url(image_check.get("source_url")) == normalize_url(source_url) and
            image_check.get("http_status") == 200 and
            str(image_check.get("content_type") or "").lower().split(";", 1)[0].startswith("image/") and
            image_check.get("https") is True and image_check.get("identity_match") is True and
            image_check.get("source_image_verified") is True and image_check.get("rendered") is True and
            image_check.get("csp_allowed") is True and "image" in page.get("verified_fields", [])
        )
        if not evidence_ok:
            failures.append("IMAGE_BROKEN" if image_url else "IMAGE_MISSING")
        if not details and not item.get("description"):
            failures.append("DESCRIPTION_MISSING")
        source_id = str(item.get("source_product_id") or "").strip() or None
        store = " ".join(str(item.get("source_store") or "").split())
        if not store:
            failures.append("VALIDATION_FAILED")
        duplicate_of, duplicate_reason = _duplicate_of(item, existing_rows, seen_urls, seen_identity, seen_product_keys)
        duplicate_status = None
        if duplicate_of:
            duplicate_status = "EXISTING" if duplicate_of == product_id and product_id in existing_by_id else "DUPLICATE"
        if duplicate_status == "DUPLICATE":
            failures = list(dict.fromkeys(failures + ["DUPLICATE_PRODUCT"]))
        failures = list(dict.fromkeys(failures))
        validation = {
            "valid_title": bool(title and len(title) <= 120), "valid_source_url": url_valid,
            "source_accessible": page.get("status") == "accessible" and page.get("http_status") == 200,
            "identity_matched": page.get("identity_match") is True,
            "verified_price": price is not None and "price" in page.get("verified_fields", []),
            "working_real_image": evidence_ok, "meaningful_source_verified_details": bool(details),
            "valid_category": bool(category), "not_duplicate": not duplicate_of,
            "verified_detail_fields": details,
        }
        if duplicate_status == "EXISTING":
            status = "EXISTING"
            action = "kept existing validated product; incoming row did not overwrite it"
        elif duplicate_status == "DUPLICATE":
            status = "DUPLICATE"
            action = "not inserted; existing product retained"
        elif failures:
            status = "REJECTED"
            action = "not inserted; excluded from active catalog"
        else:
            status = "ACCEPTED"
            action = "inserted into active import set"
            verified = set(page.get("verified_fields", []))
            description = item.get("description") if "description" in verified and "description" in details else ""
            color = item.get("color") if "color" in verified else None
            sizes = item.get("sizes") if "sizes" in verified else []
            rating = item.get("rating") if "rating" in verified and isinstance(item.get("rating"), dict) else None
            verified_mrp = mrp if "mrp" in verified or "mrp_inr" in page.get("restored_fields", []) else None
            verified_discount = discount if "discount_percent" in verified else None
            missing = []
            for field, evidence in (("description", "description"), ("color", "color"), ("sizes", "sizes"),
                                    ("brand", "brand"), ("subcategory", "subcategory"),
                                    ("mrp_inr", "mrp"), ("discount_percent", "discount_percent"),
                                    ("material", "material"), ("fit", "fit"), ("rating", "rating")):
                if evidence not in verified or item.get(field) in (None, "", []):
                    missing.append(field)
            if not source_id:
                missing.append("source_product_id")
            mapped = {
                "id": product_id, "name": title, "category": category,
                "description": description or "", "price_minor": price,
                "image": image_url, "alt": f"{title} product image",
                "sizes": sizes if isinstance(sizes, list) else [], "colors": [color.strip()] if isinstance(color, str) and color.strip() else [],
                "featured": 0, "brand": item.get("brand") if "brand" in verified else None,
                "subcategory": item.get("subcategory") if "subcategory" in verified else None,
                "mrp_minor": verified_mrp, "discount_percent": verified_discount,
                "material": item.get("material") if "material" in verified else None,
                "fit": item.get("fit") if "fit" in verified else None,
                "rating_value": (rating or {}).get("value"),
                "rating_scale": (rating or {}).get("scale"),
                "rating_count": (rating or {}).get("count"),
                "source_store": store, "source_product_id": source_id,
                "canonical_url": source_url, "verification_status": "verified",
                "missing_fields": missing, "source_code": code, "image_urls": image_urls,
            }
            accepted.append(mapped)
        if status in ("ACCEPTED", "EXISTING", "DUPLICATE") and source_url:
            normalized = normalize_url(source_url)
            if normalized:
                seen_urls.setdefault(normalized, product_id)
        if status in ("ACCEPTED", "EXISTING", "DUPLICATE") and store and source_id:
            seen_identity.setdefault((store.casefold(), source_id.casefold()), product_id)
        if status in ("ACCEPTED", "EXISTING", "DUPLICATE") and title and item.get("brand") and category and image_url:
            variants = tuple(sorted(normalize_text(v) for v in (item.get("sizes") or []) if normalize_text(v)))
            variants += tuple(sorted(normalize_text(v) for v in ([item.get("color")] if item.get("color") else [])))
            seen_product_keys.setdefault((normalize_text(title), normalize_text(item.get("brand")), category, variants, normalize_url(image_url)), product_id)
        counts[status] += 1
        results.append({
            "product_id": product_id, "title": title or None, "source_url": source_url,
            "status": status, "validation": validation, "duplicate_of": duplicate_of,
            "duplicate_reason": duplicate_reason,
            "failure_reason": failures[0] if failures else None, "failure_reasons": failures,
            "action_taken": action,
        })

    report = {
        "status": "passed", "source_checked_on": payload.get("catalog_metadata", {}).get("checked_on"),
        "urls_received": len(source), "successfully_added": counts["ACCEPTED"],
        "valid_imports": counts["ACCEPTED"] + counts["EXISTING"],
        "already_existed": counts["EXISTING"], "duplicates": counts["DUPLICATE"],
        "rejected": counts["REJECTED"], "failure_breakdown": dict(sorted(Counter(
            reason for result in results for reason in result["failure_reasons"] if result["status"] in ("REJECTED", "DUPLICATE")
        ).items())), "products": results,
    }
    return accepted, report


def seed_sql(products, retire_records=()):
    lines = ["-- Generated by scripts/import_source_catalog.py. Only complete, validated imports are active.",
             "-- Rejected records are retired without deleting order-item snapshots."]
    for product in products:
        product_id = sql_literal(product["id"])
        values = []
        for column in PRODUCT_COLUMNS:
            value = product[column]
            if column in ("sizes", "colors", "missing_fields"):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            values.append(sql_literal(value))
        updates = ",".join(f"{column}=excluded.{column}" for column in PRODUCT_COLUMNS if column != "id")
        lines += [f"DELETE FROM retired_products WHERE product_id={product_id};",
                  f"DELETE FROM unavailable_products WHERE id={product_id};",
                  f"DELETE FROM source_products WHERE id={product_id};",
                  f"INSERT INTO products ({','.join(PRODUCT_COLUMNS)}) VALUES ({','.join(values)}) "
                  f"ON CONFLICT(id) DO UPDATE SET {updates};"]
    for record in retire_records:
        product_id = record.get("product_id")
        if not product_id:
            continue
        pid = sql_literal(product_id)
        name = sql_literal(record.get("title") or product_id)
        url = sql_literal(record.get("source_url"))
        reason = sql_literal(json.dumps(record.get("failure_reasons") or [record.get("failure_reason") or "VALIDATION_FAILED"], separators=(",", ":")))
        lines += [f"INSERT INTO retired_products (product_id,name,source_url,failure_reason) VALUES ({pid},{name},{url},{reason}) "
                  f"ON CONFLICT(product_id) DO UPDATE SET name=excluded.name,source_url=excluded.source_url,failure_reason=excluded.failure_reason;",
                  f"INSERT OR IGNORE INTO unavailable_products (id,reason) SELECT {pid},'strict_import_rejected' "
                  f"WHERE EXISTS (SELECT 1 FROM products WHERE id={pid}) AND EXISTS (SELECT 1 FROM order_items WHERE product_id={pid});",
                  f"DELETE FROM source_products WHERE id={pid};",
                  f"DELETE FROM products WHERE id={pid} AND NOT EXISTS (SELECT 1 FROM order_items WHERE product_id={pid});"]
    return "\n".join(lines) + "\n"


def _active_ledger(path):
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("products", [])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "trendy_threads_products_source.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--existing", type=Path, default=ROOT / "data" / "catalog_raw.csv")
    parser.add_argument("--checks", type=Path, default=ROOT / "data" / "source_page_checks.json")
    parser.add_argument("--image-checks", type=Path, default=ROOT / "data" / "source_image_checks.json")
    parser.add_argument("--check", action="store_true", help="fail if committed generated outputs are stale")
    args = parser.parse_args(argv)
    ledger_path = args.output_dir / "active_import_ledger.json"
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        original_products, existing_report = clean_catalog(args.existing, ROOT / "public")
        if existing_report["status"] != "passed":
            raise ValueError("original catalog failed validation")
        page_checks = json.loads(args.checks.read_text(encoding="utf-8"))
        image_checks = json.loads(args.image_checks.read_text(encoding="utf-8"))
        ledger = _active_ledger(ledger_path)
        duplicate_rows = [*original_products, *ledger]
        incoming, report = validate_and_map(payload, duplicate_rows, page_checks, image_checks)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: strict product import did not run: {exc}", file=sys.stderr)
        return 1

    active = {row["id"]: row for row in ledger}
    incoming_by_id = {record["product_id"]: record for record in report["products"] if record.get("product_id")}
    for product in incoming:
        old = active.get(product["id"])
        result = incoming_by_id.get(product["id"], {})
        if result.get("status") == "EXISTING" and old:
            continue
        active[product["id"]] = product
    # Invalid updates never replace an already validated product. A cleanup run without a good ledger retires invalid items.
    retire_records = [result for result in report["products"]
                      if result.get("status") == "REJECTED" and result.get("product_id") not in active]
    merged = list(active.values())
    report["final_catalog_count"] = len(original_products) + len(merged)
    report["active_imported_count"] = len(merged)
    report["retired_count"] = len(retire_records)
    report["duplicate_url_policy"] = "HTTPS URLs are normalized for host/path and tracking parameters; retailer SKU and variant parameters are retained."
    report["idempotent"] = True
    ledger_data = {"schema_version": 1, "products": sorted(merged, key=lambda product: product["id"])}
    outputs = {
        args.output_dir / "source_products_seed.sql": seed_sql(merged, retire_records),
        args.output_dir / "source_import_report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        ledger_path: json.dumps(ledger_data, ensure_ascii=False, indent=2) + "\n",
    }
    stale = []
    for path, content in outputs.items():
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(path.name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        print("FAIL: stale strict-import outputs: " + ", ".join(stale), file=sys.stderr)
        return 1
    print("PRODUCT IMPORT REPORT")
    print(f"URLs received: {report['urls_received']}; valid: {report['valid_imports']}; "
          f"added: {report['successfully_added']}; "
          f"existing: {report['already_existed']}; duplicates: {report['duplicates']}; rejected: {report['rejected']}")
    print(f"Active products: {report['final_catalog_count']} total ({len(original_products)} original, {len(merged)} imported)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
