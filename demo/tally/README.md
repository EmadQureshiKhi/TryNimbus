# Tally

Split trip expenses with friends: add what you spent, see who owes whom, settle up.
`ledger.js` holds the pure ledger logic in integer cents, `app.js` wires it to the page.

- Run: `python3 -m http.server 5174 --bind 127.0.0.1`, then open http://127.0.0.1:5174
- Test: `node --test`
