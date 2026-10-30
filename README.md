# Aula UI for Home Assistant

A Home Assistant add-on that turns [Aula](https://www.aula.dk) — the Danish
school and daycare platform — into a fast, mobile-first family feed inside
Home Assistant's sidebar.

> Unofficial project. It is not affiliated with or endorsed by Aula, KMD,
> MitID or any municipality. It uses the same private web API as the Aula
> website, which may change without notice.

## Features

- **One feed for every child**: posts, message threads, photo albums and
  presence/nap status, filterable by child and type, with search and
  device-local bookmarks.
- **AI summaries and translations** of Danish content into English, using
  Home Assistant's AI Task (any configured model) with optional Anthropic,
  Google Gemini or Cloudflare Workers AI fallbacks. Originals are always kept.
- **Calendar**: official Aula/EasyIQ events plus AI-suggested events from posts
  and messages that you approve, published as a private ICS subscription.
- **Notifications** through Home Assistant notify services or Web Push to an
  installed PWA, per child and category.
- **Report sick today**, and an optional **Siri / Apple Shortcuts** connector.
- Built-in **MitID login** (app approval); no password is stored and no other
  Aula integration is needed.
- Respects Aula's rate limits with conservative, configurable sync intervals.

## Requirements

- Home Assistant OS or Supervised (add-ons/apps support), amd64 or aarch64.
- A guardian account in Aula with MitID (the MitID app).
- Optional: an AI Task entity in Home Assistant (*Settings → Devices &
  services*) for summaries and translations. Without AI the app still shows
  every original post and message.

## Installation

1. In Home Assistant open **Settings → Add-ons (Apps) → Add-on Store**, then
   **⋮ → Repositories**, and add this repository's URL.
2. Install **Aula UI**, start it and enable **Show in sidebar**.
3. Open **Aula Feed** in the sidebar → **Settings → Aula account login**.
4. Enter your MitID username, approve the request in the MitID app and pick
   the guardian identity if asked.
5. Children are discovered automatically. Adjust their display names,
   school/daycare type and calendar inclusion under **Settings**.

All options are optional; see the add-on's **Documentation** tab
([DOCS.md](aula-ui/DOCS.md)) for details. Access is protected by Home
Assistant ingress. Publishing the app on its own hostname (for a standalone
PWA, Web Push deep links or Siri) is optional and described in
[CLOUDFLARE_ACCESS.md](aula-ui/CLOUDFLARE_ACCESS.md).

## Privacy

Aula data and login tokens are stored only on your Home Assistant instance
(the add-on's private `/data` volume; tokens are encrypted). Content is sent
only to the AI providers you configure. The calendar subscription URL contains
a secret token — revoke it in Settings if it is ever shared.

## Development

The add-on code lives in [`aula-ui/`](aula-ui). Install
`aula-ui/requirements.txt` and `aula-ui/requirements-dev.txt`, then run:

```bash
cd aula-ui
python -m pytest -q
node --test tests/*.cjs
```

`tests/browser_verify.py` is an optional Playwright check with synthetic data.
No test uses a real MitID identity.

## Credits

Authentication and calendar helpers are vendored from
[esbenwiberg/easyiq](https://github.com/esbenwiberg/easyiq) (MIT); see
[THIRD_PARTY_NOTICES.md](aula-ui/THIRD_PARTY_NOTICES.md).

## License

[MIT](LICENSE). Vendored third-party code keeps its own licence; see
[THIRD_PARTY_NOTICES.md](aula-ui/THIRD_PARTY_NOTICES.md).
