from __future__ import annotations

import logging
import re
from typing import Callable

from fastapi import Request, Response
from ldap3 import Connection, Server
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
)
from ldap3.utils.conv import escape_filter_chars

from iris.auth.config import LDAPSettings
from iris.auth.exceptions import AuthError
from iris.auth.providers._form import render_login_form
from iris.auth.identity import User

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = logging.getLogger("iris.auth.ldap")


class LDAPProvider:
    def __init__(
        self,
        settings: LDAPSettings,
        *,
        _connection_factory: Callable[[], Connection] | None = None,
    ) -> None:
        self._settings = settings
        self._connection_factory = _connection_factory  # for tests

    async def begin(self, request: Request) -> Response:
        return render_login_form(
            request,
            {
                "invalid_credentials": "Invalid username or password.",
                "ldap_unreachable": "Authentication service unreachable. Please try again.",
                "ldap_groups": "Could not load your group membership. Please contact an admin.",
                "csrf_mismatch": "Session expired, please reload and try again.",
            },
        )

    async def complete(self, request: Request) -> User:
        raise NotImplementedError("LDAPProvider uses authenticate()")

    async def authenticate(self, username: str, password: str) -> User:
        if not _USERNAME_RE.fullmatch(username):
            raise AuthError("invalid_credentials")
        bind_dn = self._settings.bind_dn_template.format(username=username)
        try:
            conn = self._open_connection(bind_dn, password)
        except _BindFailed:
            raise AuthError("invalid_credentials")
        except _Unreachable:
            logger.exception("auth: LDAP unreachable")
            raise AuthError("ldap_unreachable")

        try:
            display_name = self._read_display_name(conn, bind_dn) or username
            groups = self._read_groups(conn, bind_dn)
        except Exception:
            logger.exception("auth: LDAP group/profile read failed")
            raise AuthError("ldap_groups")

        return User(subject=bind_dn, display_name=display_name, groups=tuple(groups))

    def _open_connection(self, bind_dn: str, password: str) -> Connection:
        if self._connection_factory is not None:
            conn = self._connection_factory()
            try:
                ok = conn.rebind(user=bind_dn, password=password)
            except LDAPInvalidCredentialsResult:
                raise _BindFailed()
            except (LDAPSocketOpenError, LDAPException):
                raise _Unreachable()
            if not ok:
                raise _BindFailed()
            return conn
        try:
            tls = None
            if self._settings.ca_cert_path:
                import ssl
                from ldap3 import Tls
                tls = Tls(
                    validate=ssl.CERT_REQUIRED,
                    ca_certs_file=self._settings.ca_cert_path,
                )
            server = Server(self._settings.url, get_info=None, tls=tls)
            conn = Connection(server, user=bind_dn, password=password, auto_bind=True)
            return conn
        except LDAPInvalidCredentialsResult as exc:
            raise _BindFailed() from exc
        except (LDAPSocketOpenError, LDAPException) as exc:
            raise _Unreachable() from exc
        except Exception as exc:
            raise _Unreachable() from exc

    def _read_display_name(self, conn: Connection, bind_dn: str) -> str | None:
        conn.search(bind_dn, "(objectClass=*)", attributes=["cn"])
        if conn.entries:
            cn = conn.entries[0].cn.value if "cn" in conn.entries[0] else None
            return str(cn) if cn else None
        return None

    def _read_groups(self, conn: Connection, bind_dn: str) -> list[str]:
        conn.search(
            self._settings.group_base_dn,
            f"(member={escape_filter_chars(bind_dn)})",
            attributes=["cn"],
        )
        groups: list[str] = []
        for entry in conn.entries:
            cn = entry.cn.value if "cn" in entry else None
            if cn:
                groups.append(str(cn))
        return groups


class _BindFailed(Exception): ...


class _Unreachable(Exception): ...
