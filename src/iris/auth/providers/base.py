from __future__ import annotations

from typing import Protocol

from fastapi import Request, Response


class Provider(Protocol):
    async def begin(self, request: Request) -> Response: ...
