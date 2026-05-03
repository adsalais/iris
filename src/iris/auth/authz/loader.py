from __future__ import annotations

import logging
import threading
from pathlib import Path

from iris.auth.authz.mapping import RoleMapping, RoleMappingError, parse

logger = logging.getLogger("iris.auth.authz.loader")


class RoleMappingLoader:
    """Loads a role mapping from disk, caching by mtime.

    On `get()`:
      1. stat the file. If mtime unchanged, return cached mapping.
      2. Otherwise, attempt to re-read and parse.
         - On success: cache and return the new mapping.
         - On failure: if a previously good mapping exists, log ERROR and
           return the cached one; otherwise re-raise (first-load failure
           must propagate so install() can fail loudly at boot).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cached: RoleMapping | None = None
        self._cached_mtime_ns: int | None = None

    def get(self) -> RoleMapping:
        # Fast path: no lock when nothing changed.
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return self._handle_load_failure(FileNotFoundError(f"file missing: {self._path}"))

        if self._cached is not None and mtime == self._cached_mtime_ns:
            return self._cached

        with self._lock:
            # Re-stat under lock in case another request just reloaded.
            try:
                mtime = self._path.stat().st_mtime_ns
            except FileNotFoundError:
                return self._handle_load_failure(
                    FileNotFoundError(f"file missing: {self._path}")
                )

            if self._cached is not None and mtime == self._cached_mtime_ns:
                return self._cached

            try:
                text = self._path.read_text()
                mapping = parse(text)
            except (FileNotFoundError, RoleMappingError, OSError) as exc:
                return self._handle_load_failure(exc)

            self._cached = mapping
            self._cached_mtime_ns = mtime
            return mapping

    def _handle_load_failure(self, exc: Exception) -> RoleMapping:
        if self._cached is None:
            raise exc
        logger.error(
            "authz: failed to reload role mapping from %s; keeping last good mapping. error=%s",
            self._path,
            exc,
        )
        return self._cached
