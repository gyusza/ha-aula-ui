# Changelog

## 1.9.62

- Give feed item types subtle distinct card tints: peach posts, blue messages,
  lavender albums, green calendar items, teal weekplans, gold homework and rose
  presence updates. Tints adapt to light/dark themes and remain visible on hover;
  detail pages retain their neutral background.

## 1.9.61

- Recover intermittent image failures with bounded retries. Retried previews
  replace cached bytes and refresh Aula's signed image links; album tiles can
  fall back to medium thumbnails if the small thumbnail remains unavailable.
- Replace broken-image icons with an explicit unavailable message and Retry.
  Covers, post images and full-size viewers also recover without losing image
  navigation; failed post viewer images can retry through the add-on.

## 1.9.60

- Cache album covers, album thumbnails and post/message image previews privately
  inside the add-on. Repeat views reuse saved image bytes, including after a
  restart, instead of downloading each preview from Aula again.
- Previews expire after seven days; the cache is limited to 128 MB and 512
  images. Full-size viewers and original downloads are unchanged, and failed
  image loads are not cached as successful previews.

## 1.9.59

- Added an MCP connector entry to Settings with the configured endpoint URL and
  a copy action. Warn when the key is disabled, ingress URL is temporary, or
  MCP write tools are enabled.

## 1.9.58

- Removed the retired Siri connector from Settings, the API and cached assets.
- Existing Siri key hashes are removed from saved profiles on startup.

## 1.9.57

- Added an MCP endpoint at `/mcp/<mcp_key>` for remote connectors such as claude.ai.
  Set `mcp_key` (32+ chars) to enable it; `mcp_allow_writes` adds `report_sickness`.
  The Siri connector is kept.

## 1.9.55

- Removed album multi-select tagging; tag photos from the full-size viewer.
- Albums now offer **Save N tagged photos** instead of Save all, saving only
  photos your children are tagged in and updating as tags change.
- Keyboard focus stays on the viewer's tag toggle after saving, so arrow keys
  keep navigating.

## 1.9.54

- Album photo tagging is now multi-select: tick photos (or **Select all**) and
  use one **Tag [child]** button at the top of the album, which saves and
  confirms each photo in turn. Tagged children appear as badges on the photos.
- The full-size image viewer gained a tag toggle. Its controls are smaller:
  previous/next centred at the top, Close as an icon at the top right, tag and
  Share at the bottom, and the image centred vertically.

## 1.9.53

- Fixed photo tagging: Aula's web API has no `gallery.addTag` method or
  media-tags REST route (both return HTTP 404). Tags are now saved with
  `files.updateAttachments`, the call Aula's own web gallery uses, sending the
  full tag list so other people's tags, the title and the description are kept.
  The result is still confirmed against the reloaded album.

## 1.9.52

- Fixed explicit photo tagging by using Aula's gallery media-tags REST routes
  instead of the obsolete `gallery.addTag` / `gallery.removeTag` RPC methods.
  Changes are still verified against the reloaded album before showing success.

## 1.9.50

- The feed shows 20 items before Show more (was 30); each Show more adds 20.
  The Calendar list still shows 30 events per step.

## 1.9.51

- Wide-screen calendars now appear on the left of the feed. Hide or reopen the
  sidebar with Hide calendar / Show calendar; the choice is remembered per browser.
- Collapsing the calendar gives the feed the full width. Mobile calendar
  navigation and existing calendar filters are unchanged.

## 1.9.49

- Prepared for public release: nothing is tied to one family or domain.
  `public_url` now defaults to empty; existing installs keep their saved value.
- Daycare colouring follows each child's school/daycare setting instead of a
  child slug. Legacy profiles no longer guess the institution type from a slug.
- Notification taps without a `public_url` open this add-on's own sidebar panel
  (derived from the container hostname), so repository installs work too.
- Added option descriptions (`translations/en.yaml`), generic Siri examples,
  a generic Cloudflare Access guide and a public README. Test fixtures use
  fictional names only.

## 1.9.48

Quality audit: bug fixes, fewer Aula requests, faster UI and quieter logs.

