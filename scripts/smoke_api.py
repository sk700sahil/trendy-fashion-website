"""Exercise the real running Python Worker/D1 API, locally or on workers.dev.

Creates exactly one visitor demo order per run; no personal information or payment.
"""
import argparse
import concurrent.futures
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def run(base):
    base = base.rstrip("/")

    def call(path, body=None, key=None, method=None, extra_headers=None):
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        headers.update(extra_headers or {})
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        try:
            response = urlopen(request, timeout=60)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            try:
                payload = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AssertionError(
                    f"{method or ('POST' if body is not None else 'GET')} {path} returned "
                    f"HTTP {response.status} with a non-JSON response: {content[:300]!r}"
                ) from error
            return response.status, payload

    status, catalog = call("/api/products")
    assert status == 200 and len(catalog["products"]) == 58, (status, catalog)
    products = catalog["products"]
    source_products = [item for item in products if item["id"].startswith("source-")]
    assert len(source_products) == 33
    assert all(isinstance(item["price_minor"], int) and item["price_minor"] > 0 for item in products)
    assert all(item["image"].startswith("https://") for item in source_products)
    for retired in ("source-footwear-004", "source-men-001"):
        assert call("/api/products/" + retired)[0] == 404
        assert call("/api/orders", {"items": [{"product_id": retired, "quantity": 1}]}, str(uuid4()))[0] == 400
    product = products[0]
    status, detail = call("/api/products/" + product["id"])
    assert status == 200 and detail["product"] == product
    for related in detail["related"]:
        assert related["category"] == product["category"] and related["id"] != product["id"]
    for image in sorted({item["image"] for item in products}):
        # Public retailer CDN images are verified in the Chrome suite, under the site CSP.
        if image.startswith("https://"):
            continue
        asset_request = Request(base + image, headers={
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        })
        with urlopen(asset_request, timeout=30) as response:
            assert response.status == 200 and response.headers["Content-Type"].startswith("image/")
    status, filtered = call("/api/products?category=men&sort=price_asc&max_price=250000")
    assert status == 200
    assert all(p["category"] == "men" and p["price_minor"] <= 250000 for p in filtered["products"])
    prices = [p["price_minor"] for p in filtered["products"]]
    assert prices == sorted(prices)
    assert call("/api/products?q=doesnotexistzzzz")[1]["total"] == 0
    assert call("/api/products/missing")[0] == 404
    assert call("/api/products?sort=invalid")[0] == 400
    assert call("/api/products", method="POST")[0] == 405

    _, before = call("/api/analytics?source=visitor")
    item = {"product_id": product["id"], "quantity": 2,
            "size": (product["sizes"] or [""])[0], "color": (product["colors"] or [""])[0]}
    payload = {"items": [item]}
    for quantity in (0, -1, 11, 1.5, True, "2"):
        assert call("/api/orders", {"items": [{**item, "quantity": quantity}]}, str(uuid4()))[0] == 400
    assert call("/api/orders", {"items": [{**item, "product_id": "missing"}]}, str(uuid4()))[0] == 400
    assert call("/api/orders", {"items": [{**item, "price_minor": 1}]}, str(uuid4()))[0] == 400
    assert call("/api/orders", payload)[0] == 400
    assert call("/api/orders", payload, str(uuid4()), extra_headers={"Origin": "https://example.com"})[0] == 403
    key = str(uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: call("/api/orders", payload, key), range(2)))
    assert sorted(status for status, _ in results) == [200, 201], results
    first = results[0][1]["order"]
    assert all(result["order"]["id"] == first["id"] for _, result in results)
    assert first["total_minor"] == product["price_minor"] * 2
    assert first["source"] == "visitor"
    status, replay = call("/api/orders", payload, key)
    assert status == 200 and replay["replayed"] and replay["order"]["id"] == first["id"]
    assert call("/api/orders", {"items": [{**item, "quantity": 1}]}, key)[0] == 409
    _, after = call("/api/analytics?source=visitor")
    assert after["summary"]["orders"] == before["summary"]["orders"] + 1
    assert after["summary"]["revenue_minor"] == before["summary"]["revenue_minor"] + first["total_minor"]
    _, synthetic = call("/api/analytics?source=synthetic")
    assert synthetic["summary"]["orders"] > 0 and len(synthetic["monthly"]) >= 6
    assert call("/api/analytics?source=invalid")[0] == 400
    print(f"PASS: {len(products)} products/images; filtering; validation; concurrent/replayed checkout; SQL analytics.")
    print(f"Verified {base}; created visitor demo order {first['id']} ({first['total_minor']} paise).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    run(parser.parse_args().base_url)
