"""Miris plugin entry point. Reload purges every miris submodule, so an edit
anywhere in the package is picked up."""

_menu = None
_controller = None
_about = None


def _purge_submodules():
    """Painter only reloads this top module, so stale submodules mix old and new
    code. Package attributes go too: `from . import x` reads the attribute before
    sys.modules, so a stale one wins over the purged entry."""
    import sys

    for name in [n for n in sys.modules if n.startswith(__name__ + ".")]:
        del sys.modules[name]
    pkg = sys.modules[__name__]
    for attr, value in list(vars(pkg).items()):
        if getattr(value, "__name__", "").startswith(__name__ + "."):
            delattr(pkg, attr)


def start_plugin():
    global _menu, _controller
    _purge_submodules()
    import substance_painter.ui
    from PySide6 import QtGui, QtWidgets

    from .ui.window import PublishWindowController

    _log_startup()
    _menu = QtWidgets.QMenu("Miris")
    _menu.addAction("Publish…", _publish)
    _menu.addSeparator()
    about = _menu.addAction("About Miris…", _show_about)
    # On macOS Qt moves any action starting with "About" into the app menu.
    about.setMenuRole(QtGui.QAction.MenuRole.NoRole)
    _menu.addAction("Reload", _schedule_reload)
    substance_painter.ui.add_menu(_menu)

    _controller = PublishWindowController()
    _controller.start()


def _publish():
    if _controller is None:
        _tell("Miris isn't ready. Try reloading the plugin from this menu.")
        return
    if not _project_is_open():
        _tell("Open a project in Painter first, then publish it.")
        return
    try:
        _controller.present_now()
    except Exception:  # noqa: BLE001 (a menu action must not fail silently)
        _log_error("could not open the publish window")
        _tell("Couldn't open the publish window. See Painter's log for details.")


def _show_about():
    global _about
    try:
        if _about is None:
            import substance_painter.ui

            from .ui.about import AboutDialog

            _about = AboutDialog(substance_painter.ui.get_main_window())
        _about.open()
    except Exception:  # noqa: BLE001 (a menu action must not fail silently)
        _log_error("could not open the About window")
        _tell("Couldn't open the About window. See Painter's log for details.")


def _project_is_open() -> bool:
    try:
        import substance_painter.project

        return substance_painter.project.is_open()
    except Exception:  # noqa: BLE001 (treat an unusable API as "no project")
        return False


def _tell(message):
    """A menu action that only logs looks identical to one that is broken."""
    try:
        import substance_painter.ui
        from PySide6 import QtWidgets

        QtWidgets.QMessageBox.information(substance_painter.ui.get_main_window(), "Miris", message)
    except Exception:  # noqa: BLE001 (fall back to the log outside Painter)
        _log_info(message)


def close_plugin():
    """Each piece tears down independently. A raise part-way used to leave the
    menu behind for the next start to duplicate."""
    global _menu, _controller, _about
    import substance_painter.ui

    if _about is not None:
        about, _about = _about, None
        _guarded(about.close, "about window teardown failed")
    if _controller is not None:
        controller, _controller = _controller, None
        _guarded(controller.stop, "controller teardown failed")
    if _menu is not None:
        menu, _menu = _menu, None
        _guarded(lambda: substance_painter.ui.delete_ui_element(menu), "menu teardown failed")


def _guarded(action, context):
    try:
        action()
    except Exception:  # noqa: BLE001 (one failed step must not strand the rest)
        _log_error(context)


def _log_info(message):
    try:
        import substance_painter.logging as sp_log

        sp_log.info(f"[miris] {message}")
    except ImportError:
        print(f"[miris] {message}")


def _log_error(context):
    import traceback

    try:
        import substance_painter.logging as sp_log

        sp_log.error(f"[miris] {context}:\n{traceback.format_exc()}")
    except ImportError:
        traceback.print_exc()


def _log_startup():
    """Log the code's mtime and the Miris host, so a stale reload or the wrong
    environment shows up in the log."""
    try:
        import os
        import time

        import substance_painter.logging as sp_log

        from .config import HOST_ENV, resolve_host
        from .destinations import by_id

        window_py = os.path.join(os.path.dirname(__file__), "ui", "window.py")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(window_py)))
        # Resolved the same way the Miris components do, so this is the host they use.
        catalog_host = ((by_id("miris") or {}).get("auth") or {}).get("host", "")
        source = "from " + HOST_ENV if os.environ.get(HOST_ENV) else "from the catalog"
        sp_log.info(
            f"[miris] plugin started: code last modified {stamp}; "
            f"Miris host {resolve_host(catalog_host)} ({source})"
        )
    except Exception:  # noqa: BLE001 (never block startup for a log line)
        pass


def _schedule_reload():
    # Deferred so the triggering QAction's handler fully returns before
    # close_plugin deletes the menu that owns it.
    from PySide6 import QtCore

    QtCore.QTimer.singleShot(0, _reload_now)


def _reload_now():
    import importlib
    import sys

    import substance_painter_plugins

    module = sys.modules[__name__]
    try:
        substance_painter_plugins.close_plugin(module)
    except Exception:  # noqa: BLE001 (match Painter's fault tolerance on close)
        pass
    # An exception escaping a QTimer callback goes to stderr, not Painter's log.
    try:
        module = importlib.reload(module)
        substance_painter_plugins.start_plugin(module)
    except Exception:  # noqa: BLE001 (surface reload failures in the log)
        import traceback

        try:
            import substance_painter.logging as sp_log

            sp_log.error(f"[miris] reload failed:\n{traceback.format_exc()}")
        except ImportError:
            traceback.print_exc()
