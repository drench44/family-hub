#!/usr/bin/env bash
# Run the full Python + JS suites for the current branch on the heavy test box,
# from a REAL git checkout of the pushed commit.
#
# Why a git checkout and not an rsync copy: tests marked engine_checkout read
# the repo's own files (README, .gitignore, tracked files, git history) and
# skip themselves when there is no .git. A copy without .git silently skipped
# them, so a README-check failure passed there and then stopped a landing in CI
# (2026-11-27). This script pushes the branch, checks that exact commit out
# with `git worktree add` on the box, and sets FH_REQUIRE_ENGINE_CHECKS=1 so a
# tree without .git fails instead of skipping. See docs/adding-a-feature.md.
#
# Usage (from the branch's worktree, committed; uncommitted edits are not run):
#   FH_TEST_HOST=<ssh alias of the box> scripts/omen-test.sh
#
# Environment:
#   FH_TEST_HOST    required. The ssh alias of the box (kept out of this public
#                   repo on purpose).
#   FH_TEST_RUNNER  optional. A command that reads a bash script on stdin and runs
#                   it on the box, for example a lock-holding wrapper. Default is
#                   `ssh $FH_TEST_HOST bash -l -s`.
#   FH_TEST_CLONE   optional. The box's existing clone of this repo, holding the
#                   venv (.venv with pytest, httpx, pillow) and the git remote.
#                   Default ~/workspace/family-hub. Its checkout is never touched.
#
# Exit status is nonzero on any test failure, any skip-forced failure, or any
# setup problem. Skips are listed loudly.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
cd "$repo"

host=${FH_TEST_HOST:-}
if [ -z "$host" ] && [ -z "${FH_TEST_RUNNER:-}" ]; then
  echo "omen-test: set FH_TEST_HOST to the box's ssh alias" >&2
  exit 64
fi

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" = HEAD ]; then
  echo "omen-test: detached HEAD; check out a branch first" >&2
  exit 64
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "omen-test: WARNING uncommitted changes are NOT tested (only the pushed commit is)" >&2
fi
sha=$(git rev-parse HEAD)

echo "omen-test: pushing $branch at ${sha:0:12}" >&2
git push --quiet origin "HEAD:refs/heads/$branch"

runner=${FH_TEST_RUNNER:-"ssh -o ConnectTimeout=10 $host bash -l -s"}
echo "omen-test: running the suites on the box" >&2
{
  printf 'FH_BRANCH=%q\nFH_SHA=%q\nFH_CLONE=%q\n' "$branch" "$sha" "${FH_TEST_CLONE:-}"
  cat "$here/omen-test-remote.sh"
} | $runner
