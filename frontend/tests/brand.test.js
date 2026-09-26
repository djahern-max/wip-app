// Brand assets (F05.2), statically: the head of index.html, the three static pages, the
// Logo component, the constants, and the committed outputs of `npm run brand` (their
// dimensions, the ICO container, and that brand-manifest.json still matches the sources).
// node --test, no dependency: PNG and ICO headers are read by hand, and the script's pure
// parts are imported without loading sharp.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { PRODUCT_NAME, SITE_HOST } from "../src/product.js";
import { fillProduct } from "../scripts/product-html.mjs";
import {
  OG_HEIGHT,
  OG_WIDTH,
  OUTPUTS,
  SOURCES,
  ico,
  iconSvg,
  ogSvg,
  parseSvg,
  readConstant,
} from "../scripts/make-brand.mjs";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = join(FRONTEND, "public");
const read = (rel) => readFileSync(join(FRONTEND, rel));
const text = (rel) => read(rel).toString("utf8");
const squash = (s) => s.split(/\s+/).join(" ");
const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

// D-33: the name and the hostname are one string, taken from the constants, never typed here.
const HOST = SITE_HOST;
const brand = JSON.parse(text("public/brand/brand.json"));

const ICON_LINKS = [
  '<link rel="icon" href="/favicon.ico" sizes="48x48" />',
  '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />',
  '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />',
  '<link rel="manifest" href="/site.webmanifest" />',
];

const PREVIEW = [
  `<meta name="theme-color" content="${brand.background}" />`,
  '<meta property="og:type" content="website" />',
  `<meta property="og:site_name" content="${PRODUCT_NAME}" />`,
  `<meta property="og:title" content="${PRODUCT_NAME}" />`,
  '<meta property="og:description" content="Job cost and WIP reporting for contractors, operated by a CPA practice." />',
  `<meta property="og:url" content="https://${HOST}/" />`,
  `<meta property="og:image" content="https://${HOST}/og-image.png" />`,
  '<meta property="og:image:width" content="1200" />',
  '<meta property="og:image:height" content="630" />',
  '<meta name="twitter:card" content="summary_large_image" />',
];

function png(rel) {
  const buf = read(rel);
  assert.equal(buf.subarray(0, 8).toString("hex"), "89504e470d0a1a0a", `${rel} is a PNG`);
  assert.equal(buf.subarray(12, 16).toString("ascii"), "IHDR");
  return {
    width: buf.readUInt32BE(16),
    height: buf.readUInt32BE(20),
    colourType: buf[25], // 2 = RGB (no alpha channel), 6 = RGBA
  };
}

// --- constants and the component -----------------------------------------------------

test("product.js exports SITE_HOST as the hostname", () => {
  assert.equal(readConstant(text("src/product.js"), "SITE_HOST"), HOST);
});

