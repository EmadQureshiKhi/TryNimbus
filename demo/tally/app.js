// DOM wiring for Tally. All arithmetic lives in ledger.js.
import {
  addExpense,
  balances,
  createGroup,
  describeBalance,
  formatMoney,
  isSettled,
  memberName,
  parseAmount,
  settleUp,
  suggestPayments,
  totalSpent,
} from "./ledger.js";

const HUES = { alex: 248, sam: 168, jo: 18 };

let group = createGroup("Lisbon trip", ["Alex", "Sam", "Jo"]);

const $ = (id) => document.getElementById(id);

function avatar(member, size = "") {
  const span = document.createElement("span");
  span.className = `avatar ${size}`.trim();
  span.style.setProperty("--hue", String(HUES[member.id] ?? 220));
  span.setAttribute("aria-hidden", "true");
  span.textContent = member.name[0];
  return span;
}

function expenseRow(expense) {
  const li = document.createElement("li");
  li.className = "entry";
  li.dataset.expenseId = expense.id;
  const payer = memberName(group, expense.paidBy);
  li.innerHTML = `
    <span class="entry-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="18" height="18"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6M9 16h3" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </span>
    <div class="entry-main">
      <span class="entry-title"></span>
      <span class="entry-sub">${payer} paid &middot; split ${group.members.length} ways</span>
    </div>
    <span class="entry-amount">${formatMoney(expense.amountCents)}</span>`;
  li.querySelector(".entry-title").textContent = expense.description;
  return li;
}

function paymentRow(payment) {
  const li = document.createElement("li");
  li.className = "entry payment";
  li.innerHTML = `
    <span class="entry-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="18" height="18"><path d="M4 12h15M14 7l5 5-5 5" stroke="currentColor" stroke-width="1.9" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </span>
    <div class="entry-main">
      <span class="entry-title">${memberName(group, payment.from)} paid ${memberName(group, payment.to)}</span>
      <span class="entry-sub">Settle up payment</span>
    </div>
    <span class="entry-amount">${formatMoney(payment.amountCents)}</span>`;
  return li;
}

function balanceRow(member, cents) {
  const li = document.createElement("li");
  li.className = `balance ${cents > 0 ? "owed" : cents < 0 ? "owes" : "even"}`;
  li.dataset.member = member.id;
  li.dataset.testid = `balance-${member.id}`;
  const text = document.createElement("div");
  text.className = "balance-text";
  text.innerHTML = `<span class="balance-name">${member.name}</span><span class="balance-desc">${describeBalance(cents)}</span>`;
  li.append(avatar(member), text);
  return li;
}

function render() {
  const bal = balances(group);
  $("total-spent").textContent = formatMoney(totalSpent(group));

  const entries = [...group.expenses.map(expenseRow), ...group.payments.map(paymentRow)].reverse();
  $("activity").replaceChildren(...entries);
  $("activity-empty").hidden = entries.length > 0;
  const n = group.expenses.length;
  $("expense-count").textContent = n === 0 ? "No expenses" : `${n} ${n === 1 ? "expense" : "expenses"}`;

  $("balance-list").replaceChildren(...group.members.map((m) => balanceRow(m, bal[m.id])));

  const suggested = suggestPayments(group);
  $("suggested").replaceChildren(
    ...suggested.map((p) => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${memberName(group, p.from)} pays ${memberName(group, p.to)}</span><b>${formatMoney(p.amountCents)}</b>`;
      return li;
    }),
  );
  $("settle-plan").hidden = suggested.length === 0;

  const settled = isSettled(group);
  $("settled").hidden = !settled;
  const settle = $("settle-up");
  settle.hidden = settled;
  settle.disabled = group.expenses.length === 0;
}

$("faces").replaceChildren(
  ...group.members.map((m) => {
    const li = document.createElement("li");
    li.append(avatar(m, "small"));
    li.title = m.name;
    return li;
  }),
);

const paidBy = $("paid-by");
paidBy.replaceChildren(...group.members.map((m) => new Option(m.name, m.id)));

$("expense-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const error = $("form-error");
  const description = $("description").value.trim();
  const amountCents = parseAmount($("amount").value);
  if (!description) {
    error.textContent = "Add a short description, like Dinner.";
    return;
  }
  if (amountCents === null) {
    error.textContent = "Enter an amount in dollars, like 42.50.";
    return;
  }
  error.textContent = "";
  group = addExpense(group, { description, amountCents, paidBy: paidBy.value });
  event.target.reset();
  paidBy.value = group.members[0].id;
  render();
  $("description").focus();
});

$("settle-up").addEventListener("click", () => {
  group = settleUp(group);
  render();
});

render();
