// Pure cart logic for the Fieldnote shop.
//
// Everything here is side-effect free and works in cents so totals never pick up floating point
// noise. app.js owns the DOM; this module owns the arithmetic, which is what the tests cover.

/** @typedef {{ id: string, name: string, blurb: string, priceCents: number, hue: number }} Product */
/** @typedef {{ productId: string, name: string, priceCents: number, quantity: number }} CartLine */
/** @typedef {{ lines: CartLine[], itemCount: number, discountCode: string | null }} Cart */

/** @type {Product[]} */
export const PRODUCTS = [
  { id: "aurora-headphones", name: "Aurora Headphones", blurb: "Wireless, 30 hour battery, soft-touch ear cups.", priceCents: 7900, hue: 262 },
  { id: "terra-mug", name: "Terra Mug", blurb: "Hand-glazed stoneware, holds 350 ml.", priceCents: 1850, hue: 24 },
  { id: "lumen-lamp", name: "Lumen Desk Lamp", blurb: "Dimmable warm light with a weighted brass base.", priceCents: 4200, hue: 45 },
  { id: "drift-backpack", name: "Drift Backpack", blurb: "Waxed canvas, padded 16 inch laptop sleeve.", priceCents: 6400, hue: 160 },
];

/** Discount codes and the fraction they take off the subtotal. */
export const DISCOUNT_CODES = Object.freeze({
  SAVE10: 0.1,
  WELCOME15: 0.15,
});

/** @returns {Cart} */
export function createCart() {
  return { lines: [], itemCount: 0, discountCode: null };
}

/** @param {string} id */
export function findProduct(id) {
  const product = PRODUCTS.find((p) => p.id === id);
  if (!product) throw new Error(`unknown product: ${id}`);
  return product;
}

/**
 * Add `quantity` of a product. Adding a product that is already in the cart bumps its line.
 * @param {Cart} cart
 * @param {Product} product
 * @param {number} [quantity]
 * @returns {Cart}
 */
export function addItem(cart, product, quantity = 1) {
  if (!Number.isInteger(quantity) || quantity < 1) throw new RangeError("quantity must be a positive integer");
  const existing = cart.lines.find((line) => line.productId === product.id);
  const lines = existing
    ? cart.lines.map((line) => (line.productId === product.id ? { ...line, quantity: line.quantity + quantity } : line))
    : [...cart.lines, { productId: product.id, name: product.name, priceCents: product.priceCents, quantity }];
  return { ...cart, lines, itemCount: cart.itemCount + quantity };
}

/**
 * Remove a product's line entirely.
 * @param {Cart} cart
 * @param {string} productId
 * @returns {Cart}
 */
export function removeItem(cart, productId) {
  return { ...cart, lines: cart.lines.filter((line) => line.productId !== productId) };
}

/** Number of items shown on the cart badge. @param {Cart} cart */
export function itemCount(cart) {
  return cart.itemCount;
}

/** @param {CartLine} line */
export function lineTotal(line) {
  return line.priceCents * line.quantity;
}

/** @param {Cart} cart */
export function subtotal(cart) {
  return cart.lines.reduce((sum, line) => sum + line.priceCents, 0);
}

/**
 * Apply a discount code. Returns the cart unchanged plus an error message for unknown codes.
 * @param {Cart} cart
 * @param {string} code
 * @returns {{ cart: Cart, error: string | null }}
 */
export function applyDiscount(cart, code) {
  const normalized = code.trim();
  if (!normalized) return { cart: { ...cart, discountCode: null }, error: null };
  if (!(normalized in DISCOUNT_CODES)) return { cart, error: `"${normalized}" is not a valid code` };
  return { cart: { ...cart, discountCode: normalized }, error: null };
}

/** @param {Cart} cart */
export function discountCents(cart) {
  if (!cart.discountCode) return 0;
  return Math.round(subtotal(cart) * DISCOUNT_CODES[cart.discountCode]);
}

/** @param {Cart} cart */
export function total(cart) {
  return subtotal(cart) - discountCents(cart);
}

/** @param {number} cents */
export function formatMoney(cents) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}
