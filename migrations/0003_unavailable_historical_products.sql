-- Preserve order-history product rows while hiding prices that are no longer verified.
CREATE TABLE unavailable_products (
    id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    reason TEXT NOT NULL
);

DROP VIEW catalog_products;
CREATE VIEW catalog_products AS
SELECT p.id,p.name,p.category,p.description,
       CASE WHEN u.id IS NOT NULL THEN NULL ELSE p.price_minor END AS price_minor,
       p.image,p.alt,p.sizes,p.colors,p.featured,
       p.brand,p.subcategory,p.mrp_minor,p.discount_percent,p.material,p.fit,p.rating_value,p.rating_scale,p.rating_count,
       p.source_store,p.source_product_id,p.canonical_url,p.verification_status,p.missing_fields
FROM products p LEFT JOIN unavailable_products u ON u.id=p.id
UNION ALL
SELECT id,name,category,description,price_minor,image,alt,sizes,colors,featured,
       brand,subcategory,mrp_minor,discount_percent,material,fit,rating_value,rating_scale,rating_count,
       source_store,source_product_id,canonical_url,verification_status,missing_fields
FROM source_products;
