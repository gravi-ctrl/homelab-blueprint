# Renovate aborted with "repository-changed" but the real error was a 403

**Date:** 2026-10  
**Severity:** low (dependency PRs stopped; no production impact)  
**Status:** resolved

## Summary
After moving Renovate from Codeberg to GitHub, every run created at most one branch and then aborted with `Repository has changed during renovation - aborting` (exit code 6). No dependency dashboard was created. The message pointed at a concurrent push, but the real cause was a missing token permission: Renovate got a `403 Forbidden` when posting a commit status, and surfaced it as `repository-changed`.

## Impact
No update PRs beyond the first branch, and no dashboard issue, until fixed. Nothing was deployed or broken.

## Timeline
1. **Symptom:** `repository-changed`, abort after 0 or 1 branches. The number of branches created varied between runs.
2. **Hypothesis 1: something pushed to `main` mid-run** (the sync job, or my own commit that day). Compared commit timestamps with the run times and recorded `main`'s SHA before and after a run.
3. **Result:** the SHA did not change, so the hypothesis was wrong.
4. **Turning point:** re-ran with `LOG_LEVEL=debug` and searched the log around the abort. Just before `Passing repository-changed error up` was an HTTP 403 on `POST .../statuses/<sha>`.
5. **Fix:** added the **Commit statuses: read/write** permission to the fine-grained PAT.
6. **Verification:** the next run created all branches, PRs and the dependency dashboard.

## Root cause
The fine-grained PAT lacked the *Commit statuses* permission. Renovate sets a status check (for example for `minimumReleaseAge`) on every branch it creates, and treats a failure of that call as "the repository changed".

## What made it hard
- The error message described a different problem than the real one.
- The default `info` log level hid the HTTP error entirely.
- The token had other permissions, which made "the token is fine" feel true.

## Resolution
Added the missing permission and re-ran. The complete permission set for Renovate on a fine-grained PAT: Contents, Pull requests, Issues, Commit statuses (read/write), Workflows (if workflow files change), and Metadata (read).

## What went well / what didn't
- Testing the first hypothesis (concurrent push) cheaply, by comparing SHAs before and after, ruled it out quickly.
- I assumed the error message was accurate for too long; the debug log should have been the first step.

## Follow-ups
- [x] Keep the full permission list in the Renovate setup notes.
- [ ] Move Renovate to the hosted GitHub App, so a long-lived PAT with write access isn't kept on the server.

## Lessons
When a tool's error doesn't match the evidence, stop theorising and turn up the logging. The first useful fact was one line at debug level.
