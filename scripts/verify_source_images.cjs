// Verify candidate retailer images through the project's restrictive img-src CSP.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
function arg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? path.resolve(process.argv[index + 1]) : path.join(root, fallback);
}
const inputFile = arg('--input', 'data/trendy_threads_products_source.json');
const pagesFile = arg('--checks', 'data/source_page_checks.json');
const outputFile = arg('--output', 'data/source_image_checks.json');
const headersFile = path.join(root, 'public', '_headers');
const source = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
const pageChecks = JSON.parse(fs.readFileSync(pagesFile, 'utf8'));
const headerText = fs.readFileSync(headersFile, 'utf8');
const csp = headerText.match(/^\s*Content-Security-Policy:\s*(.+)$/mi)?.[1]?.trim();
if (!csp || !Array.isArray(source.products)) throw new Error('Input products or CSP is missing.');
const allowedHosts = new Set((csp.match(/img-src\s+([^;]+)/)?.[1] || '').split(/\s+/).filter(value => value.startsWith('https://')).map(value => new URL(value).hostname));
const pageById = new Map(pageChecks.checks.map(check => [check.id, check]));

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage();
  const responses = new Map();
  page.on('response', response => {
    if (response.request().resourceType() === 'image') {
      responses.set(response.request().url(), { status: response.status(), content_type: response.headers()['content-type'] || null });
    }
  });
  await page.setContent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="${csp.replaceAll('"', '&quot;')}"><title>Image verifier</title>`);
  const checks = [];
  for (const product of source.products) {
    const pageCheck = pageById.get(product.id) || {};
    const pageIdentity = pageCheck.status === 'accessible' && pageCheck.identity_match === true &&
      normalize(pageCheck.source_url) === normalize(product.canonical_url) && pageCheck.verified_fields?.includes('image');
    let selected = null;
    for (const imageUrl of product.image_urls || []) {
      let host = '';
      try { host = new URL(imageUrl).hostname; } catch { /* record rejected below */ }
      if (!imageUrl.startsWith('https://') || !allowedHosts.has(host) || !pageIdentity) continue;
      responses.delete(imageUrl);
      const result = await page.evaluate(url => new Promise(resolve => {
        const image = new Image();
        const timer = setTimeout(() => resolve({ loaded: false, width: 0 }), 12000);
        image.onload = () => { clearTimeout(timer); resolve({ loaded: image.naturalWidth > 0, width: image.naturalWidth }); };
        image.onerror = () => { clearTimeout(timer); resolve({ loaded: false, width: 0 }); };
        image.src = url;
      }), imageUrl);
      const response = responses.get(imageUrl) || {};
      const mime = String(response.content_type || '').toLowerCase().split(';', 1)[0];
      const valid = result.loaded && result.width > 0 && response.status === 200 && mime.startsWith('image/');
      selected = {
        product_id: product.id, source_url: product.canonical_url, image_url: imageUrl,
        status: valid ? 'verified' : 'rejected', http_status: response.status ?? null,
        content_type: response.content_type || null, https: true, identity_match: pageIdentity,
        source_image_verified: pageCheck.verified_fields?.includes('image') === true,
        rendered: result.loaded && result.width > 0, csp_allowed: allowedHosts.has(host),
        failure_reason: valid ? null : (!allowedHosts.has(host) ? 'IMAGE_CSP_BLOCKED' : response.status !== 200 ? 'IMAGE_BROKEN' : 'IMAGE_INVALID_CONTENT'),
      };
      if (valid) break;
    }
    const firstImage = (product.image_urls || [])[0] || null;
    let failureReason = 'IMAGE_MISSING';
    if (!pageIdentity) failureReason = 'PRODUCT_MISMATCH';
    else if (firstImage) {
      try { if (!allowedHosts.has(new URL(firstImage).hostname)) failureReason = 'IMAGE_CSP_BLOCKED'; else failureReason = 'IMAGE_BROKEN'; }
      catch { failureReason = 'IMAGE_BROKEN'; }
    }
    checks.push(selected || {
      product_id: product.id, source_url: product.canonical_url,
      image_url: firstImage, status: 'rejected', http_status: null,
      content_type: null, https: false, identity_match: pageIdentity,
      source_image_verified: pageCheck.verified_fields?.includes('image') === true,
      rendered: false, csp_allowed: false,
      failure_reason: failureReason,
    });
  }
  const result = { checked_at: new Date().toISOString(), method: 'Chrome image load under the exact restrictive img-src policy in public/_headers; requires HTTPS, HTTP 200, image MIME, naturalWidth, and a matching product page.', checks };
  fs.writeFileSync(outputFile, JSON.stringify(result, null, 2) + '\n');
  const good = checks.filter(check => check.status === 'verified').length;
  console.log(`Image verification: ${good}/${checks.length} passed; ${checks.length - good} rejected. Report: ${path.relative(root, outputFile)}`);
  await browser.close();
  if (good !== checks.length) process.exitCode = 1;
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });

function normalize(value) {
  try {
    const url = new URL(value);
    url.hash = '';
    if (url.hostname.startsWith('www.')) url.hostname = url.hostname.slice(4);
    for (const key of [...url.searchParams.keys()]) {
      if (/^utm_/i.test(key) || /^(gclid|fbclid|mc_cid|mc_eid|ref|tag|linkcode|srsltid)$/i.test(key) || /^ref_/i.test(key)) {
        url.searchParams.delete(key);
      }
    }
    url.searchParams.sort();
    return url.toString().replace(/\/$/, '');
  } catch { return ''; }
}
