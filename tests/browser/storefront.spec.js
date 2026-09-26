const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

async function loaded(page) {
  await expect(page.locator('main h1')).toBeVisible();
  await expect(page.locator('main .loading')).toHaveCount(0);
}

test('pages, local images, links, and original portraits work', async ({ page, request }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const route of ['/', '/pages/explore.html', '/pages/trends.html', '/pages/about.html', '/pages/contact.html', '/pages/privacy.html', '/pages/terms.html', '/pages/analytics.html']) {
    const response = await page.goto(route);
    expect(response.status()).toBe(200);
    await loaded(page);
    await page.locator('img').evaluateAll(images => images.forEach(image => image.loading = 'eager'));
    await expect.poll(() => page.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBeTruthy();
    await expect(page.locator('input[type=email],input[type=password],input[type=tel]')).toHaveCount(0);
  }
  await page.goto('/pages/about.html');
  for (const photo of ['sahil.jpg', 'sahil1.jpg', 'sahil.png']) await expect(page.locator(`img[src$="/${photo}"]`)).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('search, category, price filters, sorting, empty states, and reset', async ({ page, request }) => {
  await page.goto('/pages/explore.html');
  await expect(page.locator('.product-card')).toHaveCount(86);
  await page.getByLabel('Collection', { exact: true }).selectOption('men');
  await expect(page.locator('.product-card')).toHaveCount(21);
  await expect(page.locator('main h1')).toHaveText("The men's edit.");
  for (const [category, count] of Object.entries({ women: 20, kids: 16, footwear: 15, accessories: 14 })) {
    await page.getByLabel('Collection', { exact: true }).selectOption(category);
    await expect(page.locator('.product-card')).toHaveCount(count);
  }
  await page.getByLabel('Collection', { exact: true }).selectOption('');
  await expect(page.locator('.product-card')).toHaveCount(86);
  await page.getByLabel('Collection', { exact: true }).selectOption('men');
  await page.getByLabel('Max price').fill('2000');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await page.getByLabel('Sort by').selectOption('price_desc');
  const expected = (await (await request.get('/api/products?category=men&max_price=200000&sort=price_desc')).json()).products;
  await expect(page.locator('.product-card h3')).toHaveText(expected.map(product => product.name));
  await page.getByLabel('Search products').fill('zzzznomatch');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('#result-count')).toHaveText('0 pieces to discover');
  await expect(page.locator('.empty-state')).toBeVisible();
  await page.locator('#empty-reset').click();
  await expect(page.locator('.product-card')).toHaveCount(86);
  await page.getByLabel('Search products').fill('denim');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  const matches = (await (await request.get('/api/products?q=denim&sort=featured')).json()).products;
  await expect(page.locator('.product-card h3')).toHaveText(matches.map(product => product.name));
  await page.getByLabel('Min price').fill('2000');
  await page.getByLabel('Max price').fill('1000');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page.locator('#filter-status')).toContainText('Minimum price');
});

