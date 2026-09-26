"""Portable catalog/order logic; SQL runs on D1 and in SQLite contract tests."""

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

CATEGORIES = ("men", "women", "kids", "accessories", "footwear")
MAX_BODY_BYTES = 16_384
MAX_LINES = 20
MAX_QUANTITY = 10
MAX_UNITS = 50
PRODUCT_COLUMNS = (
    "id,name,category,description,price_minor,image,alt,sizes,colors,featured,brand,subcategory,mrp_minor,"
    "discount_percent,material,fit,rating_value,rating_scale,rating_count,source_store,source_product_id,"
    "canonical_url,verification_status,missing_fields"
)


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def invalid(message):
    raise APIError(400, "invalid_request", message)


def product_json(row):
    product = dict(row)
    for field in ("sizes", "colors", "missing_fields"):
        product[field] = json.loads(product[field])
    product["featured"] = bool(product["featured"])
    product["currency"] = "INR"
    return product


def price_filter(value, name):
    if not re.fullmatch(r"[0-9]{1,9}", value):
        invalid(f"{name} must be a non-negative integer in paise.")
    return int(value)


async def list_products(db, params):
    clauses, values = [], []
    category = params.get("category", "").strip().lower()
    if category and category != "all":
        if category not in CATEGORIES:
            invalid("Choose a valid product category.")
        clauses.append("category = ?")
        values.append(category)
    search = params.get("q", "").strip()
    if len(search) > 100:
        invalid("Search must be 100 characters or fewer.")
    if search:
        search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(name LIKE ? ESCAPE '\\' OR COALESCE(description,'') LIKE ? ESCAPE '\\' OR COALESCE(brand,'') LIKE ? ESCAPE '\\' OR COALESCE(source_store,'') LIKE ? ESCAPE '\\')")
        values.extend([f"%{search}%"] * 4)
    minimum = maximum = None
    if params.get("min_price", ""):
        minimum = price_filter(params["min_price"], "min_price")
        clauses.append("price_minor >= ?")
        values.append(minimum)
    if params.get("max_price", ""):
        maximum = price_filter(params["max_price"], "max_price")
        clauses.append("price_minor <= ?")
        values.append(maximum)
    if minimum is not None and maximum is not None and minimum > maximum:
        invalid("Minimum price cannot exceed maximum price.")
    sorts = {
        "featured": "featured DESC, name COLLATE NOCASE, id",
        "price_asc": "CASE WHEN price_minor IS NULL THEN 1 ELSE 0 END, price_minor, name COLLATE NOCASE, id",
        "price_desc": "CASE WHEN price_minor IS NULL THEN 1 ELSE 0 END, price_minor DESC, name COLLATE NOCASE, id",
        "name": "name COLLATE NOCASE, id",
    }
    sort = params.get("sort", "featured")
    if sort not in sorts:
        invalid("Choose a valid product sort order.")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    # Keep unavailable-price source items visible in the unfiltered catalog, but sort them last.
    rows = await db.all(f"SELECT {PRODUCT_COLUMNS} FROM catalog_products{where} ORDER BY {sorts[sort]}", values)
    products = [product_json(row) for row in rows]
    return {"products": products, "total": len(products)}


async def get_product(db, product_id):
    rows = await db.all(f"SELECT {PRODUCT_COLUMNS} FROM catalog_products WHERE id = ?", [product_id])
    if not rows:
        raise APIError(404, "product_not_found", "This product could not be found.")
    product = product_json(rows[0])
    rows = await db.all(
        f"SELECT {PRODUCT_COLUMNS} FROM catalog_products WHERE category = ? AND id != ? "
        "ORDER BY featured DESC, name COLLATE NOCASE, id LIMIT 4",
        [product["category"], product_id],
    )
    return {"product": product, "related": [product_json(row) for row in rows]}


