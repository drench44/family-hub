# Runs ON the test box (piped in by omen-test.sh, which sets FH_BRANCH, FH_SHA
# and FH_CLONE first). Checks the pushed commit out as a git worktree beside
# the box's clone, runs both suites there, and removes the worktree.
set -uo pipefail
# This file arrives on bash's stdin, so every command that could read stdin
# (pytest, node) gets </dev/null or it would swallow the rest of the script.

clone=${FH_CLONE:-$HOME/workspace/family-hub}
py="$clone/.venv/bin/python"
[ -d "$clone/.git" ] || { echo "omen-test: no clone at $clone" >&2; exit 70; }
[ -x "$py" ] || { echo "omen-test: no venv python at $py" >&2; exit 70; }

git -C "$clone" fetch --quiet origin "$FH_BRANCH" || { echo "omen-test: fetch of $FH_BRANCH failed" >&2; exit 70; }
fetched=$(git -C "$clone" rev-parse FETCH_HEAD)
if [ "$fetched" != "$FH_SHA" ]; then
  echo "omen-test: the box sees $fetched for $FH_BRANCH, expected $FH_SHA (pushed since?)" >&2
  exit 70
fi

wt="$HOME/workspace/fh-test-${FH_SHA:0:12}-$$"
log=$(mktemp)
cleanup() {
  git -C "$clone" worktree remove --force "$wt" >/dev/null 2>&1
  git -C "$clone" worktree prune >/dev/null 2>&1
  rm -f "$log"
}
trap cleanup EXIT

git -C "$clone" worktree add --quiet --detach "$wt" "$FH_SHA" || { echo "omen-test: worktree add failed" >&2; exit 70; }
cd "$wt" || exit 70
[ -e .git ] || { echo "omen-test: the checkout has no .git, refusing to run" >&2; exit 70; }

status=0
skipped=0
for tz in "" UTC; do
  label=${tz:-box-default-TZ}
  echo "=== pytest tests (TZ=$label, commit ${FH_SHA:0:12}) ==="
  if [ -n "$tz" ]; then export TZ="$tz"; fi
  FH_REQUIRE_ENGINE_CHECKS=1 PYTHONPATH=src "$py" -m pytest tests -q -rs -p no:cacheprovider </dev/null 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  summary=$(grep -E '^[0-9]+ (passed|failed|error|skipped)|^=+ .*(passed|failed|error).* =+$' "$log" | tail -1)
  echo "RESULT TZ=$label exit=$rc :: $summary"
  [ "$rc" -eq 0 ] || status=1
  n=$(grep -c '^SKIPPED' "$log")
  skipped=$((skipped + n))
  if [ "$n" -gt 0 ]; then
    echo "!!! SKIPPED TESTS ($n) under TZ=$label, review each reason above !!!"
  fi
done

echo "=== engine_checkout tests (must run, not skip) ==="
FH_REQUIRE_ENGINE_CHECKS=1 PYTHONPATH=src "$py" -m pytest tests -q -m engine_checkout -rs -p no:cacheprovider </dev/null 2>&1 | tail -4
[ "${PIPESTATUS[0]}" -eq 0 ] || status=1

if [ "$skipped" -gt 0 ]; then
  echo "!!! TOTAL SKIPS across runs: $skipped !!!"
fi
if [ "$status" -eq 0 ]; then echo "omen-test: ALL PASSED"; else echo "omen-test: FAILED"; fi
exit "$status"