- **Sign-in:** an expired access token no longer fails sync as "missing or expired tokens" when profile lookups are cached; the login client's 401 retry no longer crashes.
- **Fewer Aula requests:** the working API version is remembered, so restarts no longer probe old versions (HTTP 410). After the first 429 on conversations, the rest of the sync uses cached messages. Quick checks skip endpoints that are paused.
- **Logs:** yesterday's naps just after midnight no longer log hundreds of warnings. Rate-limit pauses and AI source-lookup failures are warnings, not errors. The obsolete "aula.api_call" wording is gone.
- **Calendar:** EasyIQ all-day events whose end equals their last day are no longer rejected. The ICS file is only rewritten when events change.
- **AI:** fenced or slightly malformed model JSON no longer discards a whole analysis. Unexpected provider responses fail over to the next provider. A repeatedly failing item no longer blocks the AI queue.
- **Performance:** feed rebuilds during AI batches are coalesced (at most one every 2 s). `state.json` is written compactly and atomically. Feed polls with unchanged data no longer re-render the page or reset scroll position on thread/post pages. Shell assets are revalidated with ETags (304), and the service worker fetches app files from the network first, so updates appear immediately.
- **Storage:** stale summary/translation caches, old calendar archive flags/reviews and empty calendar groups are pruned.
- **UI:** stale responses can no longer overwrite newer views. Accept/Decline cannot double-submit. AI evidence translations render formatting. "Save photo" is used for single photos. Touch targets and focus rings are improved.
- **Security:** `frame-ancestors 'self'` and security headers on every response (including errors). 60 s request-read timeout. Cloudflare Access key fetch failures are throttled. The VAPID key is created owner-only. `requests` is updated to 2.32.5 (CVE-2024-47081).

## 1.9.47

- Replace the large sickness panel with a compact empty/checked **Sick today** button beneath each child's name.
- Require confirmation before reporting or clearing sickness, and display changes only after Aula verifies them.
- Clearing removes today's sick-day calendar entry without affecting other dates, children or non-sick presence states.
- Preserve the 1.9.46 post photo grid and image viewer navigation.

## 1.9.46

- Post images now use the same full-size square grid as albums (the thumbnails were shrunk by default figure margins).
- The full-size image viewer has previous/next buttons with a position counter, plus arrow-key and swipe navigation, for both posts and albums.

## 1.9.45

- New Settings → Sync page controls full sync, quick-check interval and hours for messages and check-in/naps, and the post, calendar/planning and album intervals, with an hourly request estimate. Changes apply immediately.
- The `poll_interval` add-on option is removed; an existing non-default value is migrated once into Settings → Sync.
- Calendar shows its visible range in the date selector, keeps subscription links in Settings only, and moves Updated below events without the item total.

## 1.9.44

- Siri & Shortcuts settings: create, replace or revoke a separate connector key, stored hashed and shown only once.
- Personal shortcuts can speak the latest cached posts for one child or report that child sick for today after explicit confirmation and a server-local date check.
- Sickness uses the same Aula permission checks and one-day calendar entry as the Calendar action. Recaps make no new Aula or AI requests.
- Includes iPhone setup recipes and the required exact-path Cloudflare Access rule; ordinary app and settings routes remain protected.

## 1.9.43

- Calendar: report a child sick for today through Aula's guardian Come/Go API, with permission checks and explicit confirmation.
- Confirmed reports create a dated all-day sick-day entry in the calendar and ICS subscription. Reporting never repeats automatically.
- Controls expire at local midnight and reject stale dates. Aula clears sickness for the next day; daycare/SFO reporting may still require a separate message to the school teacher.

## 1.9.42

- Messages and check-in/nap state are now near real time: between full syncs
  the add-on checks every minute with one Aula request each (newest message
  page; one combined presence request for all children). A new or changed
  conversation starts a full sync at once; presence changes appear straight
  away. Message checks run 06:00-23:00, presence checks on weekdays
  06:00-19:00 (local time). Quick checks skip while Aula is pausing requests or
  when fewer than 40 requests remain in the hourly budget.
- Posts (15 min), the Aula calendar, albums and now EasyIQ planning (30 min)
  stay on their slow cadence. Presence for all children is read in a single
  request. The automatic sync budget is raised to 240 requests per hour (360
  total) to make room for quick checks.
- Post images now use the album-style photo grid and Save all photos, without
  tagging controls.

## 1.9.41

- Rate-limit escalation is now per Aula endpoint: a 429 pauses all calls for
  one minute but escalates (15 min doubling to 6 h) only for the refused
  endpoint, so a throttled calendar no longer stops message syncing.

## 1.9.40

- Aula rate-limit guardrails: a 429 now pauses all Aula calls for at least
  15 minutes, doubling on repeated 429s (max 6 h) and surviving restarts;
  automatic sync is capped at 120 Aula requests per hour; requests are spaced
  one second apart; the Aula calendar and albums refresh every 30 minutes,
  posts every 15 minutes, and profile lookups are reused for an hour.