test('all source listings are browsable and only verified-price products can be ordered', async ({ page, request }) => {
  const response = await request.get('/api/products');
  expect(response.status()).toBe(200);
  const { products } = await response.json();
  expect(products).toHaveLength(86);
  const expected = { men: 21, women: 20, kids: 16, footwear: 15, accessories: 14 };
  for (const [category, count] of Object.entries(expected)) {
    expect(products.filter(product => product.category === category)).toHaveLength(count);
  }
  expect(new Set(products.map(product => product.id)).size).toBe(86);
  expect(products.filter(product => !product.id.startsWith('source-'))).toHaveLength(25);
  expect(products.filter(product => product.price_minor != null).every(product => Number.isSafeInteger(product.price_minor) && product.price_minor > 0)).toBeTruthy();
  expect(products.filter(product => product.id.startsWith('source-') && product.price_minor != null)).toHaveLength(41);
  expect(products.filter(product => product.id.startsWith('source-') && product.price_minor == null)).toHaveLength(20);
  for (const productId of ['source-footwear-004', 'source-men-001']) {
    const response = await request.post('/api/orders', {
      data: { items: [{ product_id: productId, quantity: 1 }] },
      headers: { 'Idempotency-Key': randomUUID() },
    });
    expect(response.status()).toBe(400);
    expect((await response.json()).error.code).toBe('invalid_product');
  }

  await page.goto('/pages/product.html?id=source-footwear-003');
  await expect(page.locator('.detail-price')).toHaveText('Price unavailable');
  await expect(page.locator('#add-form')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'View source listing ↗' })).toHaveAttribute('target', '_blank');
  await expect(page.locator('.detail-image img')).toHaveJSProperty('complete', true);
  expect(await page.locator('.detail-image img').evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
  await expect(page.locator('.detail-image img')).toHaveAttribute('src', /product-placeholder\.svg/);

  const inaccessiblePrice = products.find(product => product.id === 'source-men-001');
  expect(inaccessiblePrice.price_minor).toBeNull();
  expect(inaccessiblePrice.mrp_minor).toBeGreaterThan(0);
  await page.goto('/pages/product?id=source-men-001');
  await expect(page.locator('.detail-price')).toHaveText('Price unavailable');
  await expect(page.locator('.detail-copy > .product-category')).toHaveCount(0);
  await expect(page.locator('.notice')).toContainText('verified current price is unavailable');
  await expect(page.locator('#add-form')).toHaveCount(0);

  const footwear = products.find(product => product.id === 'source-footwear-004');
  expect(footwear.price_minor).toBeNull();
  expect(footwear.image).toBe('/assets/images/product-placeholder.svg');
  expect(footwear.description).toBeNull();
  expect(footwear.mrp_minor).toBeNull();
  expect(footwear.sizes).toEqual([]);
  expect(footwear.colors).toEqual([]);
  await page.goto('/pages/product?id=source-footwear-004');
  await expect(page.locator('main h1')).toHaveText(footwear.name);
  await expect(page.locator('.detail-price')).toHaveText('Price unavailable');
  await expect(page.locator('.detail-description')).toContainText('The source did not provide a product description.');
  await expect(page.locator('.product-specs')).toContainText('Besroad');
  await expect(page.locator('.product-specs')).not.toContainText('Spandex upper; rubber sole');
  await expect(page.locator('#add-form')).toHaveCount(0);
  const image = page.locator('.detail-image img');
  await expect(image).toHaveAttribute('src', /product-placeholder\.svg/);
  await expect(page.getByRole('link', { name: 'View source listing ↗' })).toHaveAttribute('href', /B08ZCGXGTS/);
});

test('all 86 catalog images display or fall back to a working local placeholder', async ({ page }) => {
  await page.goto('/pages/explore.html');
  await loaded(page);
  await expect(page.locator('.product-card')).toHaveCount(86);
  await page.locator('.product-card img').evaluateAll(images => images.forEach(image => image.loading = 'eager'));
  await expect.poll(() => page.locator('.product-card img').evaluateAll(images =>
    images.length === 86 && images.every(image => image.complete && image.naturalWidth > 0)
  )).toBeTruthy();
  const fallbacks = await page.locator('.product-card img[src*="product-placeholder.svg"]').count();
  expect(fallbacks).toBeGreaterThanOrEqual(20);
});

test('all 41 imported retailer images load or fall back under the deployed CSP', async ({ page, request }) => {
  const { products } = await (await request.get('/api/products')).json();
  const images = products.filter(product => product.id.startsWith('source-') && product.image.startsWith('https://'));
  expect(images).toHaveLength(41);
  const response = await page.goto('/pages/explore.html');
  const responseCsp = response.headers()['content-security-policy'] || '';
  await loaded(page);
  await page.locator('.product-card img').evaluateAll(elements => elements.forEach(element => { element.loading = 'eager'; }));
  await expect.poll(() => page.locator('.product-card img').evaluateAll(elements =>
    elements.length === 86 && elements.every(element => element.complete && element.naturalWidth > 0)
  ), { timeout: 20000 }).toBeTruthy();
  for (const product of images) {
    expect(responseCsp, product.image).toContain(new URL(product.image).host);
    const image = page.locator(`.product-card a[href*="${product.id}"] img`);
    await expect(image).toHaveCount(1);
    const shown = await image.evaluate(element => ({ src: element.getAttribute('src'), alt: element.alt }));
    if (shown.src !== product.image) {
      expect(shown.src).toContain('/assets/images/product-placeholder.svg');
      expect(shown.alt).toBe('Product image unavailable');
    }
  }
});

