// index.html names the product through %PRODUCT_NAME% and %SITE_HOST% (D-33): the two
// constants in src/product.js stay the only literals, and this fills the placeholders in
// dev and in the build (a transformIndexHtml hook in vite.config.js). tests/brand.test.js
// uses the same function, so the test and the build agree.
import { PRODUCT_NAME, SITE_HOST } from "../src/product.js";

export function fillProduct(html) {
  return html.replaceAll("%PRODUCT_NAME%", PRODUCT_NAME).replaceAll("%SITE_HOST%", SITE_HOST);
}
