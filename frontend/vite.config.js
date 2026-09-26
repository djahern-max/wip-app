import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fillProduct } from "./scripts/product-html.mjs";

// /api is proxied to the FastAPI dev server so the browser never deals with CORS.
// The product-constants hook fills %PRODUCT_NAME% and %SITE_HOST% in index.html (D-33);
// it runs before Vite's own env replacement so no placeholder is reported as unknown.
export default defineConfig({
  plugins: [
    react(),
    { name: "product-constants", transformIndexHtml: { order: "pre", handler: fillProduct } },
  ],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
});
