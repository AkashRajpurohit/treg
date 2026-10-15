---
title: Showcase leaderboard and pictures - applications, review, votes, avatars
status: shipped
sources:
  - src/treg/domain/showcase/__init__.py
  - src/treg/application/showcase.py
  - src/treg/routers/showcase.py
  - src/treg/domain/images/__init__.py
  - src/treg/application/images.py
  - src/treg/routers/images.py
  - src/treg/infra/imaging.py
  - src/treg/infra/media_store.py
  - src/treg/alembic/versions/0076_showcase_and_avatars.py
  - src/treg/web/showcase.html
  - tests/test_showcase_leaderboard.py
  - tests/test_images.py
related:
  - interface/seo.md
  - architecture/media.md
---

# Showcase leaderboard and pictures

`/showcase` has two parts. "The apps" is an editorial gallery written in the page itself (see
[seo](../interface/seo.md)). The leaderboard below it is database-driven: teams apply, staff
review, people upvote, and approved apps show activity badges their makers agreed to.

## Applying and review

An owner or admin of a team applies on /showcase or the dashboard's Team page: name, link, tagline,
description, category, maker, X handle, launch post, an icon and up to four screenshots, and which
metrics may be shown. A team lists at most five apps (rejected ones do not count). The proposal is
stored in `ShowcaseApp.pending`; nothing is public before staff approve it. An edit to a live app
is a new proposal and the live version stays until it is approved; a rejected application can be
edited and resubmits itself. Staff review in the private admin, which calls the staff API
(`/admin/showcase/*`, `require_superadmin`): approve, reject with a reason the maker sees, hide or
show again, correct text, upload pictures, list an app for a team. Every change appends a
`ShowcaseAppEvent` under the next version; actions carry an expected version and a stale one is
refused.

| Table | Holds | Only writer |
|---|---|---|
| `showcaseapp` | one entry: status, live fields, consent flags, the pending proposal | `application/showcase.py` |
| `showcaseappevent` | append-only history | `application/showcase.py` |
| `showcaseweek` | one row per listed team per UTC week | `refresh_activity` (`treg-worker showcase activity`) |
| `showcasecursor` | the last call id counted into a team's weeks | `refresh_activity` |

Team deletion removes all of these with the team (`domain/governance/teams.py`), as it does every
team's rows.
| `showcasevote` | one upvote per person per app (the row is the vote) | `vote` |

## Weekly rows, counted incrementally

Monthly numbers cannot be updated from a single "last status" plus new days: the week leaving the
window must be subtracted, and distinct provider counts do not add. The worker stores one row per
team per week, which do add: the leaderboard sums the last four complete weeks (the week still
running never counts), unions their provider lists, and walks the active-day bits back from the
last complete week for the streak. Rows are kept for thirteen weeks so a streak can outgrow the
window. Refused calls never reached a provider and are not counted. The calls are the team's, so
activity is counted per team that lists an approved app, and every app of that team shows the same
numbers.

The worker counts a little at a time. Each team has a cursor, the last `callrecord.id` it has
counted; a run reads only the team's calls after it, over the `(org_id, id)` index, in batches of a
few thousand, each a short transaction that adds them to their week rows and moves the cursor with
the cursor row locked (the row is inserted if missing before it is locked, so two runs never both
create it). Overlapping runs therefore never count a call twice. Calls younger than a few minutes
wait for the next run, so a lower id still being committed is not skipped. Teams take turns a
batch at a time until a run's cap, so one busy team never holds the others back; a busy team's
first four weeks are counted over several runs. A team with no approved app left loses its rows and
cursor and is counted afresh from the window if it lists again. Approval counts nothing: a newly
approved app joins the activity boards after the next run, so the run is scheduled hourly. A
team's whole-week query is not an option: one busy team's week is hundreds of thousands of rows,
each read from the table because the `(org_id, created_at)` index does not hold the provider or
tool. Upvotes are live.

## What leaves the server

`domain.showcase.public_entry` is the only shape activity leaves in: the upvote count, badges and, per
consent flag, a call tier (`100+` to `100k+`), Rising with growth rounded to ten percent, streak
days, a provider count, top categories. Boards rank only apps whose maker consented to the board's
metric. `GET /showcase/leaderboard` is checked on every view (`no-cache` with an ETag; an unchanged
board answers 304), so an approval or an upvote shows at once. There is no downvote; a second click takes an
upvote back. Upvotes need the browser session, a verified person (no agent, demo or suspended identity), the same origin and a rate limit.
All showcase routes, like the page, 404 on a self-hosted registry.

## Pictures

One pipeline serves every kind (`domain.images.KINDS`): user and team avatars (256 px PNG), showcase
icons (256 px PNG) and screenshots (1280x800 and 640x400 WebP, cropped from the top). An upload is
size-capped while it streams, its leading bytes must be PNG, JPEG or WebP, it is decoded under a
pixel cap and re-drawn from its pixels in a worker thread, and the files are named by the SHA-256 of
the re-drawn primary. Everything lands in the private STAGING bucket; "publish" copies it to the
PUBLIC bucket (read and write through treg: the SDK cannot copy across buckets). Avatars publish when
set; showcase pictures when approved, before the row points at them. A replaced picture is deleted
from the public bucket once no row names it; staging is left to the bucket's lifecycle rule.

URLs are built from `TREG_MEDIA_BASE_URL` (a media domain) or, while it is empty, treg's own
`/media-files/`; the database stores only keys, so moving to a domain changes no data. With no media
settings the feature is off: uploads answer 503 and pages show initials. No database connection is
held during storage I/O; each step opens its own short session.
