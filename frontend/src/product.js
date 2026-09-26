// The product name lives here and nowhere else in the frontend (CLAUDE.md).
// The name is the hostname, one string (D-33; D-09 closed).
export const PRODUCT_NAME = "jobcost.dev";

// The support contact, the same address as on /privacy and /terms (F05.1).
export const SUPPORT_EMAIL = "admin@jobcost.dev";

// The hostname (D-27): the wordmark on the brand assets and every og: value (F05.2).
// Since D-33 it is the product name itself, so it is defined as the constant, not as a
// second literal; if the two ever diverge, this line gets its own string and
// tests/test_product_name.py its own rule.
export const SITE_HOST = PRODUCT_NAME;
