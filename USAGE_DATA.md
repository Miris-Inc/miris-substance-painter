# Usage data

The Miris plugin for Adobe Substance 3D Painter can report anonymous usage data
to Miris. It is opt-in. The first time you open **Miris → Publish…**, the plugin
asks once whether to share, with **Share** and **Don't share**. Until you choose
**Share**, nothing is sent and no ID is created. This is stricter than the
opt-out default most tools use, where reporting is on until you turn it off.

## Why we collect it

Usage data shows us which exports, uploads and sign-ins fail and how often, so
we can fix what breaks. It also shows which features get used, which tells us
what to build next.

## What is sent

Each report is a list of entries. Every entry carries the same envelope:

| Field | Value |
|---|---|
| `name`, `type` | the event, from the tables below |
| `platform` | always `desktop` |
| `startTime` | milliseconds since the plugin loaded |
| `duration` | always `0` |
| `sessionId` | a random ID made each time the plugin loads |
| `entryId` | the entry's position in the session: 0, 1, 2 and so on |
| `targetId`, `targetType` | `substance-painter` and `integration`, or empty and `session` for the session entry |
| `data` | the event's own fields, below |

The first report of a session opens with these entries:

| Event | When | Data fields |
|---|---|---|
| `session` (type `session`) | first report of a session | `absoluteStartTime`: when the session started, in milliseconds since 1970 |
| `configuration` (type `configuration`) | first report of a session | `integration` (`substance-painter`), `integrationVersion` (the plugin version), `host` (`substance-painter`), `hostVersion` (the Painter version), `os` (`darwin`, `windows` or `linux`), `installId` (see below) |
| `install` (type `lifecycle`) | first report after the install ID is created | none |

Then the events themselves:

| Event | When | Data fields |
|---|---|---|
| `enable` (type `lifecycle`) | the plugin loads | none |
| `disable` (type `lifecycle`) | the plugin unloads | none |
| `sign-in` (type `lifecycle`) | you sign in to a destination | `destination` (for example `miris` or `playbook`), `method` (`device-grant` or `oauth2-pkce`) |
| `sign-out` (type `lifecycle`) | you sign out of a destination | `destination` |
| `update-available` (type `lifecycle`) | the plugin finds a newer release | `latestVersion` |
| `click` (type `interaction`) | you choose **Publish…** or **About Miris…** in the Miris menu, or **Publish**, sign in, cancel, copy link or open link in the publish window | `control` (`publish`, `about`, `sign-in`, `cancel-publish`, `copy-link`, `open-link`), `view` (`miris-menu` or `publish-window`) |
| `export` (type `interaction`) | an export finishes | `destination`, `format` (`usdz`), `sizeBytes` |
| `upload` (type `interaction`) | an upload finishes | `destination`, `format` (`usdz`), `sizeBytes` |
| `error` (type `error`) | an export, upload or sign-in fails | `code`: the step and the error class, such as `upload-TimeoutError` |

## What is never sent

Your name, email, account, workspace, asset names or IDs, file paths, project
contents, tokens, or error messages. An error is reported only as its class
code, never its text.

## The install ID and your IP address

A random install ID is created the first time a report is sent after you opt
in, so reports from one installation can be counted together. It is stored as
`install_id` in `~/.miris/substance-painter/ui_prefs.json` and is not linked to
your Miris account. Deleting it, or the file, starts a new one.

As with any web request, the server receiving the reports sees your IP address and the request's user agent, which is `miris-substance-painter/<version>`.

## Where it goes

Reports are sent as a GET request to a Miris pixel URL. Miris names that URL in
the `Miris-Config` header of its API responses, so nothing is sent until the
plugin has talked to Miris, for example by signing in, and only while Miris has
usage reporting switched on for its service.

## How to turn it off

- In the publish window, open Settings and clear **Share anonymous usage data**.
- Or set the `DO_NOT_TRACK` environment variable to `1` for Painter. While it is
  set, nothing is recorded or sent and no install ID is created, whatever you
  answered. Any value other than empty or `0` counts.

## How to see what is sent

Set `MIRIS_TELEMETRY_DEBUG=1` for Painter. Each report's entries are then
written to Painter's log, as a line starting `[miris] usage report:`, right
before the report is sent. Open the log with **Window → Views → Log**. Nothing
is logged when nothing is sent.

On macOS, an app opened from the Dock or Finder does not read your shell's
environment, so start Painter from Terminal to use either variable, for example:

```sh
MIRIS_TELEMETRY_DEBUG=1 "/Applications/Adobe Substance 3D Painter/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter"
```

## Privacy policy

See the [Miris privacy policy](https://www.miris.com/privacy-policy).
