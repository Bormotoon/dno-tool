"""Доступ к файлам интерфейса: без входа браузер получает только страницу входа.

Тег script не может отправить заголовок Authorization, поэтому после входа сервер
выдаёт случайную HttpOnly cookie. В ней нет пароля, она действует только на статику
и значок-иконки, а API по-прежнему проверяет свои учётные данные.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from .auth import (
    _basic_credentials,
    _decode_token,
    authenticate_async,
    credential_fingerprint,
    fingerprint_valid,
)
from .vnc_access import verify_vnc_access

COOKIE_NAME = "dno-tool_assets"
SESSION_TTL_SEC = 24 * 3600
VIEWER_TTL_SEC = 900
MAX_SESSIONS = 1024
SCOPE_APP = "app"
SCOPE_VIEWER = "viewer"
LOGIN_PAGE = "login.html"
# Всё остальное закрыто: новый файл в static не станет публичным случайно.
PUBLIC_FILES = frozenset({LOGIN_PAGE, "login.js", "favicon.ico"})
VIEWER_PAGE = "vnc.html"
# Ссылка ноды (?vt=) открывает только вьювер, а не скрипты основного интерфейса.
VIEWER_FILES = frozenset(
    {VIEWER_PAGE, "vnc.js", "network-errors.js", "lib/novnc.bundle.min.js"}
)


@dataclass(frozen=True)
class _Session:
    scope: str
    expires: float
    fingerprint: str | None = None


class AssetSessions:
    """Сессии в памяти процесса: после перезапуска страница входа выдаёт новую."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _Session] = {}

    def _store(self, session: _Session) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            for key in [key for key, item in self._items.items() if item.expires <= now]:
                self._items.pop(key)
            while len(self._items) >= MAX_SESSIONS:
                # Словарь хранит порядок выдачи: вытесняем самую старую сессию.
                self._items.pop(next(iter(self._items)))
            self._items[token] = session
        return token

    def issue_app(self, username: str, password: str) -> str:
        """Вызывать только после успешной проверки этих учётных данных."""
        return self._store(
            _Session(
                SCOPE_APP,
                time.monotonic() + SESSION_TTL_SEC,
                credential_fingerprint(username, password),
            )
        )

    def issue_viewer(self) -> str:
        return self._store(_Session(SCOPE_VIEWER, time.monotonic() + VIEWER_TTL_SEC))

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._items.pop(token, None)

    def scope(self, token: str | None) -> str | None:
        """Область действия cookie или `None`; смена пароля отзывает сессию."""
        if not token:
            return None
        with self._lock:
            session = self._items.get(token)
        if session is None or session.expires <= time.monotonic():
            self.revoke(token)
            return None
        if session.fingerprint is not None and not fingerprint_valid(session.fingerprint):
            self.revoke(token)
            return None
        return session.scope


def set_asset_cookie(response: Response, request: Request, token: str, max_age: int) -> None:
    """Secure только по HTTPS: иначе браузер отбросит cookie на обычном HTTP."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def require_asset_session(sessions: AssetSessions):
    """Зависимость для служебных маршрутов интерфейса без заголовка Authorization."""

    async def dependency(request: Request) -> None:
        if sessions.scope(request.cookies.get(COOKIE_NAME)) != SCOPE_APP:
            raise HTTPException(401, "Требуется аутентификация")

    return dependency


class GatedStaticFiles(StaticFiles):
    """Статика приложения: всё закрыто, кроме страницы входа и её файлов."""

    def __init__(self, *args, sessions: AssetSessions, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sessions = sessions

    async def get_response(self, path: str, scope) -> Response:
        request = Request(scope)
        name = path.replace(os.sep, "/")
        session = self._sessions.scope(request.cookies.get(COOKIE_NAME))
        if name in PUBLIC_FILES:
            return self._no_cache(await super().get_response(name, scope))
        if name in (".", "index.html"):
            page = "index.html" if session == SCOPE_APP else LOGIN_PAGE
            response = await super().get_response(page, scope)
            # Один адрес отдаёт разные страницы: браузер не должен хранить ответ.
            response.headers["Cache-Control"] = "no-store"
            return response
        if name == VIEWER_PAGE:
            return await self._viewer_page(request, scope, session)
        allowed = session == SCOPE_APP or (session == SCOPE_VIEWER and name in VIEWER_FILES)
        if not allowed:
            return PlainTextResponse(
                "Требуется вход", status_code=401, headers={"Cache-Control": "no-store"}
            )
        return self._no_cache(await super().get_response(name, scope))

    async def _viewer_page(self, request: Request, scope, session: str | None) -> Response:
        """Вьювер открывается по ссылке с токеном: cookie выдаётся здесь же."""
        token: str | None = None
        max_age = 0
        if session is None:
            params = request.query_params
            creds = _decode_token(params["token"]) if params.get("token") else None
            if creds and await authenticate_async(*creds) is not None:
                token = self._sessions.issue_app(*creds)
                max_age = SESSION_TTL_SEC
            elif params.get("vt") and verify_vnc_access(params["vt"], params.get("iid", "")):
                token = self._sessions.issue_viewer()
                max_age = VIEWER_TTL_SEC
            else:
                return RedirectResponse("./", status_code=303)
        response = await super().get_response(VIEWER_PAGE, scope)
        response.headers["Cache-Control"] = "no-store"
        if token is not None:
            set_asset_cookie(response, request, token, max_age)
        return response

    @staticmethod
    def _no_cache(response: Response) -> Response:
        # Кэш допустим только после проверки: каждый запрос проходит через сервер.
        response.headers["Cache-Control"] = "no-cache"
        return response


def install_asset_routes(app: FastAPI, sessions: AssetSessions) -> None:
    """Обмен сохранённого входа на cookie и выход."""

    @app.post("/api/asset-session")
    async def asset_session(request: Request) -> Response:
        """Страница входа сама восстанавливает cookie после перезапуска сервера."""
        creds = _basic_credentials(request)
        if creds is None or await authenticate_async(*creds) is None:
            # Без WWW-Authenticate: браузер не должен показать системное окно входа.
            raise HTTPException(401, "Неверные учётные данные")
        response = JSONResponse({"success": True})
        set_asset_cookie(response, request, sessions.issue_app(*creds), SESSION_TTL_SEC)
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> Response:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"success": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response
