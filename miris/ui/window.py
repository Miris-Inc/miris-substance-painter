"""The publish window and its QWebChannel bridge.

What a destination needs comes from the catalog; nothing here names one.
"""

from __future__ import annotations

import functools
import json
import secrets
import tempfile
import time
import webbrowser
from pathlib import Path

from PySide6 import (
    QtCore,
    QtGui,
    QtWebChannel,
    QtWebEngineCore,
    QtWebEngineWidgets,
    QtWidgets,
)

from .. import config, connectors, destinations, exporter, hdri, localstate, usdz
from ..api import AuthError
from . import branding
from .connection import ConnectionManager
from .workers import FnWorker, orphanage

_WEB_DIR = Path(__file__).parent / "web"
_ACK_WATCHDOG_MS = 3000
_HANDSHAKE_PROBE_MS = 4000
_WINDOW_WIDTH = 420
_MIN_WINDOW_HEIGHT = 150
# Resizing can nudge the measurement back a pixel or two and ping-pong forever.
_HEIGHT_EPSILON = 6


def log(message, *, error=False):
    try:
        import substance_painter.logging as sp_log

        (sp_log.error if error else sp_log.info)(f"[miris] publish window: {message}")
    except ImportError:
        print(f"[miris] publish window: {message}")


def log_exception(context):
    import traceback

    log(f"{context}:\n{traceback.format_exc()}", error=True)


def _reveal(directory: Path) -> None:
    try:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory)))
    except Exception:  # noqa: BLE001 (a file browser that won't open is not a failed upload)
        log_exception("could not open the export folder")


