-- D1 / SQLite schema. Every monetary value is an integer number of paise.
CREATE TABLE products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
    category TEXT NOT NULL CHECK(category IN ('men', 'women', 'kids', 'accessories', 'footwear')),
    description TEXT NOT NULL,
    price_minor INTEGER NOT NULL CHECK(typeof(price_minor) = 'integer' AND price_minor > 0 AND price_minor <= 100000000),
    image TEXT NOT NULL,
    alt TEXT NOT NULL,
    sizes TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(sizes) AND json_type(sizes) = 'array'),
    colors TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(colors) AND json_type(colors) = 'array'),
    featured INTEGER NOT NULL DEFAULT 0 CHECK(featured IN (0, 1))
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('synthetic', 'visitor')),
    total_minor INTEGER NOT NULL CHECK(typeof(total_minor) = 'integer' AND total_minor >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE order_items (
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL CHECK(line_no >= 1),
    product_id TEXT NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('men', 'women', 'kids', 'accessories', 'footwear')),
    unit_price_minor INTEGER NOT NULL CHECK(typeof(unit_price_minor) = 'integer' AND unit_price_minor > 0),
    quantity INTEGER NOT NULL CHECK(typeof(quantity) = 'integer' AND quantity BETWEEN 1 AND 10),
    size TEXT,
    color TEXT,
    PRIMARY KEY (order_id, line_no)
);

CREATE INDEX idx_products_category_price ON products(category, price_minor);
CREATE INDEX idx_products_price ON products(price_minor);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_source_created_at ON orders(source, created_at);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_order_items_category ON order_items(category);