test('a failed retailer image falls back to a loaded local placeholder', async ({ page, request }) => {
  const { products } = await (await request.get('/api/products')).json();
  const product = products.find(item => item.id.startsWith('source-') && item.image.startsWith('https://'));
  expect(product).toBeTruthy();
  await page.route(product.image, route => route.abort());
  await page.goto(`/pages/product.html?id=${encodeURIComponent(product.id)}`);
  await loaded(page);
  const image = page.locator('.detail-image img');
  await expect(image).toHaveAttribute('src', /product-placeholder\.svg/);
  await expect(image).toHaveAttribute('alt', 'Retailer product image unavailable; placeholder shown');
  await expect.poll(() => image.evaluate(img => img.complete && img.naturalWidth > 0)).toBeTruthy();
});

test('home to persisted bag, checkout, confirmation, and updated analytics', async ({ page, request }) => {
  const before = (await (await request.get('/api/analytics?source=visitor')).json()).summary.orders;
  await page.goto('/');
  await page.getByRole('link', { name: 'Explore the collection', exact: false }).first().click();
  await expect(page.locator('.product-card')).toHaveCount(86);
  await page.locator('.product-card h3 a').first().click();
  await expect(page.locator('#add-form')).toBeVisible();
  await page.getByLabel('Quantity', { exact: true }).fill('2');
  await page.getByRole('button', { name: 'Add to bag' }).click();
  await expect(page.locator('#cart-count')).toHaveText('2');
  await page.getByRole('button', { name: 'Add to bag' }).click();
  await expect(page.locator('#cart-count')).toHaveText('4');
  await page.getByRole('link', { name: 'View your bag' }).click();
  await loaded(page);
  await expect(page.locator('.cart-item')).toHaveCount(1);
  await page.reload();
  await expect(page.locator('#cart-count')).toHaveText('4');
  await page.getByRole('button', { name: /Increase quantity/ }).click();
  await expect(page.locator('#cart-count')).toHaveText('5');
  await expect(page.getByRole('button', { name: /Decrease quantity/ })).toBeVisible();
  await page.getByRole('button', { name: /Decrease quantity/ }).click();
  await expect(page.locator('#cart-count')).toHaveText('4');
  await page.getByRole('link', { name: 'Continue to demo checkout' }).click();
  await expect(page.locator('main')).toContainText('Demo order — no payment or delivery');
  await expect(page.locator('main input')).toHaveCount(0);
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page).toHaveURL(/confirmation(?:\.html)?(?:\?|$)/);
  await expect(page.locator('.order-reference')).toContainText('tt-');
  await expect(page.locator('#cart-count')).toHaveText('0');
  await page.reload();
  await expect(page.locator('.order-reference')).toContainText('tt-');
  await page.getByRole('link', { name: 'See your impact in the data' }).click();
  await expect(page.locator('.stat').first().locator('strong')).toHaveText(String(before + 1));
  await page.getByLabel('Order source').selectOption('synthetic');
  await expect(page.locator('.stat').first().locator('strong')).toHaveText('36');
  await page.getByText('View monthly figures', { exact: true }).click();
  await expect(page.locator('details tbody tr')).toHaveCount(6);
});

