// npm run brand (F05.2). One image is the brand: public/brand/logo.svg is the only
// hand-made picture in the repo, and every other brand asset (favicons, touch icon, PWA
// icons and manifest, Open Graph image) is generated from it here and committed. CI and
// deploy.sh never run this; they check dist/brand-manifest.json instead.
//
// Inputs (all read, none written):
//   public/brand/logo.svg       the logo, as supplied; must carry a viewBox
//   public/brand/logo-mark.svg  optional: a simplified mark for the 16 and 32 px favicons
//                               only (the line-art logo does not read at 16 px); absent,
//                               those two come from logo.svg
//   public/brand/brand.json     background, foreground, padding (hand-copied from
//                               styles.css; it does not follow the stylesheet)
//   src/product.js              SITE_HOST, the wordmark and the manifest name
//
// Rasterisation by sharp (devDependency). Every raster starts from an SVG this script
// builds around the logo, so nothing is resampled: each output is rendered at its own
// pixel size. Deterministic for the same inputs on the same machine (the OG wordmark is
// set in the font the machine resolves, so the committed PNG shows the owner's Mac).

import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = join(FRONTEND, "public");

export const SOURCES = {
  logo: "public/brand/logo.svg",
  mark: "public/brand/logo-mark.svg",
  brand: "public/brand/brand.json",
  product: "src/product.js",
};

// Everything the script writes, relative to public/, in the order it writes them.
export const OUTPUTS = [
  "favicon.svg",
  "favicon-16.png",
  "favicon-32.png",
  "favicon-48.png",
  "favicon.ico",
  "apple-touch-icon.png",
  "icon-192.png",
  "icon-512.png",
  "icon-512-maskable.png",
  "site.webmanifest",
  "og-image.png",
  "brand-manifest.json",
];

export const OG_WIDTH = 1200;
export const OG_HEIGHT = 630;
// Real family names: sharp renders text through fontconfig, which knows nothing of the
// browser generics system-ui and -apple-system.
export const OG_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif";

// --- pure parts (tests import these) ------------------------------------------------

/** The root element's attributes and inner markup of an SVG document. */
export function parseSvg(text) {
  const m = /<svg\b([^>]*)>([\s\S]*)<\/svg>\s*$/.exec(text);
  if (!m) throw new Error("not an SVG document (no <svg> root)");
  const attrs = {};
  for (const a of m[1].matchAll(/([\w:-]+)\s*=\s*"([^"]*)"/g)) attrs[a[1]] = a[2];
  if (!attrs.viewBox) throw new Error("the SVG has no viewBox; add one (0 0 width height)");
  const [x, y, w, h] = attrs.viewBox.trim().split(/[\s,]+/).map(Number);
  if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) {
    throw new Error(`the SVG viewBox is not four numbers: "${attrs.viewBox}"`);
  }
  return { viewBox: [x, y, w, h], attrs, inner: m[2] };
}

/** The logo as a nested <svg> filling the box (x, y, w, h), centred, aspect kept. */
export function placeLogo(logo, x, y, w, h) {
  const skip = new Set(["xmlns", "xmlns:xlink", "id", "x", "y", "width", "height", "viewBox"]);
  const carried = Object.entries(logo.attrs)
    .filter(([k]) => !skip.has(k))
    .map(([k, v]) => ` ${k}="${v}"`)
    .join("");
  return (
    `<svg x="${x}" y="${y}" width="${w}" height="${h}" viewBox="${logo.viewBox.join(" ")}"` +
    ` preserveAspectRatio="xMidYMid meet"${carried}>${logo.inner}</svg>`
  );
}

/** A square icon: optional solid background, the logo inset by `padding` of the box. */
export function iconSvg(logo, size, background, padding) {
  const inset = Math.round(size * padding);
  const box = size - 2 * inset;
  const rect = background ? `<rect width="${size}" height="${size}" fill="${background}"/>` : "";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">` +
    `${rect}${placeLogo(logo, inset, inset, box, box)}</svg>`
  );
}

/** The Open Graph image: background, the logo on the left at 60% of the height, the
 *  wordmark (SITE_HOST, once) to its right in the foreground colour. */
export function ogSvg(logo, host, brand) {
  const box = Math.round(OG_HEIGHT * 0.6);
  const left = 80;
  const top = Math.round((OG_HEIGHT - box) / 2);
  const fontSize = 104;
  const textX = left + box + 40;
  const baseline = Math.round(OG_HEIGHT / 2 + fontSize * 0.36);
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${OG_WIDTH}" height="${OG_HEIGHT}" viewBox="0 0 ${OG_WIDTH} ${OG_HEIGHT}">` +
    `<rect width="${OG_WIDTH}" height="${OG_HEIGHT}" fill="${brand.background}"/>` +
    placeLogo(logo, left, top, box, box) +
    `<text x="${textX}" y="${baseline}" font-family="${OG_FONT}" font-size="${fontSize}" fill="${brand.foreground}">${escapeXml(host)}</text>` +
    `</svg>`
  );
}