test("Logo.jsx shows /brand/logo.svg and no .jsx file inlines an <svg>", () => {
  assert.match(text("src/Logo.jsx"), /src="\/brand\/logo\.svg"/);
  const walk = (dir) =>
    readdirSync(join(FRONTEND, dir), { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? walk(join(dir, e.name)) : e.name.endsWith(".jsx") ? [join(dir, e.name)] : [],
    );
  const inlined = walk("src").filter((rel) => text(rel).includes("<svg"));
  assert.deepEqual(inlined, []);
});

// --- index.html ----------------------------------------------------------------------

test("index.html, with its placeholders filled, carries the title, the icon links and the preview set", () => {
  const source = text("index.html");
  assert.ok(source.includes("%PRODUCT_NAME%") && source.includes("%SITE_HOST%"), "the placeholders (D-33)");
  assert.ok(!source.includes(PRODUCT_NAME), "the name is not typed in index.html (D-33)");
  const html = squash(fillProduct(source));
  assert.ok(!/%[A-Z_]+%/.test(html), "every placeholder filled");
  assert.ok(html.includes(`<title>${PRODUCT_NAME}</title>`));
  for (const tag of [...ICON_LINKS, ...PREVIEW]) assert.ok(html.includes(tag), tag);
});

test("index.html has no inline script, no <style> and no style= (the CSP stands)", () => {
  const html = text("index.html");
  const scripts = [...html.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.ok(scripts.length > 0 && scripts.every((s) => /\ssrc=/.test(s)), scripts.join(" "));
  assert.ok(!/<style\b/.test(html));
  assert.ok(!/\sstyle=/.test(html));
});

// --- the three static pages ----------------------------------------------------------

for (const page of ["privacy.html", "terms.html", "qbo-disconnected.html"]) {
  test(`${page} carries the icon links and one logo image, and no og: set`, () => {
    const html = squash(text(`public/${page}`));
    for (const tag of ICON_LINKS) assert.ok(html.includes(tag), tag);
    assert.equal(html.split('<img src="/brand/logo.svg"').length - 1, 1);
    assert.ok(html.includes('<main> <img src="/brand/logo.svg" alt="" width="28" height="28" class="logo" /> <h1>'));
    assert.ok(!html.includes("og:"));
  });
}

// --- the generator's pure parts ------------------------------------------------------

test("the OG image's SVG source names SITE_HOST once, in the foreground colour", () => {
  const svg = ogSvg(parseSvg(text(SOURCES.logo)), HOST, brand);
  assert.equal(svg.split(HOST).length - 1, 1);
  assert.ok(svg.includes(`width="${OG_WIDTH}" height="${OG_HEIGHT}"`));
  assert.ok(svg.includes(`fill="${brand.foreground}"`));
  assert.ok(svg.includes(`fill="${brand.background}"`));
  assert.ok(svg.includes("'Helvetica Neue', Helvetica, Arial, sans-serif"));
});

test("an icon SVG insets the logo by the padding and keeps its viewBox", () => {
  const logo = parseSvg(text(SOURCES.logo));
  const svg = iconSvg(logo, 180, brand.background, 0.15);
  assert.ok(svg.includes('<svg x="27" y="27" width="126" height="126" viewBox="0 0 74 74"'));
  assert.ok(svg.includes(`<rect width="180" height="180" fill="${brand.background}"/>`));
  const bare = iconSvg(logo, 16, null, 0);
  assert.ok(!bare.includes("<rect"));
});

test("parseSvg refuses a document without a viewBox", () => {
  assert.throws(() => parseSvg('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'), /viewBox/);
});

test("the ICO container lists its PNG entries with the right offsets", () => {
  const a = Buffer.from("aaaa");
  const b = Buffer.from("bbbbbbbb");
  const buf = ico([
    { size: 16, png: a },
    { size: 32, png: b },
  ]);
  assert.equal(buf.readUInt16LE(2), 1);
  assert.equal(buf.readUInt16LE(4), 2);
  assert.equal(buf[6], 16);
  assert.equal(buf.readUInt32LE(6 + 8), 4);
  assert.equal(buf.readUInt32LE(6 + 12), 6 + 32);
  assert.equal(buf[6 + 16], 32);
  assert.equal(buf.readUInt32LE(6 + 16 + 12), 6 + 32 + 4);
  assert.equal(buf.subarray(6 + 32).toString(), "aaaabbbbbbbb");
});

// --- the committed outputs -----------------------------------------------------------

test("every output of npm run brand is committed", () => {
  for (const name of OUTPUTS) assert.ok(existsSync(join(PUBLIC, name)), name);
});

test("favicon.svg is the logo byte for byte", () => {
  assert.ok(read("public/favicon.svg").equals(read(SOURCES.logo)));
});

test("the raster outputs have the stated sizes; the touch icon has no alpha channel", () => {
  assert.deepEqual(png("public/favicon-16.png"), { width: 16, height: 16, colourType: 6 });
  assert.deepEqual(png("public/favicon-32.png"), { width: 32, height: 32, colourType: 6 });
  assert.deepEqual(png("public/favicon-48.png"), { width: 48, height: 48, colourType: 6 });
  assert.deepEqual(png("public/apple-touch-icon.png"), { width: 180, height: 180, colourType: 2 });
  assert.deepEqual(png("public/icon-192.png"), { width: 192, height: 192, colourType: 2 });
  assert.deepEqual(png("public/icon-512.png"), { width: 512, height: 512, colourType: 2 });
  assert.deepEqual(png("public/icon-512-maskable.png"), { width: 512, height: 512, colourType: 2 });
  assert.deepEqual(png("public/og-image.png"), { width: OG_WIDTH, height: OG_HEIGHT, colourType: 2 });
});

test("favicon.ico holds the 16, 32 and 48 px PNGs", () => {
  const buf = read("public/favicon.ico");
  assert.equal(buf.readUInt16LE(2), 1);
  assert.equal(buf.readUInt16LE(4), 3);
  const sizes = [];
  for (let i = 0; i < 3; i += 1) {
    const o = 6 + 16 * i;
    const length = buf.readUInt32LE(o + 8);
    const offset = buf.readUInt32LE(o + 12);
    const entry = buf.subarray(offset, offset + length);
    assert.ok(entry.equals(read(`public/favicon-${buf[o]}.png`)), `entry ${i} is favicon-${buf[o]}.png`);
    sizes.push(buf[o]);
  }
  assert.deepEqual(sizes, [16, 32, 48]);
});

test("site.webmanifest names the host and the three icons", () => {
  const m = JSON.parse(text("public/site.webmanifest"));
  assert.equal(m.name, HOST);
  assert.equal(m.short_name, HOST);
  assert.equal(m.start_url, "/");
  assert.equal(m.display, "standalone");
  assert.equal(m.background_color, brand.background);
  assert.equal(m.theme_color, brand.background);
  assert.deepEqual(
    m.icons.map((i) => [i.src, i.sizes, i.purpose]),
    [
      ["/icon-192.png", "192x192", undefined],
      ["/icon-512.png", "512x512", undefined],
      ["/icon-512-maskable.png", "512x512", "maskable"],
    ],
  );
});

test("brand-manifest.json records the sources as they are now (else run npm run brand)", () => {
  const m = JSON.parse(text("public/brand-manifest.json"));
  assert.deepEqual(m.files, OUTPUTS);
  for (const [key, rel] of Object.entries(SOURCES)) {
    const actual = existsSync(join(FRONTEND, rel)) ? sha256(read(rel)) : null;
    assert.equal(
      m.sources[rel],
      actual,
      `${rel} changed since the last npm run brand (${key}); run it and commit the outputs`,
    );
  }
});
