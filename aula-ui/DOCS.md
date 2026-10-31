# Aula UI

The supported interface is the built-in Reddit-style **Aula Feed** in Home
Assistant's sidebar. There is no standalone web deployment, Lovelace dashboard
or custom-card installation.

## Diagnostic logs

Open **Settings > Apps > Aula UI > Logs** (or run
`ha apps logs <slug>_aula-ui`, where the slug is shown on the app's Info page). Logging is written to stdout and retained/rotated
by Home Assistant's container logging; the add-on does not grow a separate
unbounded log file or store logs in the family data cache.

At the default `log_level: info`, logs cover startup/shutdown, refresh requests,
sync stages and outcome, per-child school/daycare decisions, API retries,
collection counts, AI queue progress/backoff/failover,
login/identity/refresh lifecycle, settings changes and calendar review/archive/
subscription actions. A completed operation means that function returned;
`event=sync.status status=...` and `event=source.sync_result outcome=...`
describe whether the sync actually succeeded or retained cached data.

Choose **log_level** in App configuration and restart. Each threshold includes
all more severe levels:

| Level | Purpose | Label color |
| --- | --- | --- |
| `trace` | Most detailed: HA requests, Aula request boundaries, ingress reads and state saves. | Gray |
| `debug` | API response status/timing, transport details, calendar publication and diagnostic decisions. | Cyan |
| `info` | Normal lifecycle, sync outcomes, progress and user actions (default). | Green |
| `warning` | Recoverable failures, retries and rejected requests. | Yellow |
| `error` | Failed operations requiring attention. | Red |
| `fatal` | Unhandled application/worker failures; displayed as `[CRITICAL]`. | Bold red |

Routine browser polling and state saves are trace-only. Return to `info`
after troubleshooting. Colors use ANSI escape sequences, supported by the HA
log viewer. If your viewer displays raw escape codes, set **log_color: false**
and restart; `NO_COLOR` also disables colors.

Each line has a UTC timestamp, explicit `[INFO]`/`[DEBUG]`/`[TRACE]` (etc.)
severity label, component, thread and `trace=<id>` correlation identifier.
The correlation identifier is separate from the `TRACE` severity.
Related operations in one sync/request share the ID; independent requests get
their own. Login workers inherit the initiating request's trace. Search for an
`event=...` or trace ID when investigating an error. Failure logs include
exception types and code locations. Provider/source warnings also include
bounded, single-line failure reasons: Anthropic HTTP status and API messages,
model/entity context, retry pauses and actionable billing/permission hints.
Known credentials and private URLs are redacted; authentication internals retain
type-only errors rather than exposing raw login responses.

New diagnostics never serialize request bodies, API responses, message text,
AI prompts/results, login identities, passwords or QR payloads. Request queries
and authorization/cookie values are excluded, and the output formatter masks
known credentials, bearer/JWT tokens and private ICS links as defence in depth.
Child slugs, entity IDs, counts and provider/API metadata remain visible.
Logs are still private operational data: review them before sharing, especially
old logs from versions before these safeguards. No logging service or telemetry
destination is added.

## Setup

Installed iOS web apps reserve the status-bar/Dynamic Island safe area above
the header. After an update, reopen the app to receive the refreshed offline
shell; no reinstall is needed.

Search has a clear button. Text fields use iOS-safe font sizes to avoid
automatic focus zoom. Page pinch-to-zoom is disabled at the user's request;
normal scrolling remains available. Native document viewers may retain their
own zoom controls.
When a new version is deployed while the app is open, a toast offers
**Reload** to pick up the new features; the bottom of Settings shows the running
version for debugging.

**Settings > Appearance** offers Automatic, Light and Dark, saved on each
device and applied to the feed, settings, login and offline pages.

Install this add-on from its repository and enable Show in sidebar. Open
**Settings > Aula account login**, enter your MitID username, approve the
animated QR/OTP request in your MitID app and select a guardian identity if
prompted. No MitID password is requested.

Then wait for Last successful direct sync. The add-on talks to Aula directly;
no separate Aula or EasyIQ Home Assistant integration is needed, and no
credentials are imported from them.

Children are discovered automatically after login. Known Aula child IDs/names
are matched to their existing slugs to preserve profiles, summaries and
subscription decisions. Reconnect the same guardian when authentication expires.

## Reading and translation

Each file attachment is one compact row showing its type (PDF, DOCX, …), its
name and Download. Download saves the original file with its filename; open it
in your device's PDF reader or app. In the installed app the page never leaves
the post: on iPhone/iPad Download opens the share sheet (Save to Files, Books,
etc.); elsewhere the file is saved directly. Audio/video files also get Play.

Feed cards show counts for images, attachments, audio/video and calendar events
awaiting review, including media within conversation replies. Open the post or
conversation to view its attachments. There is no embedded PDF preview (it is
blocked inside Home Assistant), so PDFs, Office documents and other formats
download for opening in another app. Full-width image viewing is unchanged.

Files are fetched on demand through the authenticated app, not an external
document-viewing service. Aula links are refreshed when a post/message file is
opened. Only Aula media hosts are fetched, without redirects or forwarded login
credentials; the in-app limit is 25 MB per file. Unknown/active file types are
download-only, not executed. Blocked, scanning or protected Aula documents remain
unavailable until Aula permits access; secure documents may require opening Aula
and completing its additional authentication. Errors are explicit and do not
start automatic retries. Attachment contents are not read by the summary AI.

Every item's detail page (posts, messages, calendar events, week plans, homework)
has a **Learn more** section, limited to three links.
Press **Look up additional information** to start a lookup; opening an item
never starts research. Results stay on the item, including across feed refreshes
and when the post later changes (marked "the post has changed since"), until you
press **Regenerate links**. While a lookup runs, the page makes one held request
that the add-on answers when it finishes; there is no repeated polling.
The configured HA AI Task makes one short Danish topic-only query about the
item's main subject, emphasizing authoritative Danish guidance for health
topics. Because lookups are only manual, routine activities are searched too.
**Recommended:** in Settings → AI, choose a **Learn more web search** AI Task
whose model has web search enabled (for example a Claude or OpenAI AI Task in
Home Assistant with its web search option turned on). The AI then searches the
web itself and returns up to three article links; each link is checked before it
is shown (public host only, reachable, not a homepage or aula.dk). The built-in
fallback below is used when no web search AI Task is selected; free search
engines often block or rate-limit requests from a Home Assistant host.
Lice uses a predefined generic query. Built-in web search (Brave, falling back to DuckDuckGo and then Bing) supplies a bounded set of search candidates; HA AI selects
up to three directly relevant links from their titles and snippets, excluding
generic organisation pages and unrelated advice. Article pages are not fetched.
Results from before 1.9.23's relevance filter stay hidden; this never starts another lookup. Search results
are further reading, not medical advice or an endorsement.

Only generic topic keywords are sent to the search engine, not the original message or
private Aula links. The AI is instructed to omit names, institutions, addresses,
dates and other private details; query validation rejects digits, URLs and
contact syntax. Results and failures persist across restarts until source changes.
Failures are logged without private content and stop: there are no automatic
search retries. The retry button is an explicit manual action. Summaries and
translation caches are untouched.

Feed contains posts and messages. Child presence cards appear above Feed,
Calendar and Saved and act as the child filter, with checkmarks for selected
children. Toggle each child independently to include or exclude them; select
both to show everyone, or neither to hide all children's items. The
Filters panel holds content/date filters only. Cards show arrival/departure times
when reported, without a last-checked label. Recorded Aula naps show their start
time while sleeping, then their start/end times and duration after wake-up is
recorded. Multiple naps remain visible that day and survive add-on restarts.
Times update with the normal Aula poll; missing start/end times are never
guessed from arrival or polling times. Presence is not a post: it has no
translation queue, author, Read/Save/Share actions or content-type filter.
Missing/failed reports show **Unknown**; cached status expires after two poll
intervals (at least 30 minutes) or the child's local midnight, whichever comes
first. Previous-day source timestamps are also treated as stale.

Opening an item and returning with Back restores the list position, search,
filters and loaded items. Conversations retain per-message originals without
repeating the latest message in an extra Original source section.

Calendar contains official Aula events, school planning dates and AI suggestions.
Filters combine children and content types.
The date selector displays the visible range; tap it to choose a focus date,
or use Previous/Today/Next. On mobile it fills its own row above these controls.
Calendar subscription options are in Settings. The calendar's update timestamp
appears below the events, without a feed-item total.
Agenda entries and feed posts use contrasting card surfaces and borders in both
themes, with inset spacing to distinguish adjacent items.

In Calendar > Filters, uncheck **Lessons / classroom bookings** to hide timetable
entries such as BHK and IDR without hiding meetings or other calendar events.
This uses Aula's explicit lesson type/lesson ID, not title abbreviations or room
names. EasyIQ planning entries with the exact same title and start instant as an
Aula lesson share that filter; other planning entries remain under Weekplan.
Filter choices are remembered on this browser/device and do not remove events
from the calendar subscription.

Event details use native date/time pickers, displaying times in the child's
configured timezone. All-day events use **First day** and **Last day** (inclusive);
the subscription's exclusive end date is handled automatically. Leave the last
day blank for a one-day event.

English summaries give a general synthesis of the thread, emphasizing the
latest developments, current plans and actions. Clear paragraphs, emphasis and
short lists keep instructions, dates and deadlines readable without repeating
the full conversation or imposing a three-sentence cap.

Only non-English content is translated, including non-English titles.
Already-English messages remain unchanged and are not labelled translated.
Message details show the full conversation as latest-first chat below the
summary, displaying 10 replies initially and 10 more with each Show more action,
retaining authors, timestamps and per-message originals. Original
content stays readable while translation is pending or temporarily unavailable.

Post details display images using Aula's HTTPS image links, refreshing attachment
links from the original post when opened. Message attachments are also retained.
Post images use the same compact, square photo grid as albums, with **Save all
photos** to save them together. Child-tagging controls remain album-only.
Clicking a thumbnail opens a full-width viewer with the standard Share action
and ‹ / › buttons (also arrow keys and swipe) to move to the previous or next
image in the post or album; it closes with Close or Escape. The Close button stays below the phone's
status bar, outside the scrolling image area.
Missing links, failed image loads and other attachment types are explicitly
labelled with instructions to view them in Aula. Images are not sent to the AI:
summaries and extracted events cover text only and may omit image-only details.
The calendar agenda runs chronologically from the earliest to the latest date
in the selected range, including past dates. Within a day, all-day entries come
first, followed by timed events in start-time order.
Related calendar events and pending suggestions appear directly on source
details, with their evidence and existing approval/edit controls.
Past-tense reports about the children's day or a completed trip are summary
content, not new calendar events. Pending suggestions with dates before today
are retired. Upcoming activities are compared against official and approved
entries by date, child and similar titles (including common Danish/English
activity names); extraction also receives existing-calendar context. Reminders
for an already scheduled occurrence do not create another recommendation.
Different dates or separate children's activities remain distinct. Official
events and explicitly approved events are not removed by this cleanup.
The Requires attention pill opens an overview of pending calendar suggestions.
Its count includes each suggestion once, not unread Aula messages. Review
explanations are available under Review details rather than information banners.

The add-on imports a configurable number of conversations and posts
(`thread_pages`), then retrieves the replies belonging to imported threads.
`context_messages` only applies to the legacy integration message source; it is
not a limit on the conversation displayed on a message's detail page.
Full conversations are cached: a thread is refetched only when a new message
arrives, or otherwise once a day (at most three per sync).

### Aula rate-limit guardrails

Aula throttles clients that call it too often (HTTP 429). The add-on protects
the account as follows, and always keeps showing cached content:

- **Escalating pause:** Aula usually throttles one endpoint (typically the
  calendar) while the rest keeps working. On a 429 all Aula calls pause for one
  minute (or Aula's Retry-After, if longer), and the refused endpoint pauses for
  15 minutes. Each further 429 on that endpoint doubles its pause (30 min,
  1 h, …) up to 6 hours; one successful call resets it. Messages keep syncing
  meanwhile. Pauses are saved, so restarting the add-on does not bypass them.
  After the first 429 on full conversations, the rest of that sync shows cached
  messages instead of trying each remaining thread.
- **API version:** the working Aula API version is remembered across restarts
  and logins, so old versions are not re-probed.
- **Request budget:** automatic syncing makes at most 240 Aula requests per
  rolling hour (360 in total including actions you take in the app). When the
  budget is spent the add-on waits instead of contacting Aula.
- **Spacing:** requests are sent at least one second apart instead of in bursts.
- **Cadence:** messages and presence are checked every full sync; posts every
  15 minutes; the Aula calendar, EasyIQ planning and photo albums every
  30 minutes; profile lookups are reused for an hour. These are defaults;
  change them under **Settings → Sync**.
- **Near real-time messages and check-in/naps:** between full syncs the add-on
  makes a quick check (default every minute): one request for the newest page
  of conversations (default 06:00-23:00 local time) and one combined presence
  request for all children (default weekdays 06:00-19:00). A new or changed
  conversation starts a full sync immediately; check-in, check-out and nap
  changes are shown at once. Quick checks are skipped while Aula is pausing
  requests, while that endpoint is paused, or when fewer than 40 requests
  remain in the hourly budget.

Quick checks keep messages and presence current, so the full sync does not
need to be frequent: the default 15 minutes is recommended. Short intervals
add load without making messages faster.

AI enrichment runs progressively with rate limiting and retry backoff.
The backlog is processed newest first, working backwards by each entry's date
(the latest reply date for conversations). Replies are also translated newest
first. Explicit manual retries take priority; completed work is reused.
Requests use the ordered Home Assistant AI Task list under **Settings → AI**.
Each operation starts with the first entity and tries later entries only on
failure, including invalid summaries, calendar extraction or translations.
This replaces the old direct-Anthropic parallel backlog mode: fallbacks are not
used speculatively while the primary succeeds. Completed caches are retained.
Summaries and translations are saved on the persistent add-on data volume.
Changing AI entities, priority, models or add-on versions does not invalidate
unchanged source content. Cached summaries also survive temporary source
failures and moving outside the fetched feed window. Only changed source text,
dates, replies or timezone require new analysis; completed text translations
are reused independently. Use manual reanalysis when you explicitly want a
different result. Back up the add-on data to preserve this work across reinstalls.
The header shows completed/total AI tasks and an approximate completion time.
Tasks include summaries, title language checks/translations and message
language checks/translations. The estimate uses observed processing speed;
it initially says Estimating time and explicitly shows retry or source delays.
Counts can change as new content arrives.
Source failures keep cached content and show a warning. Local extractive
fallbacks are not translations. If every configured AI task fails, originals
remain readable and the queue retries with explicit error status.

For a targeted retry, the ingress-only `POST api/feed/reprocess` endpoint accepts
`{"id":"thread:<Aula thread id>"}` with `X-Aula-Request: settings`. It queues
fresh summary analysis ahead of the normal backlog and reuses completed message
translations. A `202` response confirms queuing, not successful AI completion.
The configured AI provider must be available for processing to finish.

The orange dot marks a conversation that is unread in Aula. Opening the
conversation here (not just seeing it in the feed) marks it read in Aula, exactly
as opening it in Aula does, and the dot disappears. This needs the add-on's own
Aula login; conversations in shared/common inboxes are left for Aula. A failure is
logged and not retried automatically; it is tried again the next time you open it.
There is no separate Unread tab. Saved items are browser/device-local
bookmarks. Sharing requires an explicit share/copy action.

Click a post's header bar (or its `–`/`+` button) to collapse it to a single
title line, Reddit-style. Collapsed posts are remembered in this browser
(device-local, newest 1,000) and stay collapsed after reloads and refreshes.

## Photo albums

Each photo exposes Aula's tagging permissions. Where allowed, open a photo in the full-size viewer and use the small **+ [child]** / **✓ [child]** toggle at the bottom, next to Share, to tag or untag it; navigation sits centred at the top and Close is an icon at the top right. Tagged children are shown as small badges on each photo. Tag writes use the same `files.updateAttachments` call as Aula's web gallery, keeping every other existing tag plus the photo's title and description, and are confirmed by reloading the photo's tags. Only configured children with an unambiguous identity at the album's institution are offered. Tags are never inferred from faces or automatically applied, and other people's tags are left untouched. A failed or unconfirmed write is reported in the viewer instead of being marked as successful. Albums show **Save N tagged photos** (instead of Save all) to save just the photos your children are tagged in; the count updates as you tag.

Recent Aula photo albums appear in the feed as **Album** items (filterable like other types). Open one to see all photos in a grid, tap a photo for the full-width viewer, or use **Save all photos**: the add-on fetches every image, then the second tap opens the system share sheet (30 photos per sheet). On iPhone/iPad pick *Save Images* (goes to Recents) or a Shortcut you built that saves images into a specific album — web apps cannot choose a Photos album themselves. Videos open in the player (25 MB limit). Albums do not send push notifications.

## Settings

### Sync

Settings → **Sync** controls how often the add-on contacts Aula. Changes are
saved on the add-on data volume (`sync_settings.json`) and apply immediately,
starting a sync.

| Setting | Default | Range |
| --- | --- | --- |
| Full sync | 15 min | 5-1440 min |
| Quick check (messages, check-in/naps) | 60 s | 30-900 s |
| Quick-check messages / hours | on, 06-23 | any hours, start before end |
| Quick-check check-in/naps / hours | on, 06-19, weekdays only | any hours, start before end |
| Posts | 15 min | 5-1440 min |
| Calendar and EasyIQ planning | 30 min | 10-1440 min |
| Photo albums | 30 min | 10-1440 min |

Posts, calendar and albums are fetched at the next full sync after their
interval has passed, so they are never more frequent than the full sync. The
page shows an estimate of hourly Aula requests against the 240/hour automatic
budget; the guardrails above still apply whatever you choose. **Restore
defaults** resets every value. The former `poll_interval` add-on option is
removed; an existing non-default value is migrated once into Full sync.


The settings gear contains account login, child profiles and calendar
subscription preferences, not an archive or review queue.

The MCP connector URL is available in **Settings → MCP connector** after
setting `mcp_key` in the add-on options. Treat the URL as a password. For a
stable external URL, also configure `public_url` and Cloudflare Access as
described in `CLOUDFLARE_ACCESS.md`.

Each child's display name, school/daycare type and subscription inclusion are
stored on the add-on and shared across devices. Display names do not change
source identity matching. Set **timezone** once in Home Assistant's
**Settings > Apps > Aula UI > Configuration** (default `Europe/Copenhagen`).
It applies to all children, calendar dates, presence and notifications after
restarting the add-on; child profiles no longer have separate timezone controls.

Daycare skips EasyIQ school planning requests. School planning is fetched only
when Aula offers an EasyIQ widget; account information explains availability.
Unavailable planning does not erase existing dates. Actual source errors remain
visible. Posts, messages and native Aula events are unaffected.

Children's Aula profile pictures are downloaded during sync and shown on
posts, presence cards, child filters and settings. Aula's picture links expire,
so the image is cached in the add-on data folder (`avatars/`) and served only
through ingress. It is re-downloaded weekly or when Aula reports a different
picture; failed downloads are retried after six hours and the initial letter is
shown meanwhile.

Post and message authors' Aula profile pictures are cached the same way and
shown beside their names on posts and in message threads. Pictures are matched
by the author's display name; at most 25 new pictures are downloaded per sync
and 1000 authors are remembered. Authors without an Aula picture keep their
initials.

## Notifications

Notification previews prioritize passages that explicitly name your children,
using their Aula profile names (including first names), not just the post's
audience. An independent English AI preview focuses on their activities and
parent actions without attributing other children's activities to them.
For reply alerts, only the newest message is considered. This applies to app
push and HA notify targets, including digests; feed summaries and translations
are not changed. If AI is disabled or fails, a named passage is used instead.
No historical notifications are replayed by this change.

Reply notifications track the newest message timestamp, regardless of the
order Aula returns replies. Upgrading old detection fingerprints silently
baselines existing threads rather than replaying missed historical replies.

App and Home Assistant notification types use the same compact two-column
selector with independently selectable tiles. Messages includes new threads
and replies; Lessons includes classroom bookings.
Enable **Naps** to receive recorded nap starts and wake-ups, including the
full duration and start/end times on waking. This is off by default in both
notification sections and follows their child selection. Existing naps are
baselined silently; restarts and repeated polls do not resend them. Alerts
arrive on the next normal Aula sync, not necessarily immediately.

Settings → Notifications sends new feed items through Home Assistant's notify
action. Add targets with the selector and remove them with the row buttons,
then save. Notify entities are sent with
`notify.send_message`, or choose legacy `notify.mobile_app_*` services, where tapping
the notification opens Aula UI (the item itself at `public_url`, see below). Then choose item types (posts, messages,
calendar events, lessons, homework, week plans, AI event suggestions) and
optionally limit to specific children; leaving every child unticked includes all.

Items already in the feed when notifications are first enabled are recorded as
seen and are not sent. New items wait up to 15 minutes for translation, past
calendar events are skipped, replies to a thread notify again, and more than
five new items at once become one summary notification. **Send test** delivers
a test message to the selected targets; delivery errors appear on the page and
in the add-on log.

### App notifications (installed PWA)

When Aula is installed to the home screen (or opened in a browser that supports
Web Push), Settings → Notifications → **App notifications** can deliver
notifications straight to that device instead of, or as well as, Home
Assistant. Tap **Turn on for this device**, allow notifications, and choose the
item types and children for that device; every device has its own selection,
and other devices can be removed from the same page. To use only app
notifications, untick every Home Assistant target.

- iPhone/iPad need iOS 16.4 or later, and Aula must be added to the Home Screen
  and opened from there (Safari tabs cannot receive push).
- Tapping a notification opens the item in the app.
- Delivery uses standard Web Push: a VAPID key is generated on first use and
  stored in the add-on data directory, payloads are end-to-end encrypted to
  each device, and only Apple, Google, Mozilla and Microsoft push services are
  accepted. Expired subscriptions are removed automatically.

### Notification history

The bell in the top bar lists the last 200 notifications sent (HA notify and app
notifications), newest first, from the time this feature was installed. A dot on
the bell means something is unread. Conversations are read once read in Aula or
opened here; other items are read when opened. Nap notifications are always read.

## Calendar

### Report sick today

In Calendar (or the desktop split view), each child's card has a small **Sick today** button beneath their name: empty when off, checked when reported sick. The app checks Aula's guardian Come/Go permissions automatically. Tapping asks for confirmation before changing anything, in either direction. A change is reflected only after Aula confirms it. Reporting adds a dated all-day **sick day** to the app calendar and ICS subscription; clearing removes only that child's entry for today. Repeated taps do not duplicate the event. Child filtering remains a separate button.

Clearing follows Aula's own guardian toggle (`presence.updateStatusByInstitutionProfileIds`, status `0` / `NOT_PRESENT`); it only clears identities still marked sick, never checks a child in or overwrites other current presence states. Failed writes leave the toggle unchanged and offer a status recheck.

Aula's own Come/Go interface states that children are automatically cleared for the next day. This action never creates a recurring illness or sends an automatic “healthy” update that could overwrite another presence status. The controls expire at midnight in the configured Home Assistant time zone; an open yesterday form cannot submit for the next day. Historical calendar entries remain on their original date.

**Important:** Aula Come/Go sickness reporting informs daycare/SFO staff. It does not replace reporting school absence to the school teacher or the school's attendance system. The app shows this warning before confirmation. This is separate from changing presence/check-in, and no sickness is reported merely by opening the calendar.

Official Aula calendar entries are labelled separately from AI suggestions and
require no approval. AI suggestions require parent approval before subscription
publication, with source title/date and exact original evidence visible for
verification. Review and correction happen in Calendar or event details.

Suggestions need solid evidence: a quote found verbatim in the source that
states a date. Message threads are stricter (at least six words and an
explicit date such as "31. oktober" or "i morgen"; at most three suggestions
per thread). A bare weekday or single word is never enough, and preparation or
logistics for an event already found on the same day is not suggested
separately. Pending suggestions that fail these rules are retired
automatically; the reason is logged.

Accepted AI events and official Aula events use compact agenda rows rather
than full feed cards. Open an entry for its source evidence, notes and editing.
The Calendar page groups events by date and offers date navigation; pending
suggestions remain identifiable and reviewable without expanding every source
passage. All-day end dates are exclusive in the subscription but displayed as
the last included day in the agenda.

On wide screens (900px and up, e.g. desktop or iPad in landscape) the agenda,
its date controls and its filters appear in a column to the left of the Feed.
Use **Hide calendar** to collapse it and give the feed the full width, or
**Show calendar** to reopen it. The choice is remembered on this browser/device.
The Calendar button is hidden from the bottom navigation. Narrower screens,
including iPad in portrait, keep the Calendar tab.

Unknown or ambiguous dates must be corrected rather than invented. Original
evidence stays in its source language. Manual edits, approval decisions and
stable calendar UIDs persist across restarts and reanalysis.

Calendar view filters (including future-event filters) do not change the
subscription. Child subscription settings do. Previous saved decisions remain
in storage even though Settings no longer exposes archive management.

**Event categories** in Settings → Calendar subscription lets you leave one or
more categories out of the subscription, for example Aula lessons/classroom
bookings, school-home meetings, holidays, or AI events from posts/messages.
Excluded events still show in the Calendar tab; the choice is saved and applied
on the calendar app's next refresh.

Copy the **Private calendar subscription URL** from subscription settings into
your calendar app's Subscribe/Add from URL feature. Treat the URL token as a
password; revoking it invalidates the old URL. Download ICS is a one-time import.
The URL uses `public_url`. Calendar apps cannot complete a Cloudflare Access
login, so add an Access application for `<host>/api/aula_calendar/*` with a
Bypass/Everyone policy (see CLOUDFLARE_ACCESS.md). Only that path skips Access;
the add-on still requires the exact secret token and returns 404 otherwise.
Calendar apps control their own subscription refresh frequency.

The bundled **Aula Feed Calendar** HA integration serves this private, no-cache
ICS endpoint without requiring an ingress login. Keep it for subscription use.
The writable HA configuration mount installs its small custom component and
publishes ICS files. Restart HA once after first installation if instructed.
It is supporting infrastructure, not a separate frontend.

## AI providers

**Settings → AI → AI providers** is one ordered list. Add any mix of Home
Assistant AI Tasks and own-key providers (Anthropic, Google Gemini,
Cloudflare Workers AI), reorder them with the arrows and select **Save
providers**. The first entry is used first; if it fails, the next one is tried.

For an own-key provider select **Key**, enter the API key/token (and the
Account ID for Cloudflare), optionally a model, and select **Save key**. Keys
are stored on the add-on's private data volume and are never shown again in
the UI; **Remove key** deletes it and removes the provider from the list.
Keys can alternatively be set in the add-on Configuration
(`anthropic_api_key`, `google_api_key`, `cloudflare_account_id` +
`cloudflare_api_token`; model overrides `anthropic_model`, `google_model`,
`cloudflare_model`; defaults `claude-haiku-4-5`, `gemini-2.5-flash`,
`@cf/meta/llama-3.3-70b-instruct-fp8-fast`); keys entered in the app take
precedence. Content is sent to the providers in the list. Cloudflare's free
tier is about 10,000 neurons/day. An empty list uses Home Assistant's default
AI Task.

## Add-on options

| Option | Default | Purpose |
| --- | --- | --- |
| `thread_pages` | `2` | Pages of conversation history and 20-post batches per child (1-20). |
| `event_lookahead_days` | `21` | Calendar lookahead (1-120 days). |
| `log_level` | `info` | Logging verbosity. |
| `log_color` | `true` | Color severity labels; disable for plain-text output. |
| `access_team_domain` | empty | Cloudflare Access team domain, e.g. `myteam.cloudflareaccess.com`. |
| `access_aud` | empty | Application Audience (AUD) tag of the Access app. |
| `access_allowed_emails` | empty | Emails allowed through public access (see CLOUDFLARE_ACCESS.md). |
| `mcp_key` | empty | Enables the MCP endpoint `/mcp/<key>` (32-128 chars of `A-Za-z0-9_-`). Empty disables it. |
| `mcp_allow_writes` | `false` | Adds the `report_sickness` MCP tool. |
| `public_url` | empty | Optional public base URL (e.g. `https://aula.example.com`) used for notification deep links and the calendar subscription URL. When empty, notifications open the Home Assistant sidebar panel and the calendar URL is built from the address you opened the app with. |

Children are discovered after account login; their names, institution type and
calendar inclusion belong in built-in Settings, not the App installation form.
HA credentials come from the Supervisor, not manually configured tokens.

Home Assistant AI Task entries in the AI providers list use HA's
`ai_task.generate_data` service; their models and keys are managed in Home
Assistant. Missing/unavailable saved entries remain visible and can be removed
or reordered. Every request starts at the top of the list again. Existing
priority lists and the 1.9.36 "AI source" choice migrate automatically.

## Persistence and compatibility

`/data/state.json` stores cached feed content and generated summaries.
`/data/profiles.json` stores shared profile/calendar preferences and decisions.
Encrypted Aula tokens and their private key live under `/data/aula_auth/`.
Back up the entire add-on data directory; the private key and encrypted tokens
must be restored together, not regenerated.

Housekeeping keeps these files bounded: cached summaries/translations for
content no longer in the feed are dropped after 90 days, and archive flags or
declined/pending reviews of events more than 180 days in the past are pruned
once a day. Accepted events and anything current or upcoming are never pruned.

The add-on does not create Home Assistant entities or read helper entities.
Earlier versions published `sensor.aula_*`, `binary_sensor.aula_present_*` and
`sensor.aula_ui_status`; those states are no longer updated and disappear on the
next Home Assistant restart. The `input_boolean.aula_pin_thread_*` and
`input_boolean.aula_hide_event_*` helpers are no longer used and can be deleted.
No generated dashboards, sample helper packages or automation files are shipped.