- AI settings are now a single ordered **AI providers** list mixing Home
  Assistant AI Tasks with Anthropic, Google Gemini and Cloudflare Workers AI.
  Own-provider keys can be entered in the app (stored server-side, never shown
  again); add-on Configuration keys still work.

## 1.9.39

- Add a Share action to every full-screen image viewer, including gallery
  images. Share the image file when supported, with a link-sharing fallback.
- Add explicit per-photo child tagging in Aula albums. Only children with a
  verified identity and Aula permission are offered; tags are never inferred
  or applied automatically.
- The add-on no longer creates Home Assistant entities. `sensor.aula_*`,
  `binary_sensor.aula_present_*` and `sensor.aula_ui_status` are not updated
  and disappear after the next Home Assistant restart. The
  `input_boolean.aula_pin_thread_*` / `aula_hide_event_*` helpers are no longer
  read and can be deleted. This also removes one AI call per new message that
  only fed the old overview sensor.

## 1.9.38

- New **Album** feed item type for Aula photo albums (recent albums, attributed to the child whose institution they belong to). Titles and descriptions are translated like posts.
- Album detail page shows the whole photo grid with a full-width viewer; videos open in the player.
- **Save all photos** prepares every photo and opens the share sheet (batches of 30). On iPhone choose *Save Images*, or a Shortcut that files them into an album. Web apps cannot pick a Photos album directly.
- Photos are proxied through the add-on because Aula's signed links expire within the hour. Albums do not trigger push notifications.

## 1.9.36

- Optional AI source: use your own Anthropic or Google Gemini key, or
  Cloudflare Workers AI, instead of Home Assistant AI Tasks. Add the keys in the
  add-on Configuration tab, then choose the source in Settings → AI. Nothing
  changes unless you pick one. "Learn more" web search still uses Home Assistant.

## 1.9.35

- Bell icon in the top bar opens the notification history (newest first) with a
  small dot when anything is unread. History starts from this release.
- Read state follows the underlying item: opened/read Aula conversations are
  read; posts and other items become read when opened from the app or the list.
  Nap start/end notifications are always read.

## 1.9.34

- Opening an unread conversation marks it read in Aula (the orange dot), like
  opening it in Aula. Seeing it in the feed does not. Shared inboxes are skipped.

## 1.9.33

- Disable page pinch-to-zoom on feed, account and offline pages while preserving
  scrolling and iOS safe-area layout.

## 1.9.32

- Download in the installed app no longer opens the file inside the app window
  with no way back. On iPhone/iPad it opens the share sheet (Save to Files, open
  in Books or another app); elsewhere the file is saved directly.

## 1.9.31

- File attachments are now one compact row (type badge, name, Download) instead
  of separate Open/Download buttons and a repeated filename. The embedded PDF
  preview, which was blocked inside Home Assistant, is removed; audio/video keep
  a Play button.

## 1.9.30

- Add a direct Download action beside file attachments, including PDFs, without
  opening the preview. The preview's Download action uses the authenticated
  server download endpoint and preserves the filename.
- Keep PDF download guidance visible when embedded previews are blocked.

## 1.9.29

- Learn more can use a web-search-enabled HA AI Task (Settings → AI →
  Learn more web search). Free search engines block or rate-limit the Home
  Assistant host, so lookups often found nothing. Returned links are verified
  (public, reachable, not a homepage or aula.dk) before being shown.

## 1.9.28

- Render basic HTML lists, paragraphs and emphasis in existing summaries as
  readable formatting instead of raw tags. Unsupported HTML remains inert text;
  no source HTML or attributes are inserted into the page.
- Request Markdown-only output for newly generated briefings.

## 1.9.27

- Learn more: searches Brave first, then DuckDuckGo, then Bing. Bing's RSS feed
  returned unrelated pages (TV listings, foreign forums) for most multi-word Danish
  queries, so lookups often ended with no links. If the AI picks a URL that isn't
  in the search results, it is now dropped instead of failing the lookup.
- The AI now writes short keyword queries, and each lookup logs how many
  candidates were found and selected (no private text).
- Learn more works on calendar entries (previously "not found").

## 1.9.26

- Interim release: DuckDuckGo search (blocked from the HA host) and calendar
  lookups; superseded by 1.9.27.

## 1.9.25

- Learn more: links stay on the item after a lookup (across feed refreshes and
  later post edits) and a **Regenerate links** button reruns the lookup on
  request. No more 3-second polling: the page makes one held request that the
  add-on answers when the lookup finishes.
- Learn more is available on every item type, and routine activity posts are no
  longer excluded, since lookups only ever run when you press the button.

## 1.9.24

