# family-hub

Public family wall-dashboard: FastAPI + SQLite + vanilla JS. Keep any
deployment-specific data (real calendar IDs, LAN IPs, camera URLs, secrets) out
of this repo — it's public.

## Code review before merge — required

Any substantive change in this repo goes through review BEFORE it merges (or
opens as a PR). Run all three review agents on the branch diff and address real
findings:

- `pr-review-toolkit:silent-failure-hunter` — swallowed errors, weak fallbacks,
  silent wrong-but-reassuring outcomes
- `pr-review-toolkit:code-reviewer` — guideline/style/best-practice adherence,
  dead code, public-repo leaks
- `pr-review-toolkit:pr-test-analyzer` — test-coverage quality; flag tests that
  skip silently in CI or assert nothing

Also do a per-change review, and a whole-branch review for multi-task work.
Verify tests genuinely RUN (not silently skipped). This is the default gate — it
should happen without being asked. Docs-only changes (`*.md`, comments) are
exempt.

## After cloning

After cloning, run `scripts/install-hooks.sh` — installs the pre-push
privacy guard (inert without the operator's private scanner).
