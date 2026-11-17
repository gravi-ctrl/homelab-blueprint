# Renovate silently stopped processing the repo because of a missing comma

**Date:** 2026-11
**Severity:** low (dependency PRs stopped automerging; no production impact)
**Status:** resolved

## Summary
A config change (#150) added new entries to the never-automerge rule in `renovate.json` but missed the commas between them. Mend-hosted Renovate could not parse the file and aborted every run at the init phase, so open PRs that had passed all checks sat unmerged past the automerge window. The PR pages kept saying "Automerge: Enabled", which made it look like the config was fine. Fixing the commas and re-running Renovate resolved it, and I added a CI check so an invalid Renovate config can no longer reach `main`.

## Impact
No dependency PRs were processed or automerged from the broken commit (06:34 Cairo time) until the fix later the same day. Two green PRs (#145 and #149, both Docker digest updates) were left open past the 6-11am automerge window. Nothing was deployed wrongly and nothing was lost.

## Timeline
1. **Detection:** two Renovate PRs with all checks green had not automerged even though the 6-11am window had passed.
2. **Hypotheses:** the automerge schedule vs. when Renovate runs, branch protection blocking the bot, and a stale branch from the git-auto-sync pushes.
3. **First real lead:** reading `renovate.json` showed missing commas in the `matchPackageNames` list of the never-automerge rule.
4. **Misleading signal:** the PR body still said "Automerge: Enabled". That text is only rewritten by a successful run, so it was left over from before the break.
5. **Confirmation:** the Mend job log showed `Repository has invalid config`, `Skipping the rest to the Renovate run due to error in init phase`, every phase at 0 ms and exit code 5. Renovate had also opened an "Action Required: Fix Renovate Configuration" issue (#151).
6. **Fix and verification:** fixed the commas, validated locally with `renovate-config-validator` in Docker (the first attempt still had an error at line 122, the second passed), then ticked the Dependency Dashboard re-run checkbox. The run completed, issue #151 closed itself, and the PRs merged in the next window.

## Root cause
Invalid JSON in `renovate.json`: missing commas after several entries in the `matchPackageNames` array, added in #150. The symptom was "PRs are not automerging"; the cause was that Renovate never got past parsing the config, so automerge was never evaluated at all.

## What made it hard
- The PR body said "Automerge: Enabled", so the config looked healthy. It was stale output from the last good run.
- Nothing alerted me. The only signal was a GitHub issue created by the bot, which was easy to miss.
- The failure left existing PRs untouched and green, so everything looked normal from the PR list.
- The obvious suspects (schedule, branch protection, stale branches) were all plausible, which made it easy to keep theorising instead of reading the run log.

## Resolution
- Added the missing commas in the never-automerge rule and validated with:
  `docker run --rm -v "$PWD:/usr/src/app" -w /usr/src/app renovate/renovate renovate-config-validator`
- Triggered a new run from the Dependency Dashboard checkbox and confirmed in the job log that the run completed, issue #151 closed, and the pending PRs merged.
- Added a GitHub Actions workflow that runs the same validator on every PR, and made it a required status check on `main`. It has already failed on a syntax error before merge, as intended.

## What went well / what didn't
- The local Docker validator reproduced the error in seconds and needed no Node install.
- The Mend job log gave the definitive answer in a few lines.
- I trusted the "Enabled" line on the PR for too long. The job log should have been the first thing I opened.
- #150 merged without anything validating the Renovate config, so a typo in a file with no tests went straight to `main`.
- Side effect when adding the required check: PRs opened before the workflow existed (#153, #154) sat on "Expected: waiting for status" because `validate` was never part of their event. Re-triggering them (close/reopen or update the branch) fixed it.

## Follow-ups
- [x] CI workflow `Validate Renovate config` runs `renovate-config-validator` on every PR and on pushes to `main`, set as a required status check. It deliberately has no `paths:` filter on `pull_request`, so the required check always reports.
- [x] Validate the generated `renovate-feed/n8n-stable.json` preset too, since a broken preset causes the same failure.
- [x] Enable automerge on the generated n8n allowlist PR, gated by the required checks, so it is safe without manual review. Tested end to end.
- [X] Get notified when Renovate opens an "Action Required: Fix Renovate Configuration" issue, instead of relying on me to notice it.
- [x] When adding a new required check, re-trigger open PRs so they pick it up.

## Lessons
A status message in a PR is only as fresh as the last successful run, so when automation looks healthy but does nothing, read the run log first. Anything that can silently stall a pipeline, like a config file, deserves a CI check that fails loudly before the change merges.