- Open PDF attachments inside the app, with download and separate-view controls.
  Preview supported images/audio/video; download other formats to a compatible app.
- Fetch post/message files through authenticated app routes, refreshing Aula links
  and restricting downloads to Aula media hosts, no redirects and 25 MB per file.
- Add feed indicators for pending calendar events, images, files, audio and video,
  including attachments inside conversations. Keep blocked/protected files explicit.

## 1.9.23

- Exclude routine activity reports from suggested research topics; filter search
  titles/snippets for direct relevance instead of showing the first three hits.
- Remove stale unfiltered links from display without starting another lookup.
  Keep failed requests stopped and manual lookup unchanged.

## 1.9.22

- When a new version is deployed while the app is open, a toast offers
  **Reload** (checked every 5 minutes, on return to the app and on service
  worker updates). Reload installs the new service worker first so the fresh
  app shell is used.
- Settings shows the running app version at the bottom for debugging.

## 1.9.21

- Simplify manual further reading to a qualified topic-only web search, returning
  at most three actual search results instead of AI-guessed URLs/page verification.
- Log failed lookups and stop; never retry searches automatically.

## 1.9.20

- Start new briefing summaries with substantive news or actions instead of
  introductory filler describing the briefing. Existing cached summaries
  remain intact; no bulk reanalysis is triggered.

## 1.9.19

- Calendar subscription URL is back on the add-on host (`public_url`, e.g.
  `https://aula.example.com/api/aula_calendar/<token>.ics`). The add-on serves
  that exact secret-token path without Access login (pair with a Cloudflare
  Access Bypass for `/api/aula_calendar/*`); wrong tokens return 404. Removed the
  `calendar_base_url` option from 1.9.17. The Home Assistant URL keeps working.

## 1.9.18

- Replace automatic further-reading lookup with a Look up additional information
  button, including for lice messages. Keep verified sources, the three-link
  limit and persistent cached results unchanged.

## 1.9.17

- Calendar subscription links now use the public Home Assistant URL
  (`calendar_base_url`) instead of the Cloudflare Access-protected Aula UI
  hostname, so phone calendar apps can subscribe.

## 1.9.16

- Add Learn more to message/post details, with at most three public sources.
- Include researched Danish patient guidance and combing instructions for lice.
- Other topics use the configured HA AI Task to identify relevant public
  articles, fetch and verify them, then assess relevance before displaying.
- Cache source lookups independently of summaries; private messages never go
  into search queries or public URLs.

## 1.9.15

- Prioritize what posts and new replies say about children named in their
  content, using names from Aula child profiles, in app and HA notifications.
  Generate the English preview separately; feed summaries and translations
  are unchanged. Reuse previews across targets and fall back to named passages
  if AI is unavailable.

## 1.9.14

- Detect new replies and generate notification previews using the latest
  message timestamp, independent of Aula's message ordering.
- Silently migrate old notification fingerprints without replaying threads.

## 1.9.13

- Add opt-in Naps notifications for recorded sleep starts and wake-ups with
  full nap duration, for app devices and Home Assistant targets.
- Persist nap notification baselines to avoid old or repeated alerts.

## 1.9.12

- Fix the calendar focus-date field overflowing its box on iOS/iPadOS Safari.
- The wide-screen calendar column now extends to the bottom of the screen, and
  the Feed/Saved bar is centred under the feed so it never covers the calendar.

## 1.9.11

- On desktop and iPad landscape (900px+), the calendar agenda with its date
  controls and filters sits in a column beside the Feed, and the Calendar
  navigation button is hidden. Phones and iPad portrait keep the Calendar tab.

## 1.9.10

- Compact notification type and child selectors into aligned two-column
  tiles in both app and Home Assistant settings, with shorter type labels.

## 1.9.9

- Toggle child cards independently: select either child, both or neither,
  consistently across Feed, Calendar, Saved and returning from details.

## 1.9.8

- Add search clearing, remove the inactive Newest badge and prevent iOS input
  focus zoom while preserving pinch-to-zoom.
- Make presence cards the child selectors on Feed, Calendar and Saved, with
  selected checkmarks, and restore list position when returning from an item.
- Simplify conversations and helper text, add subtle glass navigation and
  device-local Automatic/Light/Dark appearance settings.
- Move the shared timezone to add-on configuration for all children.
- Use add/remove target rows for Home Assistant notification settings.

## 1.9.7

- Show Aula-recorded nap start times while sleeping, then each nap's duration
  and start/end times after waking. Keep nap intervals in persistent state.
- Remove the last-checked line from the presence strip.