def guard(method):
    """Log exceptions from a Qt entry point, which Qt would otherwise swallow."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:  # noqa: BLE001 (the alternative is silence)
            log_exception(f"{method.__name__} failed")
            return None

    return wrapper


class _Bridge(QtCore.QObject):
    stateChanged = QtCore.Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self._w = window

    @QtCore.Slot()
    @guard
    def ready(self):
        self._w._handshake_done = True
        self._w._push()

    @QtCore.Slot(str, str, str, str, str)
    @guard
    def publish(self, asset_name, tags_json, destination, options_json, unit_id):
        self._w._start_publish(asset_name, tags_json, destination, options_json, unit_id)

    @QtCore.Slot(str)
    @guard
    def signIn(self, destination):
        self._w._connections.sign_in(destination)

    @QtCore.Slot(str)
    @guard
    def signOut(self, destination):
        self._w._connections.sign_out(destination)

    @QtCore.Slot(str)
    @guard
    def selectDestination(self, destination):
        # The window can sit open a long time; a grant revoked meanwhile should
        # read as signed out before an export, not after.
        self._w._connections.verify(destination)

    @QtCore.Slot(str, str)
    @guard
    def setWorkspace(self, destination, value):
        self._w._connections.set_workspace(destination, value)

    @QtCore.Slot()
    @guard
    def dismissNotice(self):
        # Cleared here, not hidden in the page: a hidden message came back on the
        # next change.
        self._w._connections.clear_message()
        self._w._set(formMessage="", formTone="note")
        self._w._push_auth()

    @QtCore.Slot()
    @guard
    def cancelPublish(self):
        self._w._cancel_requested = True
        self._w._cancel_pending_export()

    @QtCore.Slot(result=str)
    @guard
    def catalog(self):
        # Fetched once, not pushed with the state: the logos are inline SVG and
        # the state is re-sent on every progress tick. signs_in is added so the
        # page never needs a mechanism name.
        return json.dumps(
            {
                "integrations": [
                    {**entry, "signs_in": destinations.signs_in(entry)}
                    for entry in destinations.load()
                ]
            }
        )

    @QtCore.Slot(result=str)
    @guard
    def settings(self):
        return json.dumps(
            {
                "keepExports": localstate.keep_exports(),
                "revealExports": localstate.reveal_exports(),
                "exportRoot": str(localstate.export_root()),
                "defaultRoot": str(localstate.painter_export_dir()),
            }
        )

    @QtCore.Slot(bool)
    @guard
    def setKeepExports(self, on):
        localstate.save_pref("keep_exports", bool(on))

    @QtCore.Slot(bool)
    @guard
    def setRevealExports(self, on):
        localstate.save_pref("reveal_exports", bool(on))

    @QtCore.Slot(result=str)
    @guard
    def chooseExportFolder(self):
        """A cancel returns the current folder, not the default."""
        current = localstate.export_root()
        start = current if current.is_dir() else current.parent
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self._w, "Choose where to keep exported files", str(start)
        )
        if chosen:
            localstate.set_export_root(chosen)
        return str(localstate.export_root())

    @QtCore.Slot(result=str)
    @guard
    def chooseEnvironment(self):
        """An EXR from disk joins the lighting choices; the page selects the id returned."""
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self._w, "Choose an HDRI", str(Path.home()), "EXR images (*.exr)"
        )
        if not chosen:
            return json.dumps({"id": "", "choices": self._w._environment_choices()})
        try:
            env = hdri.from_file(Path(chosen))
        except Exception as e:  # noqa: BLE001 (the page shows why)
            return json.dumps({"error": str(e), "choices": self._w._environment_choices()})
        localstate.remember_environment(str(env.path))
        self._w._environments = None
        return json.dumps({"id": env.id, "choices": self._w._environment_choices()})

    @QtCore.Slot(str, result=str)
    @guard
    def forgetEnvironment(self, env_id):
        """Drops a file the user chose from the lighting choices; Painter's own stay."""
        for env in self._w._environment_list():
            if env.id == env_id and env.group == hdri.YOUR_FILES:
                localstate.forget_environment(str(env.path))
        self._w._environments = None
        return json.dumps(self._w._environment_choices())

    @QtCore.Slot(result=str)
    @guard
    def resetExportFolder(self):
        localstate.set_export_root(None)
        return str(localstate.export_root())

    @QtCore.Slot(int)
    @guard
    def contentHeight(self, pixels):
        # A fixed height cropped a tall page or left dead space under a short one.
        self._w._fit_height(pixels)

    @QtCore.Slot(result=str)
    @guard
    def environments(self):
        """The lighting choices, Miris default first, each with Painter's preview if it has one."""
        return json.dumps(self._w._environment_choices())

    @QtCore.Slot(str)
    @guard
    def pageShown(self, page):
        # The export blocks Painter's main thread; it waits for this.
        self._w._on_page_shown(page)

    @QtCore.Slot()
    @guard
    def closeWindow(self):
        self._w.close()

    @QtCore.Slot(str)
    @guard
    def copyLink(self, text):
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is None:  # the page has already shown a "copied" tick
            raise RuntimeError("no clipboard available")
        clipboard.setText(text)

    @QtCore.Slot(str)
    @guard
    def openExternal(self, url):
        if not url.startswith(("http://", "https://")):
            log(f"refusing to open {url!r}", error=True)
            return
        webbrowser.open(url)


