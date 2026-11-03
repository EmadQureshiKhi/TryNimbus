import { test } from "node:test";
import assert from "node:assert/strict";

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
  splitEvenly,
  suggestPayments,
  totalSpent,
} from "./ledger.js";

const trip = () => createGroup("Lisbon trip", ["Alex", "Sam", "Jo"]);

test("a new group has its members and nothing to settle", () => {
  const group = trip();
  assert.deepEqual(group.members.map((m) => m.name), ["Alex", "Sam", "Jo"]);
  assert.equal(memberName(group, "jo"), "Jo");
  assert.deepEqual(balances(group), { alex: 0, sam: 0, jo: 0 });
  assert.equal(isSettled(group), false);
});

test("amounts typed in dollars become cents", () => {
  assert.equal(parseAmount("90"), 9000);
  assert.equal(parseAmount("42.5"), 4250);
  assert.equal(parseAmount("$1,200.00"), 120000);
  assert.equal(parseAmount("0"), null);
  assert.equal(parseAmount("12.345"), null);
  assert.equal(parseAmount("ten"), null);
});

test("an even amount splits into equal shares", () => {
  assert.deepEqual(splitEvenly(9000, ["alex", "sam", "jo"]), { alex: 3000, sam: 3000, jo: 3000 });
});

test("adding an expense lists it and credits the payer", () => {
  const group = addExpense(trip(), { description: "Museum tickets", amountCents: 9000, paidBy: "sam" });
  assert.equal(group.expenses.length, 1);
  assert.equal(group.expenses[0].description, "Museum tickets");
  assert.equal(totalSpent(group), 9000);
  assert.deepEqual(balances(group), { alex: -3000, sam: 6000, jo: -3000 });
});

test("an expense needs a description, a payer in the group and a positive amount", () => {
  assert.throws(() => addExpense(trip(), { description: " ", amountCents: 100, paidBy: "alex" }), RangeError);
  assert.throws(() => addExpense(trip(), { description: "Taxi", amountCents: 0, paidBy: "alex" }), RangeError);
  assert.throws(() => addExpense(trip(), { description: "Taxi", amountCents: 100, paidBy: "kim" }), /unknown member/);
});

test("suggested payments go from whoever owes to whoever is owed", () => {
  let group = addExpense(trip(), { description: "Museum tickets", amountCents: 9000, paidBy: "sam" });
  group = addExpense(group, { description: "Groceries", amountCents: 3600, paidBy: "jo" });
  assert.deepEqual(suggestPayments(group), [
    { from: "alex", to: "sam", amountCents: 4200 },
    { from: "jo", to: "sam", amountCents: 600 },
  ]);
});

test("settling up an even split brings every balance to zero", () => {
  const group = settleUp(addExpense(trip(), { description: "Museum tickets", amountCents: 9000, paidBy: "alex" }));
  assert.equal(group.payments.length, 2);
  assert.deepEqual(balances(group), { alex: 0, sam: 0, jo: 0 });
  assert.equal(isSettled(group), true);
});

test("money is formatted as US dollars", () => {
  assert.equal(formatMoney(3333), "$33.33");
  assert.equal(formatMoney(120000), "$1,200.00");
});

test("balances read as owed, owes or nothing", () => {
  assert.equal(describeBalance(6000), "is owed $60.00");
  assert.equal(describeBalance(-3000), "owes $30.00");
  assert.equal(describeBalance(0), "owes nothing");
});
