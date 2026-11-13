# Miris for Adobe Substance 3D Painter: install

Upload the project you are working on as a single `.usdz`, without leaving
Painter. Miris and Playbook are supported today; Box and iconik are on the way.

**Requires** Adobe Substance 3D Painter 12.x (tested on 12.1.2) and an account
with whichever destination you upload to. Nothing else to install: the plugin
uses only what Painter ships with.

## Install

Use the `.pkg` on macOS or `.exe` on Windows:

1. Painter can stay open; finish any publish in progress first. If you are
   upgrading from an older or development copy, remove the old `miris` folder
   (or development link) from the plugins folder listed below. Both installers
   refuse to write through a development link, and refuse a destination holding
   anything other than a previous installation, because installing empties it
   first. On Windows, uninstall an earlier native installation through
   **Settings → Apps** first. Your sign-in and preferences are stored separately
   and remain.
2. Open the installer and follow its steps. It installs for your current user
   only; other accounts on the same machine install separately.
   On Windows the default follows your actual Documents folder, including a
   redirected one. Change the destination only if Painter uses a custom plugins
   folder, and select its `miris` subfolder.
3. If Painter is open, choose **Python → Reload Plugins Folder** (or **Miris →
   Reload**). Otherwise start Painter. The installer enables the plugin, so the
   **Miris** menu appears in the menu bar without a trip to the Python menu.

Unsigned development builds may be blocked by macOS or Windows. For wider
distribution, use a signed installer (also notarized on macOS).

### Manual ZIP installation

1. Finish any publish in progress; Painter can stay open.
2. Copy the **`miris`** folder from this archive into Painter's plugins folder:

   | | |
   |---|---|
   | macOS | `~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/` |
   | Windows | `%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Painter\python\plugins\` |

   Create the `python/plugins` folders if they are not there. You should end up
   with `…/python/plugins/miris/__init__.py`.
3. If Painter is open, choose **Python → Reload Plugins Folder**. Otherwise start
   Painter. Then turn on **miris** in the **Python** menu; the **Miris** menu
   appears in the menu bar. A manual copy is not enabled automatically; the
   installers do that for you.

## Use

**Miris → Publish…** with a project open. Pick a destination, sign in if you
have not already, give the asset a name and any tags, and publish.

Signing in to Miris opens a page in your browser to confirm the request and
choose which workspace it publishes to. The plugin never sees your password,
and the token it stores is limited to creating assets in the workspace you
picked.

## Where things are kept

Sign-in and preferences live in `~/.miris/substance-painter/`, owner-readable
only:

| | |
|---|---|
| `miris_oauth.json` | your Miris tokens |
| `playbook.json` | your Playbook tokens |
| `ui_prefs.json` | window preferences and the export folder |

## Uninstall

Quit Painter first, since the code being removed is loaded. On Windows, use **Settings → Apps → Miris for Substance 3D
Painter → Uninstall** for an EXE installation. On macOS, or after a manual ZIP
install, remove the `miris` folder from Painter's plugins folder. Remove
`~/.miris/substance-painter/` separately to clear your sign-in and preferences.

Signing out in the window removes the plugin's copy of your Miris tokens. To
revoke the grant itself, remove it in Miris under your account's connected
applications.

## Support

Found a bug or need help? Email [support@miris.com](mailto:support@miris.com).
Include the Painter and plugin versions shown in **Miris → About Miris…**, and
what you were doing when it went wrong.
