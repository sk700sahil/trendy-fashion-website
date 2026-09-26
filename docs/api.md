# Trendy Threads API

The same Cloudflare Python Worker serves `/api/*` alongside Workers Static Assets. D1 stores the catalog and demo orders. All monetary values are integer **paise**: `129900` means ₹1,299. API responses use JSON and `Cache-Control: no-store`; there are no accounts, payment providers or customer details.

## Catalog

`GET /api/products` returns `{ "products": [...], "total": 86 }` before filtering. The catalog combines 25 existing curated items and 61 retailer-source listings. `price_minor` is integer paise for a supplied price, or `null` when the source did not provide one. Products also expose optional `brand`, `subcategory`, MRP/discount, material, fit, rating, source store/URL, verification status, and `missing_fields`. Sizes and colors are arrays; images are local public paths. All 61 source listings currently use the neutral local placeholder because no reusable image URLs were supplied.

Listings with `price_minor: null` remain searchable and link to their source page, but cannot be added to the demo bag or ordered. Price filters omit them, and price sorting puts them last. No price is inferred from MRP or another product.

| Optional query | Accepted value |
| --- | --- |
| `q` | Search name/description/brand/store, at most 100 characters; SQL wildcard characters are treated literally |
| `category` | `men`, `women`, `kids`, `accessories`, `footwear`; empty or `all` selects all |
| `min_price`, `max_price` | Inclusive non-negative integer amounts in paise |
| `sort` | `featured` (default), `price_asc`, `price_desc`, `name` |

Example: `/api/products?category=women&max_price=200000&sort=price_asc`.

`GET /api/products/{id}` returns `{ "product": {...}, "related": [...] }`, with up to four other products in the same category. An absent product returns `404`.

## Demo checkout

`POST /api/orders` requires `Content-Type: application/json` and an `Idempotency-Key` header containing a UUID. Generate the key once for each intended checkout and persist it with the immutable request in browser storage **before** sending. Keep the same key and payload on retry after a network error or page refresh. A new, deliberately changed checkout needs a new key.

```json
{
  "items": [
    {"product_id": "catalog-product-id", "quantity": 2, "size": "M", "color": "Blue"}
  ]
}
```

Use an actual product ID and one of its published size/color values. A field may be omitted or `""` only when the product has no options for that field. No other top-level or item fields are accepted, including browser prices, source labels and personal information.

Limits: 16 KiB body, 1–20 input lines, integer quantities 1–10 per variant, and at most 50 units in the whole order. Duplicate variant lines are combined; their combined quantity must still be at most 10. Variant values are trimmed and cart lines are sorted before hashing, so equivalent reordered carts safely replay.

A new order returns HTTP `201`:

```json
{
  "order": {
    "id": "tt-<uuid>",
    "total_minor": 259800,
    "currency": "INR",
    "source": "visitor",
    "created_at": "2026-09-24T12:00:00Z",
    "items": [
      {"product_id": "catalog-product-id", "product_name": "Cotton shirt", "category": "men", "unit_price_minor": 129900, "quantity": 2, "size": "M", "color": "Blue"}
    ]
  },
  "replayed": false
}
```

The backend loads prices and variant choices from D1, calculates totals and snapshots the names, categories and unit prices. It inserts the order and all items in one atomic D1 `batch()`. A unique database constraint on the key prevents two concurrent submissions from making two orders. An identical retry returns the original saved order with HTTP `200` and `replayed: true`, even if catalog prices later change. Reusing a key for a different cart returns `409 idempotency_conflict`. Other database failures return `503` without exposing SQL or request contents. Visitors cannot create `synthetic` orders via the API.

Display **“Demo order — no payment or delivery”** throughout checkout and confirmation. This is a public portfolio demonstration without authentication, fulfillment or an inventory reservation system.

## Analytics

`GET /api/analytics?source=all` accepts `all` (default), `synthetic`, or `visitor`.

```json
{
  "source": "all",
  "currency": "INR",
  "summary": {"orders": 1, "units": 2, "revenue_minor": 259800, "average_order_minor": 259800},
  "source_counts": {"synthetic": 0, "visitor": 1},
  "monthly": [{"month": "2026-09", "orders": 1, "units": 2, "revenue_minor": 259800}],
  "categories": [{"category": "men", "units": 2, "revenue_minor": 259800}],
  "popular": [{"product_id": "catalog-product-id", "name": "Cotton shirt", "category": "men", "units": 2, "revenue_minor": 259800}]
}
```

`source_counts` always gives the global counts of both sources, independent of the selected filter, so the chart context stays visible. All other metrics obey the source filter. Monthly buckets are UTC `YYYY-MM`; months with no orders are absent. Popular products are the top five by units, then demo value, with stable ID tie-breaking. Average order value is rounded to integer paise. SQL aggregates item snapshots, and order totals are counted once rather than multiplied by item joins. Empty datasets return zeros and empty chart arrays. Synthetic fixture orders and visitor demo orders are never real business sales.

## Errors

Errors have `{ "error": { "code": "invalid_request", "message": "A helpful explanation." } }`. Common statuses: `400` input validation, `403` cross-origin checkout, `404` unknown route/product, `405` wrong method (with `Allow`), `409` reused checkout key with a different payload, `413` body too large, `415` incorrect content type, `503` unavailable database. An order timeout may have committed: retry the same checkout key to discover the saved outcome safely.

## Implementation and checks

`src/main.py` handles HTTP, D1 bindings and structured errors. `src/store.py` holds validation and parameterized SQL. `tests/test_orders.py` runs the same service SQL against SQLite and the real migration, covering invalid inputs, database prices, snapshots, retries, conflicting and simultaneous submissions, rollback after a failed batch statement, analytics source separation and search filters. Real Worker/browser checks complement these tests to verify the D1 bridge and deployed assets.

The runtime follows Cloudflare's [Python Worker examples](https://developers.cloudflare.com/workers/languages/python/examples/) and [D1 binding API](https://developers.cloudflare.com/d1/worker-api/). D1 documents [transactional batch behavior](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch); the app relies on that rollback guarantee and the unique SQL key together.