def normalize_order(body, key):
    if not isinstance(key, str) or len(key) != 36:
        invalid("Send a UUID Idempotency-Key header for this checkout.")
    try:
        key = str(UUID(key))
    except ValueError:
        invalid("Send a valid UUID Idempotency-Key header.")
    if not isinstance(body, dict) or set(body) != {"items"}:
        invalid("Send only an items list; no customer or payment details are needed.")
    if not isinstance(body["items"], list) or not 1 <= len(body["items"]) <= MAX_LINES:
        invalid(f"An order must contain 1 to {MAX_LINES} item lines.")
    combined = {}
    for item in body["items"]:
        if not isinstance(item, dict) or set(item) - {"product_id", "quantity", "size", "color"}:
            invalid("Each item accepts only product_id, quantity, size and color.")
        product_id = item.get("product_id")
        quantity = item.get("quantity")
        if not isinstance(product_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", product_id):
            invalid("Each item needs a valid product_id.")
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
            invalid(f"Each quantity must be a whole number from 1 to {MAX_QUANTITY}.")
        variant = []
        for field in ("size", "color"):
            value = item.get(field, "")
            if not isinstance(value, str) or len(value) > 60:
                invalid(f"Each {field} must be a string of 60 characters or fewer.")
            variant.append(value.strip())
        identity = (product_id, *variant)
        combined[identity] = combined.get(identity, 0) + quantity
        if combined[identity] > MAX_QUANTITY:
            invalid(f"A product variant may have at most {MAX_QUANTITY} units.")
    if sum(combined.values()) > MAX_UNITS:
        invalid(f"An order may contain at most {MAX_UNITS} units.")
    items = [
        {"product_id": product_id, "size": size, "color": color, "quantity": quantity}
        for (product_id, size, color), quantity in sorted(combined.items())
    ]
    fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return items, key, fingerprint


async def existing_order(db, key, fingerprint):
    rows = await db.all("SELECT id,request_hash FROM orders WHERE idempotency_key = ?", [key])
    if not rows:
        return None
    if rows[0]["request_hash"] != fingerprint:
        raise APIError(409, "idempotency_conflict", "This checkout key already belongs to a different cart. Start a new checkout.")
    return await read_order(db, rows[0]["id"])


async def read_order(db, order_id):
    rows = await db.all("SELECT id,total_minor,source,created_at FROM orders WHERE id = ?", [order_id])
    order = dict(rows[0])
    order["currency"] = "INR"
    order["items"] = await db.all(
        "SELECT product_id,product_name,category,unit_price_minor,quantity,size,color "
        "FROM order_items WHERE order_id = ? ORDER BY line_no", [order_id]
    )
    return order


async def create_order(db, body, key):
    items, key, fingerprint = normalize_order(body, key)
    previous = await existing_order(db, key, fingerprint)
    if previous:
        return {"order": previous, "replayed": True}, 200
    ids = sorted({item["product_id"] for item in items})
    placeholders = ",".join("?" for _ in ids)
    rows = await db.all(f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id IN ({placeholders})", ids)
    products = {row["id"]: product_json(row) for row in rows}
    priced = []
    for item in items:
        product = products.get(item["product_id"])
        if product is None:
            raise APIError(400, "invalid_product", "Your cart contains an unavailable product. Refresh the catalog.")
        for field, options in (("size", product["sizes"]), ("color", product["colors"])):
            if (options and item[field] not in options) or (not options and item[field]):
                raise APIError(400, "invalid_variant", f"Choose an available {field} for {product['name']}.")
        priced.append({**item, "product_name": product["name"], "category": product["category"], "unit_price_minor": product["price_minor"]})
    total = sum(item["quantity"] * item["unit_price_minor"] for item in priced)
    order_id = "tt-" + str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    statements = [(
        "INSERT INTO orders (id,idempotency_key,request_hash,source,total_minor,created_at) VALUES (?,?,?,'visitor',?,?)",
        [order_id, key, fingerprint, total, created_at],
    )]
    for index, item in enumerate(priced, 1):
        statements.append((
            "INSERT INTO order_items (order_id,line_no,product_id,product_name,category,unit_price_minor,quantity,size,color) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [order_id, index, item["product_id"], item["product_name"], item["category"], item["unit_price_minor"], item["quantity"], item["size"], item["color"]],
        ))
    try:
        # D1 batch is one transaction. A unique-key loser rolls back all its rows.
        await db.batch(statements)
    except Exception:
        # A concurrent identical submission may have committed after our initial read.
        previous = await existing_order(db, key, fingerprint)
        if previous:
            return {"order": previous, "replayed": True}, 200
        raise
    return {"order": {"id": order_id, "total_minor": total, "currency": "INR", "source": "visitor", "created_at": created_at, "items": priced}, "replayed": False}, 201


async def analytics(db, source="all"):
    if source not in ("all", "synthetic", "visitor"):
        invalid("Analytics source must be all, synthetic or visitor.")
    where = "" if source == "all" else " WHERE o.source = ?"
    values = [] if source == "all" else [source]
    queries = [
        ("SELECT COUNT(*) AS orders,COALESCE(SUM(total_minor),0) AS revenue_minor," 
         "COALESCE(CAST(ROUND(AVG(total_minor)) AS INTEGER),0) AS average_order_minor FROM orders o" + where, values),
        ("SELECT COALESCE(SUM(i.quantity),0) AS units FROM order_items i JOIN orders o ON o.id=i.order_id" + where, values),
        ("SELECT source,COUNT(*) AS orders FROM orders GROUP BY source", []),
        ("SELECT substr(o.created_at,1,7) AS month,COUNT(DISTINCT o.id) AS orders," 
         "SUM(i.unit_price_minor*i.quantity) AS revenue_minor,SUM(i.quantity) AS units "
         "FROM orders o JOIN order_items i ON i.order_id=o.id" + where + " GROUP BY month ORDER BY month", values),
        ("SELECT i.category,SUM(i.quantity) AS units,SUM(i.unit_price_minor*i.quantity) AS revenue_minor "
         "FROM order_items i JOIN orders o ON o.id=i.order_id" + where + " GROUP BY i.category ORDER BY revenue_minor DESC,i.category", values),
        ("SELECT i.product_id,MAX(i.product_name) AS name,MAX(i.category) AS category," 
         "SUM(i.quantity) AS units,SUM(i.unit_price_minor*i.quantity) AS revenue_minor "
         "FROM order_items i JOIN orders o ON o.id=i.order_id" + where + " GROUP BY i.product_id ORDER BY units DESC,revenue_minor DESC,i.product_id LIMIT 5", values),
    ]
    results = await db.batch(queries)
    summary = {**results[0][0], **results[1][0]}
    counts = {"synthetic": 0, "visitor": 0}
    counts.update({row["source"]: row["orders"] for row in results[2]})
    return {"source": source, "currency": "INR", "summary": summary, "source_counts": counts,
            "monthly": results[3], "categories": results[4], "popular": results[5]}
