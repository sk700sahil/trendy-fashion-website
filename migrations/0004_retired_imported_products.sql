-- Remove failed imported products from the live catalog without deleting order history.
CREATE TABLE retired_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_url TEXT,
    failure_reason TEXT NOT NULL,
    retired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DROP VIEW catalog_products;
CREATE VIEW catalog_products AS
SELECT p.id,p.name,p.category,p.description,
       CASE WHEN u.id IS NOT NULL THEN NULL ELSE p.price_minor END AS price_minor,
       p.image,p.alt,p.sizes,p.colors,p.featured,
       p.brand,p.subcategory,p.mrp_minor,p.discount_percent,p.material,p.fit,p.rating_value,p.rating_scale,p.rating_count,
       p.source_store,p.source_product_id,p.canonical_url,p.verification_status,p.missing_fields
FROM products p
LEFT JOIN unavailable_products u ON u.id=p.id
LEFT JOIN retired_products r ON r.product_id=p.id
WHERE r.product_id IS NULL
UNION ALL
SELECT s.id,s.name,s.category,s.description,s.price_minor,s.image,s.alt,s.sizes,s.colors,s.featured,
       s.brand,s.subcategory,s.mrp_minor,s.discount_percent,s.material,s.fit,s.rating_value,s.rating_scale,s.rating_count,
       s.source_store,s.source_product_id,s.canonical_url,s.verification_status,s.missing_fields
FROM source_products s
LEFT JOIN retired_products r ON r.product_id=s.id
WHERE r.product_id IS NULL;