function escapeXml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

/** An ICO container holding PNG entries ({ size, png }); no dependency. */
export function ico(entries) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(entries.length, 4);
  const dir = Buffer.alloc(16 * entries.length);
  let offset = header.length + dir.length;
  entries.forEach(({ size, png }, i) => {
    const o = 16 * i;
    dir[o] = size >= 256 ? 0 : size; // width (0 means 256)
    dir[o + 1] = size >= 256 ? 0 : size; // height
    dir[o + 2] = 0; // palette size
    dir[o + 3] = 0; // reserved
    dir.writeUInt16LE(1, o + 4); // colour planes
    dir.writeUInt16LE(32, o + 6); // bits per pixel
    dir.writeUInt32LE(png.length, o + 8);
    dir.writeUInt32LE(offset, o + 12);
    offset += png.length;
  });
  return Buffer.concat([header, dir, ...entries.map((e) => e.png)]);
}

/** The string value of `export const NAME = "..."` in a source file. */
export function readConstant(source, name) {
  const m = new RegExp(`export\\s+const\\s+${name}\\s*=\\s*"([^"]+)"`).exec(source);
  if (!m) throw new Error(`${name} is not exported as a string constant`);
  return m[1];
}

export function webmanifest(host, brand) {
  return {
    name: host,
    short_name: host,
    start_url: "/",
    display: "standalone",
    background_color: brand.background,
    theme_color: brand.background,
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icon-512-maskable.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}

export function readBrand(text) {
  const brand = JSON.parse(text);
  for (const key of ["background", "foreground"]) {
    if (typeof brand[key] !== "string" || !/^#[0-9a-fA-F]{3,8}$/.test(brand[key])) {
      throw new Error(`brand.json: "${key}" must be a hex colour`);
    }
  }
  if (typeof brand.padding !== "number" || brand.padding < 0 || brand.padding >= 0.5) {
    throw new Error('brand.json: "padding" must be a number from 0 to below 0.5');
  }
  return brand;
}

export const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

// --- the run -------------------------------------------------------------------------

async function main() {
  const { default: sharp } = await import("sharp");
  const read = (rel) => readFileSync(join(FRONTEND, rel));

  const logoText = read(SOURCES.logo).toString("utf8");
  const logo = parseSvg(logoText);
  const hasMark = existsSync(join(FRONTEND, SOURCES.mark));
  const mark = hasMark ? parseSvg(read(SOURCES.mark).toString("utf8")) : logo;
  const brand = readBrand(read(SOURCES.brand).toString("utf8"));
  const host = readConstant(read(SOURCES.product).toString("utf8"), "SITE_HOST");

  const png = (svg, opaque) => {
    let s = sharp(Buffer.from(svg));
    if (opaque) s = s.flatten({ background: brand.background }).removeAlpha();
    return s.png({ compressionLevel: 9, palette: false }).toBuffer();
  };
  const out = {};

  out["favicon.svg"] = Buffer.from(logoText);
  out["favicon-16.png"] = await png(iconSvg(mark, 16, null, 0), false);
  out["favicon-32.png"] = await png(iconSvg(mark, 32, null, 0), false);
  out["favicon-48.png"] = await png(iconSvg(logo, 48, null, 0), false);
  out["favicon.ico"] = ico(
    [16, 32, 48].map((size) => ({ size, png: out[`favicon-${size}.png`] })),
  );
  out["apple-touch-icon.png"] = await png(iconSvg(logo, 180, brand.background, brand.padding), true);
  out["icon-192.png"] = await png(iconSvg(logo, 192, brand.background, brand.padding), true);
  out["icon-512.png"] = await png(iconSvg(logo, 512, brand.background, brand.padding), true);
  out["icon-512-maskable.png"] = await png(
    iconSvg(logo, 512, brand.background, brand.padding * 2),
    true,
  );
  out["site.webmanifest"] = Buffer.from(JSON.stringify(webmanifest(host, brand), null, 2) + "\n");
  out["og-image.png"] = await png(ogSvg(logo, host, brand), true);
  out["brand-manifest.json"] = Buffer.from(
    JSON.stringify(
      {
        sources: {
          [SOURCES.logo]: sha256(read(SOURCES.logo)),
          [SOURCES.mark]: hasMark ? sha256(read(SOURCES.mark)) : null,
          [SOURCES.brand]: sha256(read(SOURCES.brand)),
          [SOURCES.product]: sha256(read(SOURCES.product)),
        },
        files: OUTPUTS,
      },
      null,
      2,
    ) + "\n",
  );

  for (const name of OUTPUTS) {
    writeFileSync(join(PUBLIC, name), out[name]);
  }
  console.log(
    `brand: wrote ${OUTPUTS.length} files to public/ from ${SOURCES.logo}` +
      (hasMark ? ` (16 and 32 px favicons from ${SOURCES.mark})` : " (no logo-mark.svg: 16 and 32 px favicons from the logo)"),
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((err) => {
    console.error(`brand: ${err.message}`);
    process.exit(1);
  });
}
