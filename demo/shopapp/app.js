// DOM wiring for the Fieldnote shop. All arithmetic lives in cart.js.
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
  removeItem,
  subtotal,
  total,
} from "./cart.js";

let cart = createCart();

const $ = (id) => document.getElementById(id);

function productCard(product) {
  const li = document.createElement("li");
  li.className = "card";
  li.dataset.productId = product.id;
  li.innerHTML = `
    <div class="swatch" style="--hue:${product.hue}" aria-hidden="true">
      <span>${product.name.split(" ")[0]}</span>
    </div>
    <div class="card-body">
      <h3>${product.name}</h3>
      <p>${product.blurb}</p>
      <div class="card-foot">
        <span class="price">${formatMoney(product.priceCents)}</span>
        <button type="button" class="primary add" aria-label="Add ${product.name} to cart">Add to cart</button>
      </div>
    </div>`;
  li.querySelector("button.add").addEventListener("click", () => {
    cart = addItem(cart, findProduct(product.id));
    render();
  });
  return li;
}

function cartLine(line) {
  const li = document.createElement("li");
  li.className = "line";
  li.dataset.productId = line.productId;
  li.innerHTML = `
    <div class="line-main">
      <span class="line-name">${line.name}</span>
      <span class="line-qty">Qty ${line.quantity} &times; ${formatMoney(line.priceCents)}</span>
    </div>
    <span class="line-total">${formatMoney(lineTotal(line))}</span>
    <button type="button" class="link remove" aria-label="Remove ${line.name}">Remove</button>`;
  li.querySelector("button.remove").addEventListener("click", () => {
    cart = removeItem(cart, line.productId);
    render();
  });
  return li;
}

function render() {
  const lines = $("cart-lines");
  lines.replaceChildren(...cart.lines.map(cartLine));
  $("cart-empty").hidden = cart.lines.length > 0;
  const count = itemCount(cart);
  $("cart-badge").textContent = String(count);
  $("cart-link").setAttribute("aria-label", `Cart, ${count} ${count === 1 ? "item" : "items"}`);
  $("cart-subtotal").textContent = formatMoney(subtotal(cart));
  $("cart-discount").textContent = `-${formatMoney(discountCents(cart))}`;
  $("cart-total").textContent = formatMoney(total(cart));
  $("checkout").disabled = cart.lines.length === 0;
}

$("product-grid").replaceChildren(...PRODUCTS.map(productCard));

$("discount-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const result = applyDiscount(cart, $("discount-code").value);
  cart = result.cart;
  const msg = $("discount-msg");
  msg.textContent = result.error ?? (cart.discountCode ? `Code ${cart.discountCode} applied.` : "");
  msg.classList.toggle("error", Boolean(result.error));
  render();
});

$("checkout").addEventListener("click", () => {
  $("checkout-note").textContent = "This is a demo shop, so checkout is turned off.";
});

render();
