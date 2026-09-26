-- Keep incomplete source listings browseable without inventing prices.
ALTER TABLE products ADD COLUMN brand TEXT;
ALTER TABLE products ADD COLUMN subcategory TEXT;
ALTER TABLE products ADD COLUMN mrp_minor INTEGER CHECK(mrp_minor IS NULL OR (typeof(mrp_minor) = 'integer' AND mrp_minor > 0));
ALTER TABLE products ADD COLUMN discount_percent INTEGER CHECK(discount_percent IS NULL OR discount_percent BETWEEN 0 AND 100);
ALTER TABLE products ADD COLUMN material TEXT;
ALTER TABLE products ADD COLUMN fit TEXT;
ALTER TABLE products ADD COLUMN rating_value REAL CHECK(rating_value IS NULL OR rating_value >= 0);
ALTER TABLE products ADD COLUMN rating_scale REAL CHECK(rating_scale IS NULL OR rating_scale > 0);
ALTER TABLE products ADD COLUMN rating_count INTEGER CHECK(rating_count IS NULL OR rating_count >= 0);
ALTER TABLE products ADD COLUMN source_store TEXT;
ALTER TABLE products ADD COLUMN source_product_id TEXT;
ALTER TABLE products ADD COLUMN canonical_url TEXT;
ALTER TABLE products ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'existing';
ALTER TABLE products ADD COLUMN missing_fields TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(missing_fields) AND json_type(missing_fields) = 'array');

CREATE UNIQUE INDEX idx_products_source_url ON products(canonical_url) WHERE canonical_url IS NOT NULL;
CREATE UNIQUE INDEX idx_products_source_identity ON products(source_store,source_product_id)
    WHERE source_store IS NOT NULL AND source_product_id IS NOT NULL;

CREATE TABLE source_products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
    category TEXT NOT NULL CHECK(category IN ('men','women','kids','accessories','footwear')),
    description TEXT,
    price_minor INTEGER CHECK(price_minor IS NULL),
    image TEXT NOT NULL,
    alt TEXT NOT NULL,
    sizes TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(sizes) AND json_type(sizes) = 'array'),
    colors TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(colors) AND json_type(colors) = 'array'),
    featured INTEGER NOT NULL DEFAULT 0 CHECK(featured IN (0,1)),
    brand TEXT,
    subcategory TEXT,
    mrp_minor INTEGER CHECK(mrp_minor IS NULL OR (typeof(mrp_minor) = 'integer' AND mrp_minor > 0)),
    discount_percent INTEGER CHECK(discount_percent IS NULL OR discount_percent BETWEEN 0 AND 100),
    material TEXT,
    fit TEXT,
    rating_value REAL CHECK(rating_value IS NULL OR rating_value >= 0),
    rating_scale REAL CHECK(rating_scale IS NULL OR rating_scale > 0),
    rating_count INTEGER CHECK(rating_count IS NULL OR rating_count >= 0),
    source_store TEXT NOT NULL,
    source_product_id TEXT,
    canonical_url TEXT NOT NULL UNIQUE,
    verification_status TEXT NOT NULL,
    missing_fields TEXT NOT NULL CHECK(json_valid(missing_fields) AND json_type(missing_fields) = 'array')
);
CREATE UNIQUE INDEX idx_source_products_identity ON source_products(source_store,source_product_id)
    WHERE source_product_id IS NOT NULL;
CREATE INDEX idx_source_products_category ON source_products(category,name COLLATE NOCASE);

CREATE VIEW catalog_products AS
SELECT id,name,category,description,price_minor,image,alt,sizes,colors,featured,
       brand,subcategory,mrp_minor,discount_percent,material,fit,rating_value,rating_scale,rating_count,
       source_store,source_product_id,canonical_url,verification_status,missing_fields
FROM products
UNION ALL
SELECT id,name,category,description,price_minor,image,alt,sizes,colors,featured,
       brand,subcategory,mrp_minor,discount_percent,material,fit,rating_value,rating_scale,rating_count,
       source_store,source_product_id,canonical_url,verification_status,missing_fields
FROM source_products;