## 1.9.6

- Keep the full-screen image Close button below the iOS status bar and outside
  the scrolling image area, so it remains reachable for tall images.

## 1.9.5

- Web Push: stop sending the optional `Topic` header (Apple's push service answered HTTP 400) and log the push service's rejection reason.

## 1.9.4

- Top bar now holds only the logo, search and Settings so it fits on phones.
- The signed-in user and the Refresh button moved to a card at the top of
  Settings, which also shows the last sync time and refresh status.

## 1.9.3

- App notifications: an installed Aula PWA can receive notifications directly
  through standard Web Push (VAPID, RFC 8291 encryption) instead of, or as
  well as, Home Assistant notify. Turn it on per device in Settings →
  Notifications and choose item types and children per device; send a test,
  turn off, or remove other devices. Taps open the item in the app.
- Browser test harness now emulates Home Assistant ingress (`X-Ingress-Path`),
  so deep-link routing is exercised as in production.

## 1.9.2

- Share now opens the system share sheet with the item's title, a summary and the deep link (falls back to copying all three). The separate Copy link button is gone.

## 1.9.2

- Use more compact typography while retaining comfortable touch targets.
- Reserve the iOS top safe area above sticky navigation and account/offline
  content, keeping installed-PWA controls below the status bar/Dynamic Island.
- Refresh the service-worker shell cache so installed apps receive the fix.

## 1.9.1

- Header shows who is signed in: `HA` via Home Assistant ingress, or the Cloudflare Access email on the public domain (new `GET /api/whoami`).

## 1.9.0

- Optional public access through a Cloudflare Tunnel protected by Cloudflare
  Access. New options `access_team_domain`, `access_aud` and
  `access_allowed_emails`; requests not coming from Supervisor ingress are only
  accepted with a valid `Cf-Access-Jwt-Assertion` (RS256, JWKS-verified) and
  are refused when the options are unset. Ingress is unchanged.
- Serve `/manifest.webmanifest`, `/sw.js`, `/icons/*` and deep links
  (`/post/<id>`, `/thread/<id>`, `/child/<slug>`, `/calendar-view`) with a
  `<base href>` so relative URLs work at the ingress prefix and at `/`.
- Send HSTS on non-ingress responses; CSP now allows manifest and worker.

## 1.8.22

- Stop Aula rate limiting (HTTP 429): full conversations are cached and only
  refetched when a new message arrives or once a day (at most 3 per sync),
  instead of every sync.
- When Aula answers HTTP 429, all Aula requests pause (Retry-After, default
  30 minutes) and cached content is shown; failed conversations are reported
  as one summary line instead of one error per thread.

## 1.8.21

- Show Aula profile pictures for post and message authors (teachers, staff,
  parents) next to "By …" on posts and in message threads. Up to 25 pictures
  are downloaded per sync and cached like children's pictures.

## 1.8.20

- Accept profile pictures served with a generic content type by checking the
  image bytes, log why a picture download failed and retry after upgrades.

## 1.8.19

- Show each child's Aula profile picture on posts, presence cards, child
  filters and settings. Pictures are cached locally and served via ingress.

## 1.8.18

- Add Settings → Notifications: send new posts, messages, events, homework and
  other selected item types through Home Assistant notify entities or
  `mobile_app` services, with per-child filtering and a test button.
- Existing items are baselined, translations are awaited briefly and bursts
  are digested into one notification.

## 1.8.17

- Move AI configuration to Settings → AI with add/remove and ordered HA AI
  Task priorities; fall back in exact order for summaries, events and translations.
- Remove direct Anthropic/command transports and provider/model/key options
  from App admin. Migrate single HA entity selections and retain completed work.
- Replace speculative dual-provider backlog processing with priority-only fallback.
- Rename Home navigation to Feed and hide completed-task header indicators.
- Improve long-chat readability with safe paragraphs, lists and emphasis.

## 1.8.16

- Keep the calendar agenda in chronological order throughout the selected range,
  rather than placing past dates after upcoming events.
- Order same-day all-day entries before timed events, then by start time.

## 1.8.15

- Allow calendar subscriptions to exclude selected event categories.
- Give mobile Calendar a full-width date picker, separate navigation row,
  cleaner heading and roomier, readable agenda entries.
- Increase card/background and border contrast in dark and light themes;
  separate mobile posts with inset cards and consistent spacing.

## 1.8.14

- Exclude past-tense day/trip reports and past-dated activities from new calendar
  recommendations, including cleanup of pending suggestions.
- Compare upcoming activities with official and approved calendar entries,
  including translated/similar titles, dates and child assignments.
- Give AI extraction existing-calendar context to avoid reminder duplicates;
  keep official events and parent approvals unchanged.

## 1.8.13

- Give the mobile feed more breathing room with inset presence cards, wrapping
  headings, clearer post separation and roomier text, badges and actions.

## 1.8.12

- Require stronger dated source evidence for AI calendar suggestions, with
  stricter message-thread rules and a maximum of three suggestions per thread.
- Suppress same-source same-day duplicates and speculative preparation events.
- Retire existing weak pending suggestions while retaining parent decisions and
  official Aula events.

## 1.8.11

- Accelerate backlogs of at least 200 pending tasks with independent HA AI Task
  and Anthropic translation work, limited to one request per provider.
- Stop scheduling parallel work below the threshold, preserve provider pacing
  and completed caches, and pause parallel mode after a provider failure.
- Keep normal summary processing and primary-with-fallback behavior for smaller
  backlogs or when a second provider is unavailable.

## 1.8.10

- Preserve digit sequences and 24-hour times in translations, with actionable
  validation feedback when numeric details are missing.
- Accept structured translation objects surrounded by model commentary and
  report JSON parsing failures without logging response contents.
- Retain completed translation caches rather than reprocessing the backlog.

## 1.8.9

- Include actionable provider and source failure reasons in warning/error logs,
  including Anthropic HTTP status, API errors and billing/permission hints.
- Explain both provider failures and AI retry pauses while retaining credential
  redaction and bounded, single-line error descriptions.

## 1.8.8

- Collapse and expand post bodies from their header, retaining a compact title.
  Remember collapsed posts on the device across refreshes and page reloads.
- Support keyboard toggling while keeping original-source menu actions separate.

## 1.8.7

- Replace the unread total with a Requires attention link to pending calendar
  suggestions, deduplicated across their source posts and calendar entries.
- Remove repetitive calendar information banners; retain review explanations
  under expandable Review details alongside approval and correction controls.

## 1.8.6

- Simplify image sections and open clicked images in a full-width viewer.
- Add a persisted lesson filter using Aula's structured lesson metadata.
- Use timezone-local calendar date/time pickers and inclusive all-day end dates.
- Replace presence posts with a compact per-child current-status strip on Home,
  including reported arrival/departure times and last checked time.
- Show Unknown for missing, failed or stale reports; expire cached status at
  local midnight or after two poll intervals (minimum 30 minutes).
- Remove presence from content filters and AI processing, including legacy cards.

## 1.8.5

- Rename the header progress indicator to "Pending tasks".
- Show related calendar suggestions directly on their source detail page,
  including evidence and approval/correction controls.
- Preserve image and attachment metadata, display available Aula images and
  explicitly flag inaccessible attachments and image-blind AI summaries.

## 1.8.4

- Explicitly order every AI processing batch newest first, including restored
  backlogs, while preserving manual retry priority and completed work.
- Translate the most recent replies first within each conversation.

## 1.8.3

- Separate TRACE from DEBUG; classify verbose polling, storage and request
  boundaries at TRACE, API responses at DEBUG, normal activity at INFO and
  failures at WARNING/ERROR/CRITICAL.
- Add explicit bracketed severity labels and ANSI severity colors, with
  `log_color: false` and `NO_COLOR` support for plain-text logs.

## 1.8.2

- Add a compact header indicator for completed/total AI tasks and approximate
  completion time, including explicit retry and source-waiting states.

## 1.8.1

- Use the same original icon for App branding, feed/account headers and
  browser favicons, serving a single vector source through ingress.
- Add correlated, timed operation logging for sync, direct API requests, login,
  AI processing, calendar actions, profile/AI settings and persistence.
- Log status/retry/cache decisions and item counts at normal verbosity; keep
  routine ingress polling and detailed HA/transport/storage activity at debug.
- Protect log output with credential/query/ICS-link redaction, content-free
  failure stacks and safe ingress access logs; remove message-title/state-value
  logging. Include main-thread, worker and ingress failure reporting.
- Document log access, verbosity, correlation IDs, retention and privacy.

## 1.8.0

- Replace oversized accepted-event cards with compact agenda entries; keep
  source evidence and corrections available on the detail page.
- Add a date-grouped calendar agenda with date navigation and distinct
  official Aula, approved AI and pending suggestion classifications.
- Simplify App admin configuration around the authenticated add-on workflow.
  Discover children after login and select available HA AI Task entities
  from a dropdown in the built-in Settings page.
- Automatically fall back from HA AI Task to Anthropic when an Anthropic
  key is configured, including summaries, translations and event extraction.

## 1.7.2

- Show conversations latest first, initially displaying 10 messages with
  a Show more control that reveals the next 10 without losing loaded replies
  during automatic feed refreshes.
- Support a targeted, ingress-authenticated summary retry that prioritizes one
  entry while retaining its completed message translations.

## 1.7.1

- Preserve already-English messages unchanged and distinguish them from
  translations in mixed-language conversations.
- Present a formatted general thread summary, prioritizing the latest
  developments and current actions rather than repeating the conversation.
- Style full conversations as chronological chat messages with authors,
  timestamps and per-message originals.

## 1.7.0

- Expand English summaries, translate titles, and show complete translated
  message conversations below detail-page summaries.
- Distinguish official Aula calendar entries from AI suggestions; require
  approval of AI suggestions with their original source evidence visible.
- Move account login into Settings, simplify calendar review, and remove
  the separate Unread tab while retaining upstream unread indicators.
- Focus on the built-in ingress feed; retire generated Lovelace dashboards
  and sample helper packages.

## 1.6.2

- Replace the removed Aula widget-list endpoint with widget availability from
  the supported guardian profile context.
- Only request EasyIQ tokens for offered EasyIQ widgets. Explicitly show when
  EasyIQ is not offered on the account page and generated event-list sensors;
  keep cached dates without treating an unavailable optional source as an error.
- Continue reporting genuine failures when an offered widget cannot be read.

## 1.6.1

- Respect each child's saved school/daycare selection in direct mode: skip
  EasyIQ school planning for daycare children without reporting an error.
- Keep school planning enabled for school children and retain explicit source
  errors and cached dates. Other Aula content and recognized events are unchanged.

## 1.6.0

- Add independent Aula/MitID app login inside ingress, including animated QR,
  OTP progress, guardian identity selection, cancellation and reauthentication.
- Default to direct Aula/EasyIQ data collection; the Aula and EasyIQ HA
  integrations can be removed after a successful direct sync. HA still provides
  ingress authentication and its configured default AI.
- Encrypt and refresh the add-on's own tokens, discover children, and preserve
  existing child slugs, profile settings, summaries, calendar decisions and URLs.
- Publish add-on-owned presence sensors and remove integration-entity
  dependencies from the bundled landing dashboard. Retain explicit legacy
  `data_source: integration` compatibility mode.
- Reuse the attributed MIT EasyIQ authentication client; no stored integration
  credentials are copied or migrated.

## 1.5.1

- Move calendar events and recommendations to a dedicated Calendar page in
  the bottom navigation, separate from the Home feed. Calendar subscription
  and archive management have their own Calendar settings page.
- Combine child and content-type controls in one collapsible Filters container.
  Calendar has independent content filters and a persistent All / Hide future /
  Only future event-date selector. Undated recommendations remain visible when
  future events are hidden; today's all-day events are not treated as future.
- Remove duplicate top safe-area padding inside HA ingress to fix the mobile
  header gap. Use compact four-tab navigation on narrow screens.

## 1.5.0

- Show recognized events as individual Calendar posts in the chronological
  feed, with Save, Archive and Edit. Deduplicate shared event identities.
- Retain unvalidated/uncertain candidates as actionable recommendations with
  issue, source evidence and suggested next step. Accept, decline or correct
  their content; only valid, accepted recommendations enter the ICS calendar.
- Persist manual corrections and declines independently of AI reanalysis.
  Editing retains the event's ICS UID. Declined recommendations can be reviewed
  again from Profiles & calendar.
- Backfill existing content with recommendation-aware AI extraction while
  retaining its summaries.

## 1.4.1

- Archive recognized events from post details, child settings or the family
  calendar page. Archived events disappear from active UI lists and ICS.
- Archive choices persist across restarts and reanalysis, including duplicate
  recognitions of the same event. Restore via Profiles & calendar → Archived
  events. Original Aula posts and their summaries are retained.

## 1.4.0

- Add full-page profile settings for every configured child: display name,
  school/daycare, timezone and inclusion in the calendar subscription. Settings
  persist across devices and restarts without changing Aula source identities.
- Analyse each post/message using HA's default AI: summary plus grounded dated
  events in one structured request. Validate exact source quotes, ISO dates,
  end ordering and timezone conversion; never invent an unknown event duration.
- Publish a subscribable ICS calendar through HA's `/api/aula_calendar/` route with a
  random secret URL, stable event UIDs, deduplication, all-day/date-time support,
  child labels and source quotes. Settings include copy/download/revoke controls.
- Calendar includes source calendar/weekplan/homework events and recognized
  events from posts/messages. Retain historical recognitions across feed rollover.
- Show complete summary text without CSS line clamps. Read opens a full detail
  page with original content, attribution, translation status and recognized dates.
- Bundle a credential-free HA endpoint integration to serve `text/calendar`
  without HA static files' 31-day cache headers.
- Add writable HA config mapping to publish the ICS file and install the bridge; the interactive
  UI remains ingress-only. Calendar links are bearer secrets, not HA login URLs.

## 1.3.1

- Add combinable content-type filters to the ingress feed. Presence is hidden
  by default; Posts, Messages, Calendar, Weekplan and Homework remain visible.
- Remember content-type selections on each browser/device across reloads.
  Filters combine with child selection, search, Unread and Saved.

## 1.3.0

- Added a mobile-first, Reddit-inspired **Aula Feed** ingress page and sidebar
  panel. All content types share one newest-first timeline with child/type
  flairs, explicit translation status, timestamps, search and child filters.
- Collect Aula announcements using `posts.getAllPosts`, supplying both
  guardian and child institution profiles. Pagination uses offsets, not page
  numbers. Shared announcements are deduplicated with both child flairs.
- Mix posts, message threads, calendar/homework and current presence updates.
  Retain cached content on source errors and visibly report failures.
- Publish originals first; progressively cache summaries from HA's default AI.
  Never label untranslated extractive fallback text as translated.
- Pace feed AI requests and back off on provider/quota failures, showing a
  warning while keeping originals readable.
- Added Read/original text, local Save, Unread and device Share actions; no
  fake votes, posting or read receipts.
- Ingress-only HTTP server checks Supervisor's source IP, serves only
  allowlisted assets, and exposes no host port or browser HA credentials.
- Mobile Lovelace Home cards are full width, with compact read tiles, shorter
  summaries and timestamps. README now documents card installation and use.

## 1.2.0

- **Per-child message attribution.** Previously every child showed the same
  message, because `binary_sensor.easyiq_messages` / `binary_sensor.aula_aulamessage`
  only expose one globally-latest message with no child information. The
  add-on now calls Aula's own `messaging.getThreads` API through the
  `aula.api_call` service and matches each thread via its `regardingChildren`
  list, so each child gets their own threads.
- New option `message_source` (`auto` | `aula_api` | `sensor`). `auto` uses
  the Aula API when it is reachable and logs failures before falling back to the sensor.
- New option `thread_pages` controls how many pages of the thread list are
  fetched per cycle (20 threads per page).
- New per-child options `aula_name` (the child's display name in Aula, used
  for matching) and `aula_child_id` (match on `profileId` instead).
- `sensor.aula_thread_overview_<child>` gained `message_origin`
  (`aula_api` / `sensor`) and `unread`, a per-child unread thread count.
- The dashboard Home view now shows per-child unread badges instead of one
  global unread count.

## 1.1.0

- **Uses the AI configured in Home Assistant.** The new default provider
  `ha_ai_task` calls `ai_task.generate_data`, so summaries run on whichever
  model is selected under *Settings - Devices & services - AI Task*. No API
  key is needed in the add-on. Set `ai_task_entity` to pin a specific entity,
  or leave it empty to follow the instance default.
- Structured output (`overview` + `is_translated`) replaces the `[translated]`
  prefix parsing, which is still honoured for the text-based providers.
- Added `lovelace/aula_landing.yaml`: a streamlined dashboard organised per
  child — a Home view with one block per child, a school view
  (schedule, weekplan, homework), a daycare view (drop-off, nap,
  pickup) and a message-detail subview each. Danish presence statuses are
  translated in the UI.
- `sensor.aula_thread_overview_*` now reports `ai_task_entity` alongside
  `source`.
- The prompt template moved to `aula_ui/prompts/thread_overview.txt`.

## 1.0.0

Initial release.

- 15-minute (configurable) polling of the Aula / EasyIQ message and calendar
  entities.
- Claude summaries via the Anthropic Messages API, an arbitrary CLI
  (e.g. the Paseo CLI) or a local extractive fallback.
- `[translated]` detection with a Danish/Nordic heuristic pre-check.
- Keyword classification of calendar events into
  meeting / party / talk / exam / homework / other.
- Pin-thread and hide-event support via `input_boolean` helpers.
- State persisted to `/data/state.json` and republished after a Home
  Assistant restart.
- Ready-to-paste Lovelace cards.
- Make AI cache identity independent of provider/entity/model and software
  version; migrate completed work without regeneration and keep caches when
  sources disappear from the fetched window.
