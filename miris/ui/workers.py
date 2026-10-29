"""Off-UI-thread work for the whole plugin.

Qt swallows exceptions in slots, and destroying a running QThread aborts
Painter. FnWorker sends results and failures back as signals, and a worker that
outlives teardown is parked in the orphanage instead of destroyed.
"""

from __future__ import annotations

import sys

from PySide6 import QtCore


def orphanage() -> list:
    """Keeps still-running workers referenced past teardown.

    Lives on sys, not in this module: reload drops miris.ui.workers from
    sys.modules, leaving a module-level list reachable only through a cycle
    (globals -> list -> worker -> lambda -> __globals__) that the collector may
    free, taking the running thread with it.
    """
    return sys.__dict__.setdefault("_miris_orphaned_workers", [])


class FnWorker(QtCore.QThread):
    """Runs a callable off the UI thread; emits done(result) or failed(exception)."""

    done = QtCore.Signal(object)
    failed = QtCore.Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except BaseException as e:  # noqa: BLE001 (marshal everything to the UI thread)
            self.failed.emit(e)
