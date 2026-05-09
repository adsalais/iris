"""Multi-process test: process A creates a session, process B reads it.

Proves SessionStore can back multiple uvicorn workers that share one DB file.
Skipped on platforms where ``fork`` start method isn't available (i.e. Windows).
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import sys
from pathlib import Path

import pytest

from iris.auth.identity import User
from iris.auth.store import SessionStore


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocessing.fork is not available on Windows",
)


def _writer(db_path: str, queue) -> None:
    store = SessionStore(path=db_path, ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        user = User(
            subject="cross_proc_user",
            username="cross_proc_user",
            display_name="Cross",
            groups=("g1", "g2"),
        )
        session = asyncio.run(store.create(user))
        asyncio.run(store.update_data(session.id, {"shared": "yes"}))
        queue.put(session.id)
    finally:
        asyncio.run(store.close())


def _reader(db_path: str, session_id: str, queue) -> None:
    store = SessionStore(path=db_path, ttl_seconds=60, absolute_ttl_seconds=3600)
    try:
        session = asyncio.run(store.get_and_refresh(session_id))
        if session is None:
            queue.put(None)
        else:
            queue.put(
                {
                    "subject": session.user.subject,
                    "groups": list(session.user.groups),
                    "data": session.data,
                }
            )
    finally:
        asyncio.run(store.close())


def test_session_visible_across_processes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "shared.db")
    ctx = mp.get_context("fork")
    q = ctx.Queue()

    writer = ctx.Process(target=_writer, args=(db_path, q))
    writer.start()
    writer.join(timeout=10)
    assert writer.exitcode == 0, "writer process failed"
    session_id = q.get(timeout=5)
    assert isinstance(session_id, str)

    reader = ctx.Process(target=_reader, args=(db_path, session_id, q))
    reader.start()
    reader.join(timeout=10)
    assert reader.exitcode == 0, "reader process failed"
    result = q.get(timeout=5)

    assert result is not None, "reader saw no session"
    assert result["subject"] == "cross_proc_user"
    assert result["groups"] == ["g1", "g2"]
    assert result["data"] == {"shared": "yes"}
