"""Helpers to run a worker QObject on its own QThread.

A worker exposes ``start()`` and ``stop()`` slots. ``start()`` runs in the
worker thread once the thread starts (so timers are created there); ``stop()``
is invoked blocking from the caller's thread before the thread quits.
"""

from __future__ import annotations

from PyQt5.QtCore import QMetaObject, QObject, Qt, QThread


def start_worker(worker: QObject, name: str = "") -> QThread:
    """Move ``worker`` to a new QThread, start it and call ``worker.start()`` there."""
    thread = QThread()
    if name:
        thread.setObjectName(name)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()
    return thread


def stop_worker(worker: QObject, thread: QThread, timeout_ms: int = 5000) -> bool:
    """Call ``worker.stop()`` in its thread (blocking), then quit and join the thread.

    Returns True if the thread finished within ``timeout_ms``.
    """
    if thread.isRunning():
        QMetaObject.invokeMethod(worker, "stop", Qt.BlockingQueuedConnection)
        thread.quit()
        return thread.wait(timeout_ms)
    return True
