from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuthzSettings:
    config_path: Path

    @classmethod
    def from_env(cls) -> "AuthzSettings":
        raw = os.environ.get("AUTHZ_CONFIG_PATH", "").strip()
        if not raw:
            raise ValueError("Missing required env var: AUTHZ_CONFIG_PATH")
        return cls(config_path=Path(raw))
