from __future__ import annotations

from typing import Protocol

from fastapi import Request, Response

from iris.auth.identity import User


class Provider(Protocol):
    async def begin(self, request: Request) -> Response: ...
    async def complete(self, request: Request) -> User: ...
