"""Строгие схемы и ограничение размера запросов федерации нод."""

from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from pydantic import field_validator, model_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MAX_NODE_REQUEST_BYTES = 2 * 1024 * 1024
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=256)]
ShortText = Annotated[str, StringConstraints(max_length=4096)]
CommandText = Annotated[str, StringConstraints(min_length=1, max_length=65536)]
Port = Annotated[int, Field(ge=1, le=65535)]


class NodePayload(BaseModel):
    """Общий контракт: неизвестные поля и неявные преобразования запрещены."""

    model_config = ConfigDict(strict=True, extra="forbid")


class NodeCommandPayload(NodePayload):
    """Команда с сохранением порядка и намеренных повторов."""

    type: Literal["ssh", "sftp", "local"] = "ssh"
    text: CommandText
    background: bool = False
    desktop_session: bool = False

    @model_validator(mode="after")
    def validate_execution_modes(self) -> "NodeCommandPayload":
        """Отклонить режимы SSH для local/SFTP до создания задания."""
        from src.domain.task_options import background_enabled, desktop_session_for_step

        background_enabled(self.model_dump())
        desktop_session_for_step(self.model_dump())
        return self


class NodeExecutePayload(NodePayload):
    """Запуск ограниченного набора устройств одним заданием."""

    commands: Annotated[list[NodeCommandPayload], Field(min_length=1, max_length=256)]
    device_iids: Annotated[list[Identifier], Field(min_length=1, max_length=4096)]
    timeout: Annotated[int, Field(ge=1, le=86400)] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    session: bool = False
    desktop_session: bool = False

    @field_validator("device_iids")
    @classmethod
    def unique_devices(cls, values: list[str]) -> list[str]:
        """Не выполнять побочный эффект дважды для одного устройства."""
        if len(set(values)) != len(values):
            raise ValueError("Повторяющиеся device_iids запрещены")
        return values


class NodeDevicePayload(NodePayload):
    """Разрешённые поля устройства; пропуски сохраняют прежние значения."""

    iid: Identifier | None = None
    name: ShortText | None = None
    host: ShortText | None = None
    port: Port | None = None
    icon: ShortText | None = None
    icon_offline: ShortText | None = None
    icon_online: ShortText | None = None
    credential_bundle_iid: Identifier | None = None
    mac_address: ShortText | None = None
    vnc_port: Port | None = None
    thumbnail_backend: Literal["veyon", "vnc"] | None = None
    use_sudo: bool | None = None
    locale: ShortText | None = None

    @field_validator("name", "host")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str:
        """Явное пустое имя или адрес не является сбросом наследования."""
        if value is None or not value.strip():
            raise ValueError("Имя и адрес не могут быть пустыми")
        return value.strip()


class NodeHostsPayload(NodePayload):
    """Создание, частичное изменение или удаление устройства."""

    op: Literal["add", "update", "delete"]
    iid: Identifier | None = None
    device: NodeDevicePayload | None = None
    folder: ShortText | None = None

    @model_validator(mode="after")
    def required_fields(self) -> "NodeHostsPayload":
        """Проверить обязательные поля выбранной операции до изменения данных."""
        if self.op != "add" and not self.iid:
            raise ValueError("Для update/delete требуется iid")
        if self.op in ("add", "update") and self.device is None:
            raise ValueError("Для add/update требуется device")
        if self.op == "add" and (not self.device.name or not self.device.host):
            raise ValueError("Для add требуются name и host")
        if self.op == "update" and self.device.iid not in (None, self.iid):
            raise ValueError("Нельзя изменить iid устройства")
        return self


class NodeFeatureArgs(NodePayload):
    """Типизированные аргументы поддерживаемых операций управления."""

    mac: ShortText = ""
    username: ShortText = ""
    password: ShortText = ""
    url: ShortText = ""
    text: Annotated[str, StringConstraints(max_length=65536)] = ""
    applications: Annotated[list[CommandText], Field(max_length=256)] = Field(
        default_factory=list
    )


class NodeFeaturePayload(NodePayload):
    """Вызов фичи по её API-токену."""

    iid: Identifier
    feature: Identifier
    args: NodeFeatureArgs = Field(default_factory=NodeFeatureArgs)


class NodeReleasePayload(NodePayload):
    """Снятие подписок только вызывающего потребителя."""

    iids: Annotated[list[Identifier], Field(max_length=4096)]


class NodeStatusBatchPayload(NodePayload):
    """Статусы нескольких хостов одним запросом."""

    iids: Annotated[list[Identifier], Field(max_length=512)]


class NodeInputPayload(NodePayload):
    """Ввод для задания без неявного преобразования произвольного объекта."""

    device_iid: Identifier | None = None
    text: Annotated[str, StringConstraints(max_length=65536)]


class NodeChallengePayload(NodePayload):
    """Запрос challenge: тело пустое, клиент себя не называет."""


class NodeVerifyPayload(NodePayload):
    """Одноразовый challenge и ответ строго заданного размера.

    ``proof`` — HMAC-SHA256 ключа доступа, ``master_signature`` — подпись
    Ed25519 служебным ключом; наличие ровно одного проверяет маршрут.
    """

    challenge: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    client_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    client_name: Annotated[str, StringConstraints(max_length=128)] = ""
    proof: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    master_signature: (
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")] | None
    ) = None


def validate_node_payload(
    model: type[NodePayload], payload: dict[str, Any]
) -> dict[str, Any]:
    """Вернуть проверенные поля без значений по умолчанию и секретов в ошибке."""
    try:
        return model.model_validate(payload).model_dump(exclude_unset=True)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"])
        raise HTTPException(422, f"{location}: {first['msg']}") from None


class NodeBodyLimitMiddleware:
    """Ограничить тело до разбора JSON, включая запросы без Content-Length."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in (
            "POST",
            "PUT",
            "PATCH",
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_NODE_REQUEST_BYTES:
                await JSONResponse({"detail": "Тело запроса превышает 2 МиБ"}, 413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
