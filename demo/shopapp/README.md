# Fieldnote Supply (demo shop)

A small static shop used to demo and test Nimbus Repro.
`cart.js` holds the pure cart logic, `app.js` wires it to the page.

- Run: `python3 -m http.server 5173 --bind 127.0.0.1`, then open http://127.0.0.1:5173
- Test: `node --test`

The cart deliberately ships with bugs that the tests do not cover yet.