test('lost checkout response retries the same saved order after refresh', async ({ page, request }) => {
  const before = (await (await request.get('/api/analytics?source=visitor')).json()).summary.orders;
  await page.goto('/pages/product.html?id=men-overshirt');
  await page.getByRole('button', { name: 'Add to bag' }).click();
  await page.goto('/pages/checkout.html');
  let firstKey;
  await page.route('**/api/orders', async route => {
    firstKey = route.request().headers()['idempotency-key'];
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    await route.abort('failed');
  }, { times: 1 });
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page.locator('#checkout-status')).toContainText('Retry');
  await page.reload();
  await expect(page.locator('main')).toContainText('earlier checkout is awaiting confirmation');
  const pending = await page.evaluate(() => JSON.parse(localStorage.getItem('trendy-threads-pending-v1')));
  expect(pending.key).toBe(firstKey);
  await page.getByRole('button', { name: 'Retry pending demo order' }).click();
  await expect(page).toHaveURL(/confirmation(?:\.html)?(?:\?|$)/);
  const orderId = await page.locator('.order-reference').innerText();
  const replay = await request.post('/api/orders', {
    data: { items: pending.items },
    headers: { 'Idempotency-Key': firstKey },
  });
  expect(replay.status()).toBe(200);
  const replayBody = await replay.json();
  expect(replayBody.replayed).toBe(true);
  expect(orderId).toContain(replayBody.order.id);
  const after = (await (await request.get('/api/analytics?source=visitor')).json()).summary.orders;
  expect(after).toBe(before + 1);
});

test('bag removal and recoverable catalog error', async ({ page }) => {
  await page.goto('/pages/product.html?id=men-overshirt');
  await page.getByRole('button', { name: 'Add to bag' }).click();
  await page.getByRole('link', { name: 'View your bag' }).click();
  await page.getByRole('button', { name: /^Remove / }).click();
  await expect(page.locator('.empty-state')).toBeVisible();
  await page.route('**/api/products?*', route => route.abort());
  await page.goto('/pages/explore.html');
  await expect(page.getByRole('alert')).toContainText('could not reach');
  await page.unroute('**/api/products?*');
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.locator('.product-card')).toHaveCount(86);
});

test('corrupted browser storage recovers to an empty bag with a clear notice', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('trendy-threads-cart-v1', '{broken json'));
  await page.goto('/pages/cart.html');
  await loaded(page);
  await expect(page.locator('.empty-state')).toBeVisible();
  await expect(page.locator('.storage-warning')).toContainText('Browser storage is unavailable');
  await expect(page.locator('#cart-count')).toHaveText('0');
  expect(errors).toEqual([]);
});

test('legacy page names redirect and unknown pages have a useful 404', async ({ request, page }) => {
  const redirects = fs.readFileSync(path.join(__dirname, '../../public/_redirects'), 'utf8').trim().split(/\r?\n/);
  for (const line of redirects) {
    if (line.startsWith('#') || !line.trim()) continue;
    const [source, target] = line.trim().split(/\s+/);
    const response = await request.get(source, { maxRedirects: 0 });
    expect(response.status(), source).toBe(301);
    expect(new URL(response.headers().location, response.url()).pathname).toBe(new URL(target, response.url()).pathname);
  }
  const response = await page.goto('/not-a-real-page');
  expect(response.status()).toBe(404);
  await expect(page.getByRole('heading', { name: 'Let’s find your way.' })).toBeVisible();
});

test('mobile layout and keyboard navigation', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  for (const route of ['/', '/pages/explore.html', '/pages/product.html?id=men-overshirt', '/pages/analytics.html', '/pages/about.html', '/pages/trends.html']) {
    await page.goto(route);
    await loaded(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), route).toBe(true);
  }
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto('/pages/product.html?id=source-men-001');
  await loaded(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.locator('#main-nav')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused();
  await expect(page.locator('#main-nav')).toBeHidden();
});

test('save real desktop, mobile, and analytics screenshots', async ({ page }) => {
  const directory = path.join(__dirname, '../../docs/screenshots');
  fs.mkdirSync(directory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await loaded(page);
  await page.locator('img').evaluateAll(images => images.forEach(image => image.loading = 'eager'));
  await expect.poll(() => page.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBeTruthy();
  await page.screenshot({ path: path.join(directory, 'home-desktop.png'), fullPage: true });
  await page.goto('/pages/analytics.html?source=synthetic');
  await loaded(page);
  await page.screenshot({ path: path.join(directory, 'analytics.png'), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await loaded(page);
  await page.screenshot({ path: path.join(directory, 'home-mobile.png'), fullPage: true });
});