class PublishWindow(QtWidgets.QDialog):
    """A QDialog for its modality; it is the window the whole upload happens in."""

    _stage_changed = QtCore.Signal(str)
    # Texture archives can exceed Qt's 32-bit int range.
    _bytes_changed = QtCore.Signal("qlonglong", "qlonglong")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Miris")
        self.setWindowIcon(branding.mark_icon())
        # Application-modal like Adobe's AlgDialog. It is hidden while Painter
        # exports, or it would cover Painter's progress dialog and its Cancel.
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        # The controller reuses this instance.
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        # Height follows the content; width is fixed so a resize cannot reflow
        # the page and trigger another measurement.
        self.resize(_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT)

        self._workers = []
        self._shutting_down = False
        self._handshake_done = False
        self._cancel_requested = False
        self._export_dir = None
        self._login_window = None
        self._destination = destinations.default_destination()
        self._pending_upload = None
        self._publish_options: dict = {}
        self._meters_per_unit: float | None = None
        self._pending_export = None
        self._publish_generation = 0
        self._uploaded_count = 0
        self._variant_total = 0
        self._environments: list[hdri.Environment] | None = None

        # The page clears the form only when this changes. Resetting on entering
        # the form page missed a second export and wiped a retry after a failed
        # upload.
        self._form_nonce = 0

        self._connections = ConnectionManager(connectors.build(), self._spawn, log, parent=self)
        self._connections.show_login.connect(self._show_login)
        self._connections.hide_login.connect(self._close_login)
        self._connections.changed.connect(lambda _destination: self._push_auth())

        self._state = {
            "page": "form",
            "assetName": "",
            "formNonce": 0,
            "formMessage": "",
            "keepExports": False,
            "unitChoices": [],
            "unitId": "cm",
            "formTone": "note",
            "stage": "",
            "progressPct": 0,
            "bytesLine": "",
            "shareUrl": "",
            "doneMessage": "",
            "auth": {},
        }

        self.bridge = _Bridge(self)
        self.view = QtWebEngineWidgets.QWebEngineView(self)
        self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        self.view.page().setBackgroundColor(QtGui.QColor("#1c1c1f"))
        self.view.settings().setAttribute(
            QtWebEngineCore.QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            True,
        )
        self.channel = QtWebChannel.QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        # A blank modal reads as "Painter has hung". Log both silent causes: a
        # dead renderer, or JS that never reaches bridge.ready().
        self.view.loadFinished.connect(self._on_web_load_finished)
        self.view.page().renderProcessTerminated.connect(
            lambda status, code: log(
                f"web renderer terminated (status={status}, code={code})", error=True
            )
        )
        self.view.load(QtCore.QUrl.fromLocalFile(str(_WEB_DIR / "dialog.html")))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self._stage_changed.connect(lambda text: self._set(stage=text))
        self._bytes_changed.connect(self._on_bytes)

    def _fit_height(self, content_px):
        if content_px <= 0:
            return
        chrome = self.frameGeometry().height() - self.view.height()
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        ceiling = int(screen.availableGeometry().height() * 0.9) if screen else 1400
        target = max(_MIN_WINDOW_HEIGHT, min(content_px + max(chrome, 0), ceiling))
        if abs(target - self.height()) <= _HEIGHT_EPSILON:
            return
        self.resize(self.width(), target)
        self._center_on_parent()

    def _center_on_parent(self):
        reference = self.parentWidget()
        area = (
            reference.frameGeometry()
            if reference is not None
            else self.screen().availableGeometry()
        )
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())

    @guard
    def _on_web_load_finished(self, ok):
        if not ok:
            log("web view failed to load dialog.html", error=True)
            return
        QtCore.QTimer.singleShot(_HANDSHAKE_PROBE_MS, self._check_handshake)

    @guard
    def _check_handshake(self):
        if self._handshake_done:
            return
        log(
            "page loaded but JS never called bridge.ready(); the window will be "
            "blank; check dialog.js/dropdown.js for a load error",
            error=True,
        )

    def present(self, asset_name: str):
        if self._state["page"] == "publishing":
            # Resetting would strand the running upload and hide its progress.
            self.show()
            return
        # Retires any export watchdog left from an earlier attempt.
        self._publish_generation += 1
        self._environments = None  # a shelf may have gained an HDRI since
        self._cancel_requested = False
        self._connections.clear_message()
        self._connections.verify_all()
        self._form_nonce += 1
        # Shown before the upload, not after: once the asset exists the
        # textures are already on geometry they may not fit.
        raised = (exporter.source_mesh_warning(), exporter.displacement_warning())
        cautions = [c for c in raised if c]
        message, tone = " ".join(cautions), "note"
        interrupted = localstate.pending_publish()
        if interrupted is not None:
            # Painter quit or the plugin reloaded mid-upload, so nothing was shown.
            localstate.set_pending_publish(None)
            log(f"a previous publish did not finish: {interrupted}", error=True)
            message = (
                f"Your last publish, {interrupted.get('asset') or 'the asset'} to "
                f"{interrupted.get('destination') or 'the destination'} at "
                f"{interrupted.get('when') or 'an earlier time'}, was interrupted before "
                "it finished. It did not arrive; publish it again."
            )
            tone = "error"
        self._set(
            page="form",
            keepExports=localstate.keep_exports(),
            unitChoices=exporter.unit_choices(),
            unitId="cm",
            assetName=asset_name,
            formNonce=self._form_nonce,
            formMessage=message,
            formTone=tone,
            shareUrl="",
        )
        self._push_auth()
        self._center_on_parent()
        # Flags and modality are set once, in __init__: changing them on a
        # visible window recreates the native window on macOS and drops focus.
        self.show()

    def _set(self, **fields):
        self._state.update(fields)
        self._push()

    def _push(self):
        self.bridge.stateChanged.emit(json.dumps(self._state))

    def _push_auth(self):
        self._set(auth=self._connections.snapshot())

    def _report_stage(self, text):
        if not self._shutting_down:
            self._stage_changed.emit(text)

    def _report_bytes(self, sent, total):
        if not self._shutting_down:
            self._bytes_changed.emit(sent, total)

    def _on_bytes(self, sent, total):
        pct = int(sent * 100 / total) if total else 0
        self._set(
            progressPct=pct,
            bytesLine=f"{branding.format_bytes(sent)} of {branding.format_bytes(total)}",
        )

    @guard
    def _show_login(self, destination, url, code):
        entry = destinations.by_id(destination) or {}
        title = entry.get("title") or destination
        from .login_window import LoginWindow

        if self._login_window is None:
            self._login_window = LoginWindow(self, title=f"Sign in to {title}")
            self._login_window.cancelled.connect(self._login_cancelled)
        else:
            self._login_window.setWindowTitle(f"Sign in to {title}")
        self._login_window.setProperty("destination", destination)
        if code:
            log(f"{title} sign-in code: {code}")
        self._login_window.present(url)

    @guard
    def _login_cancelled(self):
        destination = self._login_window.property("destination") if self._login_window else ""
        if destination:
            self._connections.cancel(destination)

    def _close_login(self):
        if self._login_window is not None:
            self._login_window.finish()

    def _start_publish(self, asset_name, tags_json, destination, options_json="{}", unit_id=""):
        if self._state["page"] == "publishing":
            return
        try:
            options = json.loads(options_json or "{}")
        except ValueError:
            options = {}
        if not isinstance(options, dict):
            options = {}
        # Read in _start_upload, after the export.
        self._publish_options = options
        self._meters_per_unit = exporter.meters_per_unit_for(unit_id)
        self._cancel_requested = False
        self._destination = destinations.parse_destination(destination)

        if not asset_name:
            self._set(formMessage="Give the asset a name first.", formTone="note")
            return
        blocked = self._not_ready(self._destination)
        if blocked:
            self._set(formMessage=blocked, formTone="note")
            return
        try:
            extra_tags = [t for t in json.loads(tags_json or "[]") if isinstance(t, str)]
        except json.JSONDecodeError:
            extra_tags = []

        chosen = options.get("environments")
        if not isinstance(chosen, list) or not chosen:
            chosen = [hdri.DEFAULT_ID]
        # Refused here, on the form, rather than after a blocking export.
        try:
            variants = hdri.resolve([str(c) for c in chosen], self._environment_list())
        except Exception as e:  # noqa: BLE001 (every failure must reach the form)
            log_exception("environments missing")
            self._set(formMessage=f"Cannot publish with that lighting: {e}", formTone="error")
            return
        if len(variants) > 1 and not options.get("skip_preview"):
            # A set that is not streamable would never show in the player link.
            self._set(
                formMessage="Turn on Skip preview to publish more than one lighting.",
                formTone="note",
            )
            return
        if variants == [(hdri.DEFAULT_LABEL, None)]:
            variants = []
        self._uploaded_count = 0
        self._variant_total = len(variants)

        # The USD export runs on Painter's main thread (its API is not thread
        # safe) and blocks it for seconds, holding back anything pushed to the
        # page. Show the progress page first; pageShown() resumes once it painted.
        self._publish_generation += 1
        generation = self._publish_generation
        self._pending_export = (asset_name, extra_tags, variants)
        self._set(page="publishing", stage="Exporting…", progressPct=0, bytesLine="")
        # In case the ack never arrives. The generation stops a watchdog left
        # from a failed attempt starting the next one before its page painted.
        QtCore.QTimer.singleShot(_ACK_WATCHDOG_MS, lambda: self._run_pending_export(generation))

    def _environment_list(self) -> list[hdri.Environment]:
        if self._environments is None:
            chosen = []
            for file in localstate.chosen_environments():
                try:
                    chosen.append(hdri.from_file(Path(file)))
                except Exception:  # noqa: BLE001 (moved, renamed or deleted since)
                    localstate.forget_environment(file)
            self._environments = chosen + hdri.find(hdri.shelves())
        return self._environments

    def _environment_choices(self) -> list[dict]:
        cache = config.state_dir() / "previews"
        choices = [{"id": hdri.DEFAULT_ID, "label": hdri.DEFAULT_LABEL, "group": "", "preview": ""}]
        for env in self._environment_list():
            image = hdri.cached_preview(env, cache)
            choices.append(
                {
                    "id": env.id,
                    "label": env.label,
                    "group": env.group,
                    "preview": QtCore.QUrl.fromLocalFile(str(image)).toString() if image else "",
                }
            )
        return choices

    def _not_ready(self, destination: str) -> str:
        """Why this destination cannot be published to yet, or an empty string."""
        entry = destinations.by_id(destination) or {}
        title = entry.get("title") or destination
        if destination in self._connections:
            if not self._connections.signed_in(destination):
                return f"Sign in to {title} first."
        return ""

    def _on_page_shown(self, page):
        if page == "publishing":
            self._run_pending_export()

    @guard
    def _run_pending_export(self, generation=None):
        if generation is not None and generation != self._publish_generation:
            return  # a watchdog armed for an attempt that is already over
        pending, self._pending_export = self._pending_export, None
        if pending is not None:
            self._export_and_upload(*pending)

    def _export_and_upload(self, asset_name, extra_tags, variants):
        # Runs behind a committed "publishing" page that present() won't reset,
        # so any escape wedges the window for the session. Failures go to the form.
        try:
            # Kept exports go to Documents: the OS clears $TMPDIR on its own schedule.
            if localstate.keep_exports():
                self._export_dir = localstate.export_folder(
                    asset_name, time.strftime("%Y-%m-%d %H%M")
                )
            else:
                self._export_dir = Path(tempfile.mkdtemp(prefix="miris-upload-"))
            self.hide()
            try:
                geometry = exporter.export_uploadable_asset(self._export_dir, note=log)
            finally:
                self.show()
                self.raise_()
                self.activateWindow()
            # A substitute geometry leaves the stage referencing the original
            # source by its imported name, which the packer can't derive. Capture
            # it while the project can still be asked.
            source = exporter.source_mesh_path()
            stands_in_for = (
                source.name
                if source is not None and geometry is not None and source.name != geometry.name
                else None
            )
            self._pending_upload = (
                # geometry may be a layer this plugin wrote, never the scene.
                usdz.find_root_layer(self._export_dir, exclude=[geometry] if geometry else []),
                geometry,
                stands_in_for,
                asset_name,
                extra_tags,
                variants,
            )
        except exporter.ExportCancelled:
            log("export cancelled in Painter's progress dialog")
            self._discard_export()
            self._set(page="form", formMessage="Publish cancelled.", formTone="note")
            return
        except Exception as e:  # noqa: BLE001 (every failure must reach the window)
            log_exception("export failed")
            self._cleanup_export()
            self._set(page="form", formMessage=f"Export failed: {e}", formTone="error")
            return

        self._start_upload()

    def _pack(self, pending, variant=None):
        """Runs on a worker. `variant` is (label, hdri) for a lit copy; the label
        keeps the archives apart."""
        root, geometry, stands_in_for, _name, _tags, _variants = pending
        label, dome_texture = variant if variant else (None, None)
        suffix = f"-{usdz.safe_member_name(label)}" if label else ""
        archive = root.parent / f"{root.stem}{suffix}.usdz"
        usdz.write(
            archive,
            usdz.package(
                root,
                source_mesh=geometry,
                stands_in_for=stands_in_for,
                meters_per_unit=self._meters_per_unit,
                dome_texture=dome_texture,
                note=log,
            ),
        )
        if self._cancel_requested:
            raise connectors.Cancelled("upload cancelled by the user")
        return archive

    def _start_upload(self):
        pending, self._pending_upload = self._pending_upload, None
        connector = self._connections.get(self._destination)
        if pending is None or connector is None:
            self._cleanup_export()
            self._set(page="form", formMessage="There was nothing to publish.", formTone="note")
            return
        _root, _geometry, _stands_in_for, asset_name, extra_tags, variants = pending
        entry = destinations.by_id(self._destination) or {}
        title = entry.get("title") or self._destination
        self._report_stage(f"Packing for {title}…")

        def checkpoint(description):
            # Persisted so the next open can report a publish cut off by a quit.
            if description is None:
                localstate.set_pending_publish(None)
            else:
                localstate.set_pending_publish(
                    {
                        "asset": asset_name,
                        "destination": title,
                        "what": description,
                        "when": time.strftime("%Y-%m-%d %H:%M"),
                    }
                )

        def upload_one(archive, name, tags, stage_prefix=""):
            return connector.upload(
                connectors.UploadRequest(
                    archive=archive,
                    asset_name=name,
                    tags=tags,
                    report_stage=lambda text: self._report_stage(stage_prefix + text),
                    report_bytes=self._report_bytes,
                    should_continue=lambda: not self._cancel_requested,
                    options=self._publish_options,
                    log=log,
                    checkpoint=checkpoint,
                )
            )

        def work():
            if not variants:
                return upload_one(self._pack(pending), asset_name, extra_tags)
            if len(variants) == 1:
                return upload_one(self._pack(pending, variants[0]), asset_name, extra_tags)
            # One set tag ties the copies together; the player key is scoped to it.
            set_tag = f"sp-{secrets.token_hex(6)}"
            results = []
            for index, variant in enumerate(variants, start=1):
                prefix = f"{index} of {len(variants)}: "
                self._report_stage(f"{prefix}Packing for {title}…")
                archive = self._pack(pending, variant)
                results.append(
                    upload_one(
                        archive, f"{asset_name} ({variant[0]})", [*extra_tags, set_tag], prefix
                    )
                )
                self._uploaded_count = index
            self._report_stage("Creating the player link…")
            try:
                link = connector.share_set(set_tag, asset_name)
                warning = ""
            except Exception as e:  # noqa: BLE001 (the assets are already there)
                log(f"player link for {set_tag} failed: {type(e).__name__}: {e}", error=True)
                link = results[0].share_url
                warning = (
                    f"Published {len(results)} variants, but the player link could not be "
                    f"created. Sign out of {title} and back in, then publish again to get one."
                )
            return connectors.Uploaded(
                f"Published {len(results)} variants to {title}.", share_url=link, warning=warning
            )

        self._spawn(work, self._finish_upload, self._on_upload_failed)

    def _finish_upload(self, result):
        self._cleanup_export()
        log(f"uploaded to {self._destination}: {result.share_url or 'no link'}")
        if not self.isVisible():
            log("the publish finished while the window was closed")
        # A warning still means the file arrived.
        self._set(
            page="done",
            stage="",
            shareUrl=result.share_url,
            doneMessage=result.warning or result.message,
        )

    def _on_upload_failed(self, exc):
        self._cleanup_export()
        landed = self._uploaded_count
        total = self._variant_total
        after = f" after {landed} of {total} variants uploaded" if landed else ""
        if isinstance(exc, connectors.Cancelled):
            self._set(page="form", formMessage=f"Publish cancelled{after}.", formTone="note")
            return
        connector = self._connections.get(self._destination)
        # A revoked credential must send the user back to sign-in rather than
        # look like a transient upload failure.
        if connector is not None and isinstance(exc, AuthError):
            connector.sign_out()
            self._push_auth()
        log(f"{self._destination} upload failed: {exc}", error=True)
        detail = connector.upload_error(exc) if connector is not None else str(exc)
        self._set(page="form", formMessage=f"Publish failed{after}. {detail}", formTone="error")

    def _discard_export(self):
        """A cancelled export is half written, so even a kept one is not worth keeping."""
        import shutil

        discarded, self._export_dir = self._export_dir, None
        if discarded:
            shutil.rmtree(discarded, ignore_errors=True)

    def _cleanup_export(self):
        import shutil

        if not self._export_dir:
            return
        kept, self._export_dir = self._export_dir, None
        if localstate.keep_exports():
            # Failed exports are revealed too; they are the ones worth looking at.
            log(f"kept export files: {kept}")
            if localstate.reveal_exports():
                _reveal(kept)
            return
        shutil.rmtree(kept, ignore_errors=True)

    def _spawn(self, fn, on_done, on_failed):
        """Run fn on a worker and deliver its result on the UI thread."""
        worker = FnWorker(fn)

        def deliver(handler, result):
            if self._shutting_down:
                # A reload mid-upload used to leave no trace either way.
                if handler is on_failed:
                    log(f"finished after the window shut down: failed: {result}", error=True)
                else:
                    log("finished after the window shut down")
                return
            handler(result)

        def finished():
            # Plain callbacks survive deletion of the dialog's C++ object. Bound
            # QObject slots would disconnect at deletion and strand both the
            # temporary export and the worker in the orphanage.
            self._workers.remove(worker)
            if worker in orphanage():
                orphanage().remove(worker)
            if self._shutting_down and not self._workers:
                self._cleanup_export()
            worker.deleteLater()

        worker.done.connect(lambda result: deliver(on_done, result))
        worker.failed.connect(lambda exc: deliver(on_failed, exc))
        worker.finished.connect(finished)
        self._workers.append(worker)
        worker.start()

    def _cancel_pending_export(self):
        # Otherwise the watchdog runs the export and upload seconds later with
        # nothing on screen, creating an asset the user thinks they cancelled.
        if self._pending_export is not None:
            log("export cancelled before it started")
            self._pending_export = None
            self._set(page="form", formMessage="Publish cancelled.", formTone="note")

    def _before_close(self):
        self._cancel_pending_export()
        # An upload in flight survives the close: it reads from the export
        # directory and its completion handler owns cleanup.
        if self._state["page"] != "publishing":
            self._cancel_requested = True
            self._cleanup_export()

    def reject(self):
        # Escape calls reject directly, bypassing QDialog.closeEvent.
        self._before_close()
        super().reject()

    def closeEvent(self, event):
        self._before_close()
        super().closeEvent(event)

    def shutdown(self):
        self._shutting_down = True
        self._connections.retire()
        if self._login_window is not None:
            login, self._login_window = self._login_window, None
            login.shutdown()
            login.deleteLater()  # else its renderer process outlives the reload
        self._cancel_requested = True
        if self._state["page"] != "publishing":
            self._cleanup_export()
        for worker in self._workers:
            # Never wait on network I/O from Painter's GUI thread. finished()
            # releases these and cleans exports once all work stops.
            if worker not in orphanage():
                orphanage().append(worker)
        self.close()


class PublishWindowController(QtCore.QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._window = None

    def start(self):
        pass  # nothing to install: the window opens on demand

    def stop(self):
        if self._window is not None:
            window, self._window = self._window, None
            window.shutdown()
            # Parented to Painter and never WA_DeleteOnClose, so dropping the
            # Python reference leaves the widget and its renderer process alive.
            window.deleteLater()

    def present_now(self, asset_name=None):
        if self._window is None:
            self._window = PublishWindow(_painter_main_window())
        self._window.present(asset_name or _safe_default_name())


def _safe_default_name():
    try:
        return exporter.default_asset_name()
    except Exception:  # noqa: BLE001 (naming must never block the window)
        return "Untitled"


def _painter_main_window():
    """Parented to Painter so the window stays above the app, not the desktop."""
    try:
        import substance_painter.ui

        return substance_painter.ui.get_main_window()
    except Exception:  # noqa: BLE001 (an unparented window still works)
        return None
