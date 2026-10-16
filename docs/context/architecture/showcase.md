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
stored in `ShowcaseApp.pending`; nothing is public before staff approve it. The four show flags
(`SHOW_FLAGS`: call tier, streak, providers, categories) are not part of the reviewed proposal: they
live on the entry and a save applies them at once (a "sharing" event), so a team turns any of them
off without waiting; staff set them when they list an app for a team. An edit to a live app
is a new proposal and the live version stays until it is approved; a rejected application can be
edited and resubmits itself. The team's owner or admin can also take a live app off the showcase
at once ("removed", status `hidden`); only staff show it again. Staff review in the private admin,
which calls the staff API (`/admin/showcase/*`, `require_superadmin`): approve, reject with a reason
the maker sees, hide or show again, correct text, upload pictures, list an app for a team. The
queue carries `pending_since`, when the waiting proposal was sent, so the admin can warn before its
staged pictures expire. Every change appends a `ShowcaseAppEvent` under the next version; actions
carry an expected version and a stale one is refused.

## Where an app's activity comes from

`ShowcaseApp.usage_source` is `team` (every call the team makes; the default, and what an app staff
list starts with) or `keys` (only the calls made with the API keys in `ShowcaseAppKey`). The team's
owner or admin chooses it (`PUT /showcase/apps/{id}/usage`, keys from
`GET /showcase/teams/{id}/keys`, names and prefixes only). It is applied at once and not reviewed:
it changes no public text, link or picture and can only count the team's own calls; the change is
an `usage` event in the history. A key belongs to at most one app; it must be the team's and not
revoked. A rotated key (`ApiKey.replacement_key_id`) keeps counting through its replacement, which
is then taken by the same app. Staff see the source and the key names but cannot change them. Two
apps of a team that both count the whole team show the same numbers; the team fixes that by linking
keys. Calls made with no key (the dashboard, some MCP connections) count only for a whole-team app.

| Table | Holds | Only writer |
|---|---|---|
| `showcaseapp` | one entry: status, live fields, consent flags, usage source, the pending proposal | `application/showcase.py` |
| `showcaseappevent` | append-only history | `application/showcase.py` |
| `showcaseappkey` | the API keys an app's activity comes from, one app per key | `set_usage` |
| `showcaseweek` | one row per source (a team, or one key) per UTC week | `refresh_activity` (`treg-worker showcase activity`) |
| `showcasecursor` | the last call id counted into a source's weeks | `refresh_activity` |
| `showcasevote` | one upvote per person per app (the row is the vote) | `vote` |

Team deletion removes all of these with the team (`domain/governance/teams.py`), as it does every
team's rows; the key links go before the team's keys.

## Weekly rows, counted incrementally

Monthly numbers cannot be updated from a single "last status" plus new days: the week leaving the
window must be subtracted, and distinct provider counts do not add. The worker stores one row per
source per week, which do add: the leaderboard sums an app's sources (its keys and their
replacements, or its team), then the last four complete weeks (the week still running never
counts), unions their provider lists, and walks the active-day bits back from the last complete
week for the streak. Rows are kept for thirteen weeks so a streak can outgrow the window. Refused
calls never reached a provider and are not counted. Rows belong to sources, not apps, so switching
an app's source only changes which rows it reads; a source no approved app uses loses its rows and
cursor, and a newly used one is counted from the window at the next run.

The worker counts a little at a time. Each source has a cursor, the last `callrecord.id` it has
counted; a run reads only the source's calls after it, over the `(org_id, id)` index for a team or
`(org_id, api_key_id, id)` for a key, in batches of a few thousand, each a short transaction that
adds them to their week rows and moves the cursor with the cursor row locked (the row is inserted if
missing before it is locked, so two runs never both create it). Overlapping runs therefore never
count a call twice. Calls younger than a few minutes wait for the next run, so a lower id still
being committed is not skipped. Sources take turns a batch at a time until a run's cap, so one busy
source never holds the others back; a busy source's first four weeks are counted over several
runs. A team that counts the whole team and also has keyed apps has those keys' calls read twice,
once per source; the batches are small and indexed. Approval counts nothing: a newly approved app
joins the activity boards after the next run, so the run is scheduled hourly. A team's whole-week
query is not an option: a busy team's week can be a very large number of rows, each read from the
table because the `(org_id, created_at)` index does not hold the provider or tool. Upvotes are live.

## What leaves the server

`domain.showcase.public_entry` is the only shape activity leaves in: the upvote count, badges and, per
consent flag, a call tier (`100+` to `100k+`), Rising with growth rounded to ten percent, streak
days, a provider count, top categories. Boards rank only apps whose maker consented to the board's
metric. `GET /showcase/leaderboard` is checked on every view (`no-cache` with an ETag; an unchanged
board answers 304), so an approval or an upvote shows at once. There is no downvote; a second click takes an
upvote back. Upvotes need the browser session, a verified person (no agent, demo or suspended identity), the same origin and a rate limit.
App links carry `rel="noopener ugc"`: makers write the entries, and the referrer stays so they can
see visits from treg. All showcase routes, like the page, 404 on a self-hosted registry.

## Pictures

One pipeline serves every kind (`domain.images.KINDS`): user and team avatars (256 px square WebP),
showcase icons (512 px square PNG from at least 96 px, so a logo stays sharp when given large) and screenshots (WebP, longest side 1280
and a 640 copy, shape kept: a phone screenshot stays tall; nothing is cropped or stretched, and only
a square is scaled up to fill its side). An upload is size-capped while it streams, its leading
bytes must be PNG, JPEG or WebP, it is decoded under a pixel cap and re-drawn from its pixels in a
worker thread, and the files are named by the SHA-256 of the re-drawn primary, under `img/<kind>/`.
Everything lands in the private STAGING bucket; "publish" copies it to the PUBLIC bucket (read and
write through treg: the SDK cannot copy across buckets). Avatars publish when set (staging is only
where they wait until the person saves); showcase pictures when approved, before the row points at
them, and an approval whose staged pictures expired fails with "upload it again". A replaced
picture is deleted from the public bucket once no row names it. `treg-worker media sweep` deletes
every public picture no row names any more, through the same check: what a deleted team or person
left behind, or a file a racing change missed; it skips files younger than a day, since approval
publishes before the row is saved. Same-named files are one file, so a picture two teams use stays
while either names it.

The public bucket may be shared with other uses (page videos under `site/`): treg reads, writes and
deletes only under `img/`, and the bucket must carry no lifecycle rule that is not limited to its own
prefix. Staging is treg's alone, with a lifecycle rule that expires every object after about 30 days
(abandoned uploads and long-waiting proposals alike).

URLs are built from `TREG_MEDIA_BASE_URL` (a media domain) or, while it is empty, treg's own
`/media-files/`; the database stores only keys, so moving to a domain changes no data. With no media
settings the feature is off: uploads answer 503 and pages show initials; an endpoint that is neither
https nor localhost also leaves it off (logged), never breaking sign-in. Uploads and avatar changes
refuse a cross-site Origin, like every cookie-authenticated mutation. No database connection is
held during storage I/O; each step opens its own short session.
