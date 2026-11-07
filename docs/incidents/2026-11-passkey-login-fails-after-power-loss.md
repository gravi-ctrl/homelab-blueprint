# Pocket ID passkey login failed after a power outage

**Date:** 2026-11
**Severity:** low (single-user homelab, about 7 minutes of blocked SSO logins, no data lost, and password logins stayed available on every app)
**Status:** resolved (symptom cleared by restarting the browser; root cause not confirmed)

## Summary
After a power outage, passkey logins through Pocket ID failed with a generic "An unknown error occurred" for every app I tried (Nextcloud, n8n). The server was healthy throughout, and the logs show every login stalling right after `login/start`, so the failure was in the browser's passkey step. Restarting Firefox fixed it; the exact cause is unverified.

## Impact
SSO login to Nextcloud and n8n was blocked for about 7 minutes (21:48 to 21:55 by the server logs). No service was down after boot, no data was lost, and username/password logins stayed available on every app. The server itself was offline for at most about 17 minutes (last log line 21:04, restart 21:21). The unclean shutdown also cleared the browser's cookies, so every app needed a fresh login.

## Timeline
Times are container log times, 2026-11-06.

- **21:04** Last log line from the previous run. Unlike the clean stop at 18:43, no shutdown message follows, so power was lost suddenly at some point between 21:04 and 21:21.
- **21:21** Pocket ID boots normally (database connected, server listening).
- **21:48** First Nextcloud login attempt. Nextcloud's back end fetched the OIDC discovery document fine, the browser reached the Pocket ID login page, and `GET /api/webauthn/login/start` returned 200. No `login/finish` ever followed, and the page showed "An unknown error occurred".
- **21:49 to 21:53** Retried Nextcloud and n8n in Firefox. Same pattern every time: three failed login flows, five `login/start` calls, zero `login/finish` calls.
- **\~21:52** Piped the 113 log lines into an LLM helper. It blamed missing HTTPS, which did not hold up (see below). Its useful point was that the logs stop after `login/start`, so the problem is probably client-side.
- **21:53** Tried Nextcloud in Chrome on the same machine, which has no password manager extension installed, so no passkey was available. It stalled at `login/start`.
- **\~21:54** Restarted Firefox.
- **21:55** Nextcloud login in Firefox: `login/start` at 21:55:27, `login/finish` at 21:55:29, token exchange at 21:55:30. Fixed.
- **21:57** n8n SSO worked off the existing Pocket ID session, with no new passkey prompt.

## Root cause
**Not confirmed.** The symptom was "An unknown error occurred" on the Pocket ID login page.

What the logs establish:

- The server answered every request with a 200 in a few milliseconds, starting 27 minutes after boot.
- The back channel (Nextcloud to Pocket ID) worked, so this was not a repeat of the 2026-06 DNS incident, and the browser was on `https://pocket.wonder.lan`.
- Every failed flow stops between `login/start` and `login/finish`, so the browser never completed the passkey step.
- The `401 not_signed_in` lines on `/api/users/me` are the normal "am I logged in?" probe and match the cleared cookies. They are not the fault.

The Chrome attempt is not evidence either way: it has no password manager extension, so it could not have completed a passkey login whatever the server did. The simplest explanation left is that Firefox's extension was in a bad state after the unclean shutdown (for example locked or disconnected from the password manager) and restarting Firefox re-initialised it. This fits the logs but is unverified: the browser console error was not captured, and a restart resets other browser state too, so it cannot separate the extension from anything else.

## What made it hard
- The error message is generic and never says it is a browser or passkey failure.
- The server logs look healthy. The real clue was something missing (no `login/finish` after `login/start`), and a missing line is much harder to spot than an error line.
- Nothing had changed on the server, so it was tempting to suspect the server or its config.
- The LLM's HTTPS theory sounded plausible. It misread `tls=false`, which is only the container's own listener behind the reverse proxy; the browser was already on https, and the same setup worked minutes later with no change.
- It was late and I was tired.

## Resolution
Restarted Firefox. No server or configuration changes were made. Verified from the logs: a complete OIDC flow for Nextcloud (authorize, `login/finish`, interaction complete, token, JWKS), then an n8n login with no new passkey prompt.

## What went well / what didn't
Went well:

- Noticed that every app broke at once with nothing changed, which pointed at the shared login step rather than the individual apps.
- Made no server changes on the strength of an unverified suggestion.
- Username/password logins were available on every app as a fallback, and I still diagnosed the SSO failure instead of bypassing it.
- The containers came back unattended after the power loss, and Pocket ID's request logs were detailed enough to rebuild the timeline afterwards.

Didn't go well:

- Reached for an LLM before doing the cheap client-side checks, and it gave a confident, wrong diagnosis.
- The browser console was not checked, so the actual WebAuthn error was never recorded and the cause stays unconfirmed.

## Follow-ups
- [ ] Next time, before restarting the browser, open the console (F12) and record the exact error, then try reloading or re-unlocking the password manager extension. If that alone fixes it, the cause is confirmed.
- [ ] Add a short troubleshooting note: run `docker logs pocket-id 2>&1 | grep -E 'webauthn/login/(start|finish)'`. Starts with no finishes means the failure is in the browser or passkey step, not the server.
- [ ] Consider a small UPS for the server, ideally with a clean-shutdown signal, since the power loss was the trigger.

## Lessons
When every app behind one identity provider fails at once and nothing has changed, work out which hop failed before touching anything: here the server logged `login/start` but never `login/finish`, which put the fault in the browser's passkey step, not the server. A confident LLM diagnosis is only a hypothesis until the evidence agrees with it. Password logins on every app kept this low severity, but I diagnosed it rather than bypassing it.
