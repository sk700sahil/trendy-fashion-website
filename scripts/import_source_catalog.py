"""Validate and import the supplied retailer list without inventing values.

Priced entries become orderable products. Entries without a verified current
price remain visible in source_products and cannot be checked out.
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
from urllib.parse import urlparse

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


def validate_and_map(payload, existing_rows, page_checks):
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ValueError("source JSON must contain a products array")
    source = payload["products"]
    if len(source) != 61:
        raise ValueError(f"expected all 61 supplied rows, got {len(source)}")

    ids, urls, identities = Counter(), Counter(), Counter()
    mapped, errors = [], []
    field_missing = Counter()
    for row_number, item in enumerate(source, 1):
        try:
            if not isinstance(item, dict):
                raise ValueError("product row must be an object")
            code = item.get("id")
            canonical = item.get("canonical_url")
            parsed = urlparse(canonical or "")
            if not code or not canonical or parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("stable source id and HTTPS canonical URL are required")
            if item.get("currency") != "INR":
                raise ValueError("source currency must be INR")
            category_key = str(item.get("category", "")).strip().lower()
            category = CATEGORIES.get(category_key)
            if not category:
                raise ValueError(f"unsupported category: {category_key}")
            name = " ".join(str(item.get("title") or "").split())
            if not name or len(name) > 120:
                raise ValueError("title must contain 1-120 characters")
            store = " ".join(str(item.get("source_store") or "").split())
            if not store:
                raise ValueError("source store is required")
            sizes = item.get("sizes") or []
            if not isinstance(sizes, list) or any(not isinstance(s, str) or not s.strip() or len(s) > 40 for s in sizes):
                raise ValueError("sizes must be a list of non-empty labels")
            if len({s.strip().casefold() for s in sizes}) != len(sizes):
                sizes = list(dict.fromkeys(s.strip() for s in sizes))
            color = item.get("color")
            if color is not None and not isinstance(color, str):
                raise ValueError("color must be text or missing")
            description = item.get("description")
            if description is not None and (not isinstance(description, str) or len(description) > 2000):
                raise ValueError("description must be text of 2000 characters or fewer")
            price = minor(item.get("price_inr"), "price_inr")
            mrp = minor(item.get("mrp_inr"), "mrp_inr")
            raw_images = item.get("image_urls") or []
            if not isinstance(raw_images, list) or any(not isinstance(value, str) for value in raw_images):
                raise ValueError("image_urls must be a list of verified HTTPS image URLs")
            image_urls = []
            for value in raw_images:
                image_url = urlparse(value)
                if image_url.scheme != "https" or image_url.hostname not in IMAGE_HOSTS:
                    raise ValueError("image_urls must use an allowlisted HTTPS retailer/CDN host")
                if value not in image_urls:
                    image_urls.append(value)
            discount = item.get("discount_percent")
            if discount is not None and (type(discount) is not int or not 0 <= discount <= 100):
                raise ValueError("discount_percent must be an integer from 0 to 100")
            rating = item.get("rating")
            if rating is not None and not isinstance(rating, dict):
                raise ValueError("rating must be an object or missing")
            rating_value = rating.get("value") if rating else None
            rating_scale = rating.get("scale") if rating else None
            rating_count = rating.get("count") if rating else None
            if rating_value is not None:
                rating_value = float(rating_value)
                rating_scale = float(rating_scale or 5)
                if not 0 <= rating_value <= rating_scale:
                    raise ValueError("rating value is outside its supplied scale")
            if rating_count is not None and (type(rating_count) is not int or rating_count < 0):
                raise ValueError("rating count must be a non-negative integer")
            source_id = item.get("source_product_id")
            source_id = str(source_id).strip() if source_id is not None else None
            if source_id == "":
                source_id = None
            row_id = stable_id(code)
            missing = [field for field in FIELDS if not item.get(field)]
            if not image_urls:
                missing.append("image")
            ids[row_id] += 1
            urls[canonical] += 1
            if source_id:
                identities[(store.casefold(), source_id.casefold())] += 1
            field_missing.update(missing)
            mapped.append({
                "id": row_id, "name": name, "category": category,
                "description": description if description is not None else ("" if price is not None else None),
                "price_minor": price,
                "image": image_urls[0] if image_urls else "/assets/images/product-placeholder.svg",
                "alt": f"{name} product image" if image_urls else "Retailer product image unavailable; local placeholder shown",
                "sizes": sizes, "colors": [color.strip()] if isinstance(color, str) and color.strip() else [],
                "featured": 0, "brand": item.get("brand"), "subcategory": item.get("subcategory"),
                "mrp_minor": mrp, "discount_percent": discount, "material": item.get("material"),
                "fit": item.get("fit"), "rating_value": rating_value, "rating_scale": rating_scale,
                "rating_count": rating_count, "source_store": store, "source_product_id": source_id,
                "canonical_url": canonical, "verification_status": str(item.get("verification_status") or "unverified"),
                "missing_fields": missing, "source_code": code, "image_urls": image_urls,
            })
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append({"row": row_number, "id": item.get("id") if isinstance(item, dict) else None, "error": str(exc)})

    duplicate_ids = sorted(key for key, count in ids.items() if count > 1)
    duplicate_urls = sorted(key for key, count in urls.items() if count > 1)
    duplicate_identity = sorted([list(key) for key, count in identities.items() if count > 1])
    if duplicate_ids or duplicate_urls or duplicate_identity:
        errors.append({"error": "duplicate source identity", "ids": duplicate_ids,
                       "canonical_urls": duplicate_urls, "store_product_ids": duplicate_identity})
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))

    # Exact titles are a conservative duplicate check; fuzzy matches are reported for review, never silently dropped.
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    old_names = [(row["id"], row["name"]) for row in existing_rows]
    existing_name_set = {normalize(name) for _, name in old_names}
    exact = [p["source_code"] for p in mapped if normalize(p["name"]) in existing_name_set]
    candidates = []
    for product in mapped:
        best = max(((difflib.SequenceMatcher(None, normalize(product["name"]), normalize(name)).ratio(), old_id, name)
                    for old_id, name in old_names), default=(0, "", ""))
        if best[0] >= 0.65:
            candidates.append({"source_id": product["source_code"], "title": product["name"],
                               "existing_id": best[1], "existing_title": best[2], "similarity": round(best[0], 3),
                               "disposition": "retained; no matching source ID or canonical URL proves a duplicate"})

    checked_ids = {entry.get("id") for entry in page_checks.get("checks", [])}
    source_by_code = {item["id"]: item for item in source}
    page_results = []
    for check in page_checks.get("checks", []):
        original = source_by_code.get(check.get("id"), {})
        page_results.append({**check, "url": original.get("canonical_url")})
    inaccessible = sum(check["status"] == "inaccessible" for check in page_results)
    blocked = sum(check["status"] == "blocked" for check in page_results)
    no_product_content = sum(check["status"] == "no_product_content" for check in page_results)
    priced = [p for p in mapped if p["price_minor"] is not None]
    unpriced = [p for p in mapped if p["price_minor"] is None]
    combined_counts = Counter(row["category"] for row in existing_rows)
    combined_counts.update(p["category"] for p in mapped)
    report = {
        "status": "passed", "source_file": "trendy_threads_products_source.json",
        "source_checked_on": payload.get("catalog_metadata", {}).get("checked_on"),
        "source_rows": len(source), "imported_source_rows": len(mapped),
        "existing_products_preserved": len(existing_rows), "final_catalog_count": len(existing_rows) + len(mapped),
        "source_priced_and_orderable": len(priced), "source_visible_price_unavailable": len(unpriced),
        "products_enriched": sum(bool(check.get("identity_match") and check.get("restored_fields")) for check in page_results),
        "products_with_real_images_restored": sum(bool(p["image_urls"]) for p in mapped),
        "prices_restored": sum("price_inr" in check.get("restored_fields", []) for check in page_results),
        "descriptions_details_restored": sum(bool(set(check.get("restored_fields", [])) &
                                                    {"description", "brand", "color", "material", "fit", "sizes", "rating", "mrp_inr", "discount_percent"})
                                              for check in page_results),
        "products_still_using_placeholders": [p["source_code"] for p in mapped if not p["image_urls"]],
        "missing_current_prices": [p["source_code"] for p in unpriced],
        "missing_images": [p["source_code"] for p in mapped if "image_urls" in p["missing_fields"]],
        "image_policy": "Verified HTTPS retailer/CDN URLs are used when confirmed on the matching product page; exact CDN origins are allowlisted in public/_headers. Images that fail verification remain on the local placeholder. Retailer images are not downloaded or rehosted.",
        "missing_field_counts": dict(sorted(field_missing.items())),
        "exact_duplicates_found": exact,
        "id_collisions_with_existing_catalog": sorted(set(ids) & {row["id"] for row in existing_rows}),
        "duplicate_ids_or_urls": {"source_ids": duplicate_ids, "canonical_urls": duplicate_urls,
                                   "store_product_ids": duplicate_identity},
        "similarity_review_candidates": candidates,
        "category_counts_final": dict(sorted(combined_counts.items())),
        "confirmed_broken_urls": [],
        "source_pages_checked": len(page_results), "source_pages_inaccessible": inaccessible,
        "source_pages_blocked": blocked, "source_pages_without_product_content": no_product_content,
        "source_pages_not_individually_checked": len(source) - len(checked_ids),
        "redirected_or_suspicious_products": [check for check in page_results if check.get("redirected_asin")],
        "source_page_checks": page_results,
        "unavailable_fields_note": "Missing retailer values remain NULL/empty and are exposed in missing_fields; no values were inferred.",
    }
    return mapped, report


def seed_sql(products):
    lines = ["-- Generated by scripts/import_source_catalog.py. Do not edit by hand.",
             "-- Source-page values are populated only when the matching retailer listing exposed them."]
    for product in products:
        target = "products" if product["price_minor"] is not None else "source_products"
        other = "source_products" if target == "products" else "products"
        lines.append(f"DELETE FROM {other} WHERE id={sql_literal(product['id'])};")
        columns = PRODUCT_COLUMNS
        values = []
        for column in columns:
            value = product[column]
            if column in ("sizes", "colors", "missing_fields"):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            values.append(sql_literal(value))
        updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "id")
        lines.append(f"INSERT INTO {target} ({','.join(columns)}) VALUES ({','.join(values)}) "
                     f"ON CONFLICT(id) DO UPDATE SET {updates};")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "trendy_threads_products_source.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--existing", type=Path, default=ROOT / "data" / "catalog_raw.csv")
    parser.add_argument("--checks", type=Path, default=ROOT / "data" / "source_page_checks.json")
    parser.add_argument("--check", action="store_true", help="fail if committed generated outputs are stale")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        with args.existing.open(encoding="utf-8-sig", newline="") as stream:
            existing = list(csv.DictReader(stream))
        existing_products, existing_report = clean_catalog(args.existing, ROOT / "public")
        if existing_report["status"] != "passed":
            raise ValueError("existing catalog failed validation")
        checks = json.loads(args.checks.read_text(encoding="utf-8"))
        products, report = validate_and_map(payload, existing_products, checks)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: source catalog was not generated: {exc}", file=sys.stderr)
        return 1
    outputs = {
        args.output_dir / "source_products_seed.sql": seed_sql(products),
        args.output_dir / "source_import_report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
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
        print("FAIL: stale source catalog outputs: " + ", ".join(stale), file=sys.stderr)
        return 1
    print(f"PASS: imported {len(products)} source rows; {report['products_enriched']} enriched, "
          f"{report['products_with_real_images_restored']} with verified images, "
          f"{report['prices_restored']} prices restored; "
          f"{report['source_priced_and_orderable']} priced, "
          f"{report['source_visible_price_unavailable']} with price unavailable; "
          f"catalog total {report['final_catalog_count']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
