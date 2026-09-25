"""Cloudflare Python Worker entrypoint. Static files are served by ASSETS."""

import json
from urllib.parse import parse_qs, unquote, urlparse

from workers import Response, WorkerEntrypoint

from store import APIError, MAX_BODY_BYTES, analytics, create_order, get_product, list_products


def native(value):
    return value.to_py() if hasattr(value, "to_py") else value


def result_rows(result):
    # The current Workers SDK converts D1 responses to native dictionaries.
    # Older raw JS bindings expose a results attribute instead.
    result = native(result)
    return result["results"] if isinstance(result, dict) else native(result.results)


class D1Database:
    def __init__(self, binding):
        self.binding = binding

    def prepare(self, sql, values):
        statement = self.binding.prepare(sql)
        return statement.bind(*values) if values else statement

    async def all(self, sql, values=()):
        result = await self.prepare(sql, values).all()
        return result_rows(result)

    async def batch(self, statements):
        results = await self.binding.batch([self.prepare(sql, values) for sql, values in statements])
        return [result_rows(result) for result in results]


def json_response(payload, status=200, extra_headers=None):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if extra_headers:
        headers.update(extra_headers)
    return Response.json(payload, status=status, headers=headers)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path.rstrip("/") or "/"
        if not path.startswith("/api/") and path != "/api":
            return await self.env.ASSETS.fetch(request.js_object)
        try:
            params = {key: values[-1] for key, values in parse_qs(url.query, keep_blank_values=True).items()}
            db = D1Database(self.env.DB)
            method = request.method.upper()
            if path == "/api/products":
                allowed = "GET"
                if method == "GET":
                    return json_response(await list_products(db, params))
            elif path.startswith("/api/products/") and path.count("/") == 3:
                allowed = "GET"
                if method == "GET":
                    return json_response(await get_product(db, unquote(path.rsplit("/", 1)[-1])))
            elif path == "/api/analytics":
                allowed = "GET"
                if method == "GET":
                    return json_response(await analytics(db, params.get("source", "all")))
            elif path == "/api/orders":
                allowed = "POST"
                if method == "POST":
                    origin = request.headers.get("Origin")
                    if origin and origin.rstrip("/") != f"{url.scheme}://{url.netloc}":
                        raise APIError(403, "origin_not_allowed", "Create demo orders from this website.")
                    content_type = request.headers.get("Content-Type") or ""
                    if content_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise APIError(415, "unsupported_media_type", "Send this checkout as application/json.")
                    length = request.headers.get("Content-Length")
                    if length and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                        raise APIError(413, "request_too_large", "Checkout payload is too large.")
                    raw = await request.text()
                    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
                        raise APIError(413, "request_too_large", "Checkout payload is too large.")
                    try:
                        body = json.loads(raw)
                    except (ValueError, RecursionError):
                        raise APIError(400, "invalid_json", "Send valid JSON for this checkout.")
                    payload, status = await create_order(db, body, request.headers.get("Idempotency-Key"))
                    return json_response(payload, status)
            else:
                raise APIError(404, "not_found", "This API route does not exist.")
            return json_response({"error": {"code": "method_not_allowed", "message": f"Use {allowed} for this route."}}, 405, {"Allow": allowed})
        except APIError as error:
            return json_response({"error": {"code": error.code, "message": error.message}}, error.status)
        except Exception as error:
            # Keep request bodies and database details out of the public response.
            print(f"Trendy Threads API failed: {type(error).__name__}")
            return json_response({"error": {"code": "service_unavailable", "message": "The demo store is temporarily unavailable. Retry using the same checkout key."}}, 503)
