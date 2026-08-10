"""Backend selection.

``DATABASE_URL`` set  -> Postgres (hosted; the filesystem is read-only there)
otherwise             -> local files (laptop; zero dependencies)

Nothing else in the app checks which one is active.
"""

from __future__ import annotations

import os

from .base import Backend
from .files import FileBackend

_backend: Backend | None = None


def get_backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = _build()
    return _backend


def _build() -> Backend:
    if os.environ.get("DATABASE_URL"):
        from .postgres import PostgresBackend

        return PostgresBackend()
    return FileBackend()


def set_backend(backend: Backend | None) -> None:
    """Override the backend. Used by tests and by ``--data-dir`` style setups."""
    global _backend
    _backend = backend


def reset() -> None:
    set_backend(None)


__all__ = ["Backend", "FileBackend", "get_backend", "reset", "set_backend"]
