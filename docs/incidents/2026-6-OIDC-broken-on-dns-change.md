# OIDC (pocket-id) broke upon changing the main DNS on my server

**Date:** 2026-06
**Severity:** medium (All OIDC broke. Impacted all containers using OIDC)
**Status:** resolved / mitigated / open

## Summary
I rebuilt `daemon.json` putting Google and Cloudflare first. I mean, at the time it felt like the right choice rather than depending on my own unbound DNS. It made sense back then, but it was a not-so-smart-decision because OIDC broke in every container that used it. It took me a bit to connect "why is OIDC broken?" to "Oh ye, I changed the DNS"

## Impact
It impacted all containers using OIDC, and they are... a lot. No major issues in password login, how the containers function, how the server reaches the internet.

## Timeline
1. Trying to access Nextcloud through OIDC led to a "DNS error page".
2. Confirming that it's not an invidividual issue with just Nextcloud by accessing every container that uses OIDC and, confirmed that it wasn't an issue wit one container.
3. This led to confirm that the issue was neither in the affected containers, nor Pocket ID itself, but how the containers reach each other. Something broke in how containers reach each other.
4. The turning point was me connecting the dots that I did a recent "harmless" change in the order of the DNS during `setup.sh`
5. Once it was confirmed that this was the root cause, a simpple fix was implemented to update the order in `daemon.json`, and add an entire bullet point in `ARCHITECTURE.md` (see point 2).
6. After the fix was implemented, OIDC worked again in all containers.

## Root cause
Putting Google and Cloudflare's DNS first before my own server's DNS broke it.

## What made it hard
1. At that time, Nextcloud had an update that shouldn't break anything, but it had me go through a rabbit hole of logs trying to see if it was the issue.
2. What also didn't help is that Pocket ID itself had an update, which had me dig through its logs, which took time.
3. While the root cause might be clear from the outside, but doing a lot of adjustments made it quite hard to pinpoint the root cause that it was the adjustment I made on the `daemon.json`.

## Resolution
Adding ${SERVER_IP}, making sure the actual IP is added correctly in the `.env` file, running `setup.sh` (it's idempotent so it shouldn't cause any issues running it again), then checking OIDC in Nextcloud.

## What went well / what didn't
- Assuming that the issue was in either Nextcloud or Pocket ID, then dig through their logs was a terrible decision.
- After finally testing every container that has OIDC in it is what led me to the right path.


## Follow-ups
- [x] Added the change back in `setup.sh` then coming up with the `ARCHITECTURE.md` which covers similar cases.
- [x] Finally implementing the incidents documenting for those cases.

## Lessons
Tracking down the root cause, alienating all false positives first before going through the rabbit hole could've sped the solution even more.
