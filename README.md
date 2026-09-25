# Trendy Threads

A fashion catalog and data engineering portfolio built from the original Trendy Threads pages and photography. Browse 25 products, filter the catalog, keep a bag in browser storage, place a no-payment demo order, and see SQL-backed analytics update.

**Demo only. No real shop, account, inventory, payment, customer details, or delivery.** Prices, available sizes and colors, and synthetic orders are illustrative. Visitor orders are demonstrations and are never actual sales.

## Stack and flow

- Static storefront: accessible HTML, responsive CSS, and vanilla JavaScript in `public/`; all product photographs are local.
- API: Python 3.13+ Cloudflare Worker using the supported `pywrangler` tool.
- Database: Cloudflare D1 (SQLite). One SQL migration creates `products`, `orders`, and `order_items`; prices and order totals are stored as integer paise.
- Data prep: standard-library Python validates and cleans the curated CSV, then produces repeatable D1 SQL imports and a quality report.

`data/catalog_raw.csv` → `python scripts/clean_catalog.py` → quality report and cleaned catalog → idempotent D1 product seed → Python product API → saved visitor demo orders → SQL analytics API. A second, fixed SQL seed creates explicitly **synthetic** monthly fixtures. The [data flow and schema notes](docs/data-flow.md) describe the source images, validation, fixtures, and aggregate queries; [API details](docs/api.md) document endpoints and checkout guarantees.

## Run on Windows

Install Node.js 22+, Python 3.13+, and the free `uv` tool. Install Google Chrome for browser tests. From the project folder in PowerShell:

```powershell
python -m pip install uv
uv sync --locked
npm ci
python scripts/clean_catalog.py --check
```

Initialize this checkout's local D1 database and start the Python Worker with Workers Static Assets:

```powershell
.\scripts\worker.ps1 d1 migrations apply trendy-threads-db --local
.\scripts\worker.ps1 d1 execute trendy-threads-db --local --file=data/catalog_seed.sql
.\scripts\worker.ps1 d1 execute trendy-threads-db --local --file=data/synthetic_orders.sql
.\scripts\worker.ps1 dev --ip 127.0.0.1 --port 8787
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Keep the Worker running while you use the local site. The project-local launcher adds an existing `.tools/bin/uv.exe` to the temporary command path when available; `uv` can instead be installed normally. `wrangler.jsonc` points to the existing `trendy-threads-db` database. Local D1 state remains in the ignored `.wrangler/` folder and is not committed.

## Checks

```powershell
python -m unittest discover -s tests -v
python scripts/clean_catalog.py --check
python scripts/audit_site.py
node --check public/js/app.js
npm run test:browser
npm run test:api
```

Python tests use only the standard library and an in-memory SQLite database. Playwright runs the pages in installed Chrome against the local Worker, including product search/filter/sort, image and link checks, mobile layout, cart storage, checkout, a lost-response retry, and confirmation that retry creates no second order. Its API smoke script also submits **one new visitor demo order per run** to the selected Worker and verifies analytics. Run it against a different deployment with `python scripts/smoke_api.py --base-url https://your-worker.workers.dev`.

Representative storefront screenshots:

![Trendy Threads homepage on desktop](docs/screenshots/home-desktop.png)

![Trendy Threads homepage on mobile](docs/screenshots/home-mobile.png)

![Demo analytics dashboard](docs/screenshots/analytics.png)

## Existing Cloudflare account and deployment

The current `wrangler.jsonc` binds the existing `trendy-threads-db`; do not create another database. Install dependencies and sign in once with Cloudflare's browser login if Wrangler asks:

```powershell
.\scripts\worker.ps1 login
.\scripts\worker.ps1 d1 migrations apply trendy-threads-db --remote
.\scripts\worker.ps1 d1 execute trendy-threads-db --remote --file=data/catalog_seed.sql
.\scripts\worker.ps1 d1 execute trendy-threads-db --remote --file=data/synthetic_orders.sql
.\scripts\worker.ps1 deploy
```

All seed statements are repeatable; product rows are upserted and fixed synthetic orders are preserved on reimport. These commands use the already configured Cloudflare account and database, and the default `workers.dev` address; they do not configure a paid plan, custom domain, payment service, or secret. Static assets and the Python API are served by the deployed Worker, so the local computer does not need to remain on.

**Deployment status:** Deployed to [trendy-threads.sk73sahil.workers.dev](https://trendy-threads.sk73sahil.workers.dev). Public Chrome checks cover catalog/filtering, product images and pages, cart persistence, demo checkout and idempotent retry, analytics, redirects, and 404 handling.
