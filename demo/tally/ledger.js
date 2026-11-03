// Pure ledger logic for Tally: groups, shared expenses, balances and settling up.
//
// Everything here is side-effect free and works in integer cents so balances never pick up
// floating point noise. app.js owns the DOM; this module owns the arithmetic, which is what the
// tests cover.

/** @typedef {{ id: string, name: string }} Member */
/** @typedef {{ id: string, description: string, amountCents: number, paidBy: string, shares: Record<string, number> }} Expense */
/** @typedef {{ id: string, from: string, to: string, amountCents: number }} Payment */
/** @typedef {{ name: string, members: Member[], expenses: Expense[], payments: Payment[] }} Group */

/** @returns {Group} */
export function createGroup(name, memberNames) {
  if (!memberNames.length) throw new RangeError("a group needs at least one member");
  const members = memberNames.map((n) => ({ id: n.toLowerCase(), name: n }));
  return { name, members, expenses: [], payments: [] };
}

/** @param {Group} group @param {string} id */
export function memberName(group, id) {
  const member = group.members.find((m) => m.id === id);
  if (!member) throw new Error(`unknown member: ${id}`);
  return member.name;
}

/**
 * Parse what someone typed in the amount field ("42", "42.5", "$1,200.00") into cents.
 * Returns null for anything that is not a positive amount with at most two decimals.
 * @param {string} text
 */
export function parseAmount(text) {
  const cleaned = String(text).trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  const [whole, frac = ""] = cleaned.split(".");
  const cents = Number(whole) * 100 + Number(frac.padEnd(2, "0"));
  return cents > 0 ? cents : null;
}

/**
 * Split an amount equally among members.
 * @param {number} amountCents
 * @param {string[]} memberIds
 * @returns {Record<string, number>} cents owed by each member
 */
export function splitEvenly(amountCents, memberIds) {
  if (!memberIds.length) throw new RangeError("cannot split between nobody");
  const share = Math.round(amountCents / memberIds.length);
  return Object.fromEntries(memberIds.map((id) => [id, share]));
}

/**
 * Add an expense paid by one member and split equally among everyone in the group.
 * @param {Group} group
 * @param {{ description: string, amountCents: number, paidBy: string }} expense
 * @returns {Group}
 */
export function addExpense(group, { description, amountCents, paidBy }) {
  const label = description.trim();
  if (!label) throw new RangeError("an expense needs a description");
  if (!Number.isInteger(amountCents) || amountCents <= 0) throw new RangeError("amount must be a positive number of cents");
  memberName(group, paidBy);
  const shares = splitEvenly(amountCents, group.members.map((m) => m.id));
  const id = `e${group.expenses.length + 1}`;
  return { ...group, expenses: [...group.expenses, { id, description: label, amountCents, paidBy, shares }] };
}

/**
 * Each member's balance in cents: positive means the group owes them, negative means they owe.
 * @param {Group} group
 * @returns {Record<string, number>}
 */
export function balances(group) {
  const out = Object.fromEntries(group.members.map((m) => [m.id, 0]));
  for (const e of group.expenses) {
    out[e.paidBy] += e.amountCents;
    for (const [id, cents] of Object.entries(e.shares)) out[id] -= cents;
  }
  for (const p of group.payments) {
    out[p.from] += p.amountCents;
    out[p.to] -= p.amountCents;
  }
  return out;
}

/** Total spent by the group, in cents. @param {Group} group */
export function totalSpent(group) {
  return group.expenses.reduce((sum, e) => sum + e.amountCents, 0);
}

/**
 * The payments that settle the group: whoever owes the most pays whoever is owed the most,
 * until nobody owes anything. Members are matched in a stable order.
 * @param {Group} group
 * @returns {{ from: string, to: string, amountCents: number }[]}
 */
export function suggestPayments(group) {
  const bal = balances(group);
  const order = group.members.map((m) => m.id);
  const debtors = order.filter((id) => bal[id] < 0).map((id) => ({ id, cents: -bal[id] }));
  const creditors = order.filter((id) => bal[id] > 0).map((id) => ({ id, cents: bal[id] }));
  const byAmount = (a, b) => b.cents - a.cents || order.indexOf(a.id) - order.indexOf(b.id);
  debtors.sort(byAmount);
  creditors.sort(byAmount);
  const payments = [];
  let d = 0;
  let c = 0;
  while (d < debtors.length && c < creditors.length) {
    const amountCents = Math.min(debtors[d].cents, creditors[c].cents);
    payments.push({ from: debtors[d].id, to: creditors[c].id, amountCents });
    debtors[d].cents -= amountCents;
    creditors[c].cents -= amountCents;
    if (debtors[d].cents === 0) d += 1;
    if (creditors[c].cents === 0) c += 1;
  }
  return payments;
}

/**
 * Record the suggested payments, which should bring every balance to zero.
 * @param {Group} group
 * @returns {Group}
 */
export function settleUp(group) {
  const recorded = suggestPayments(group).map((p, i) => ({ id: `p${group.payments.length + i + 1}`, ...p }));
  return { ...group, payments: [...group.payments, ...recorded] };
}

/** True once there is something to settle and every balance is zero. @param {Group} group */
export function isSettled(group) {
  return group.expenses.length > 0 && Object.values(balances(group)).every((cents) => cents === 0);
}

/** @param {number} cents */
export function formatMoney(cents) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

/**
 * How a balance reads in the Balances card.
 * @param {number} cents
 */
export function describeBalance(cents) {
  if (cents > 0) return `is owed ${formatMoney(cents)}`;
  if (cents < 0) return `owes ${formatMoney(-cents)}`;
  return "owes nothing";
}
