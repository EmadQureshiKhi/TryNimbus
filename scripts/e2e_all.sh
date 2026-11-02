#!/bin/zsh
# Every end-to-end check, back to back. Headless and offscreen: safe to run while using the Mac.
# Needs MODEL_API_KEY. Prints one summary line per check; full output in artifacts/evals/e2e.log.
set -u
ROOT="${0:A:h:h}"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/artifacts/evals/e2e.log"
mkdir -p "$ROOT/artifacts/evals"
: > "$LOG"
pass=0; fail=0
run() {
  local name="$1"; shift
  local start=$SECONDS
  if "$@" >> "$LOG" 2>&1; then
    pass=$((pass + 1)); printf "PASS  %-34s %4ss\n" "$name" $((SECONDS - start))
  else
    fail=$((fail + 1)); printf "FAIL  %-34s %4ss  (see %s)\n" "$name" $((SECONDS - start)) "$LOG"
  fi
}
run "offline test suite"            "$PY" -m pytest -q
run "live API checks"               "$PY" -m pytest -q -m live tests/test_live.py
run "pointing: fixture apps"        "$PY" tools/eval_pointing.py
run "pointing: live web apps"       "$PY" tools/eval_pointing.py --web
run "Shadow end to end (fresh)"     zsh -c "rm -rf artifacts/evals/shadow_plan && '$PY' tools/eval_shadow.py"
run "overlay layout (no overlaps)"  "$PY" tools/check_layout.py
run "app controller smoke"          "$PY" tools/app_smoke.py
run "Repro end to end (Muse Code)"  "$PY" -m pytest -q -m live tests/test_repro_live.py
run "Shadow in the real app"        "$PY" tools/shadow_e2e.py
run "Repro in the real app"         "$PY" tools/repro_e2e.py
echo "---"
echo "$pass passed, $fail failed"
grep -hE '"(pointing_hit_rate|web_pointing_hit_rate|math_anchored|pointing_hits|false_passes_blocked|real_completions_verified)"' \
  artifacts/evals/pointing.json artifacts/evals/pointing_web.json artifacts/evals/shadow.json 2>/dev/null | sed 's/^ */  /'
grep -h "walkthroughs clean" "$LOG" | tail -1 | sed 's/^/  /'
[ $fail -eq 0 ]
