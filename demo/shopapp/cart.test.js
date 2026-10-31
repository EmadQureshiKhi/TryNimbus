import { test } from "node:test";
import assert from "node:assert/strict";

import {
  PRODUCTS,
  addItem,
  applyDiscount,
  createCart,
  discountCents,
  findProduct,
  formatMoney,
  itemCount,
  lineTotal,
  subtotal,
  total,
} from "./cart.js";

const headphones = findProduct("aurora-headphones");
const mug = findProduct("terra-mug");

test("a new cart is empty", () => {
  const cart = createCart();
  assert.equal(cart.lines.length, 0);
  assert.equal(itemCount(cart), 0);
  assert.equal(total(cart), 0);
});

test("adding different products lists each one", () => {
  const cart = addItem(addItem(createCart(), headphones), mug);
  assert.deepEqual(cart.lines.map((l) => l.productId), ["aurora-headphones", "terra-mug"]);
  assert.equal(itemCount(cart), 2);
});

test("subtotal sums single items", () => {
  const cart = addItem(addItem(createCart(), headphones), mug);
  assert.equal(subtotal(cart), 7900 + 1850);
});

test("adding the same product bumps its quantity", () => {
  const cart = addItem(addItem(createCart(), mug), mug);
  assert.equal(cart.lines.length, 1);
  assert.equal(cart.lines[0].quantity, 2);
});

test("line total multiplies price by quantity", () => {
  assert.equal(lineTotal({ productId: "x", name: "X", priceCents: 1850, quantity: 3 }), 5550);
});

test("a valid discount code takes its percentage off", () => {
  const { cart, error } = applyDiscount(addItem(createCart(), headphones), "SAVE10");
  assert.equal(error, null);
  assert.equal(discountCents(cart), 790);
  assert.equal(total(cart), 7110);
});

test("an unknown discount code is rejected", () => {
  const { cart, error } = applyDiscount(addItem(createCart(), headphones), "FREESTUFF");
  assert.match(error, /not a valid code/);
  assert.equal(cart.discountCode, null);
});

test("quantity must be a positive integer", () => {
  assert.throws(() => addItem(createCart(), mug, 0), RangeError);
});

test("money is formatted as US dollars", () => {
  assert.equal(formatMoney(15800), "$158.00");
});

test("every product has a unique id and a price", () => {
  const ids = new Set(PRODUCTS.map((p) => p.id));
  assert.equal(ids.size, PRODUCTS.length);
  for (const p of PRODUCTS) assert.ok(Number.isInteger(p.priceCents) && p.priceCents > 0);
});
