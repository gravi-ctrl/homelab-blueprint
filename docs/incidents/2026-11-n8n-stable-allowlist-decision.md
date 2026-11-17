# Keeping Renovate away from n8n prereleases

**Date:** 2026-11
**Status:** in use (runbook has the details, including how to remove it)

## The problem

n8n marks some of its releases as prereleases on GitHub. Their Docker tags look the same as stable ones, just `2.47.5`, `2.47.6` and so on. Renovate only sees the tag, so it can't tell which is which, and it was happy to open PRs for prereleases.

What I wanted for n8n:

- stable releases only
- explicit version numbers, pinned to a digest
- the usual release-age wait
- a changelog I can read
- me merging it by hand, never automerge

## What I did

A small Python script runs once a day in GitHub Actions. It asks GitHub for n8n's releases, throws out drafts and prereleases, and writes a list of the stable versions into a Renovate preset. Renovate is only allowed to pick versions from that list. Everything else, like finding versions, looking up digests, the release-age wait and opening PRs, is still Renovate's job.

The script runs in GitHub Actions and not inside Renovate because I didn't want to rely on running my own code in the hosted Renovate. It opens a PR instead of pushing, since `main` is protected, and it uses a PAT limited to this one repo so the normal CI runs on that PR.

The code lives in my stacks repo (`gravi-ctrl/server-docker-backup`), not in this one. The runbook with the day-to-day details is in my notes.

If the script can't be sure about something, it stops. A broken run means the list stays old and I stop getting new n8n PRs. It never means a bad list.

## What else I looked at

- **Just review each PR.** n8n is manual-merge anyway, so this was the simplest option. I passed on it because a prerelease PR looks the same as a stable one, so I'd be checking the releases page every time.
- **A longer release-age wait for n8n.** Cheap, and it would probably catch most prereleases, but it's a guess based on time and doesn't look at the actual stable flag. This is what I'd switch to if the script ever becomes a pain.
- **Renovate's GitHub releases lookup instead of the Docker one.** It does understand prereleases, but as far as I could tell it can't give me image digests, so I'd lose digest pinning.

## Is it overkill?

A bit. The cost is a script, a workflow, a token that has to be renewed, and a PR I have to merge every time a new stable release shows up. It's more than the problem strictly needs. I kept it because it gives me the real stable flag and digest pinning together, and because it's easy to take out. The runbook has a "how to retire" section with the exact order.

## Lessons
When two systems each know half of what you need (GitHub knows stable or not, Renovate only sees the tag), you can often close the gap with one small script, as long as it fails safe and you've written down how to take it out.
