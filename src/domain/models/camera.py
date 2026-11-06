"""Модель камеры видеонаблюдения ЕЦХД."""

from __future__ import annotations

import datetime
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

RTSP_PORT = 554


def parse_camera_endpoint(value: str) -> tuple[str, str, int, str]:
    """Разобрать IP/имя, необязательные порт и путь либо RTSP URL без пароля."""
    if not isinstance(value, str):
        raise ValueError("Адрес RTSP должен быть строкой")  # noqa: TRY004 — единый тип ошибок валидации адреса
    value = value.strip()
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Укажите адрес RTSP без пробелов и управляющих символов")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        if address.version == 6:
            value = f"[{value}]"
    try:
        explicit_scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value)
        parts = urlsplit(value if explicit_scheme else "rtsp://" + value)
        host = parts.hostname
        port = RTSP_PORT if parts.port is None else parts.port
    except ValueError:
        raise ValueError("Некорректный адрес или порт RTSP") from None
    if (
        parts.scheme not in ("rtsp", "rtsps") or not host
        or not 1 <= port <= 65535 or parts.fragment
    ):
        raise ValueError("Ожидается адрес RTSP, например 192.0.2.1:554/onvif1")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Логин и пароль задаются на вкладке «Подключение», отдельно от адреса")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("Некорректное имя камеры") from None
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            raise ValueError("Некорректное имя камеры")
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path = (path or "/") + "?" + quote(parts.query, safe="=&?/%:@!$'()*+,;~-._")
    return parts.scheme, host, port, path


# Подстановки данных камеры в шаблонах каталога, пути и имени файла.
CAMERA_TOKENS = ("%addr%", "%cabinet%", "%camera%")

# Подстановки даты/времени — единый набор для каталога, пути и имени файла.
DATETIME_TOKENS = ("%year%", "%month%", "%day%")
TIME_TOKENS = ("%hour%", "%min%", "%sec%")

# Все %токены%, защищаемые от strftime при построении имени файла.
FILE_MASK_TOKENS = CAMERA_TOKENS + DATETIME_TOKENS + TIME_TOKENS

# Маска имени по умолчанию, если шаблон не задан: имя камеры обязательно -
# при общем каталоге без неё записи разных камер (и перезапуски после
# восстановления связи) совпадают по имени файла и перезаписывают друг друга.
DEFAULT_FILE_MASK = "%camera%_%Y_%m_%d__%H_%M_%S"

# Символы, запрещённые в именах файлов/каталогов (Windows + разделители).
_FORBIDDEN_CHARS = set('/\\:*?"<>|') | {chr(c) for c in range(32)}


def sanitize_token(value: str) -> str:
    """Очистить значение для подстановки в путь/имя файла.

    Запрещённые символы заменяются на '_', trailing пробелы/точки
    (невидимые в Windows) срезаются. Пустое → '_'.
    """
    cleaned = "".join("_" if ch in _FORBIDDEN_CHARS else ch for ch in value or "")
    cleaned = cleaned.strip().strip(".")
    if cleaned.upper().split(".")[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned or "_"


def redact_camera_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Удалить учётные данные из URL и диагностических сообщений."""
    result = re.sub(r"(?i)(rtsps?://)[^\s]*@", r"\1***@", str(text))
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        result = result.replace(secret, "***").replace(quote(secret, safe=""), "***")
    return result


def validate_record_path(path: Path) -> None:
    """Проверить переносимые имена компонентов пути записи."""
    for part in path.parts:
        if part == path.anchor:
            continue
        if (
            part in (".", "..")
            or part != part.rstrip(" .")
            or any(c in _FORBIDDEN_CHARS for c in part)
        ):
            raise ValueError("Недопустимый компонент пути записи")
        if part.upper().split(".")[0] in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ValueError("Запрещённое имя файла или каталога")


def _token_values(
    address: str,
    cabinet: str,
    camera_name: str,
    dt: datetime.datetime,
) -> tuple[str, ...]:
    """Значения подстановок в порядке FILE_MASK_TOKENS (единый источник)."""
    return (
        sanitize_token(address),
        sanitize_token(cabinet),
        sanitize_token(camera_name),
        dt.strftime("%Y"),
        dt.strftime("%m"),
        dt.strftime("%d"),
        dt.strftime("%H"),
        dt.strftime("%M"),
        dt.strftime("%S"),
    )


def apply_camera_tokens(
    mask: str,
    address: str,
    cabinet: str,
    camera_name: str,
    dt: datetime.datetime,
) -> str:
    """Подставить данные камеры и дату/время в шаблон каталога или пути.

    Единый набор: %addr%, %cabinet%, %camera%, %year%, %month%, %day%,
    %hour%, %min%, %sec%. Значения камеры санитизируются.
    """
    if not mask:
        return ""
    result = mask
    for token, value in zip(
        FILE_MASK_TOKENS, _token_values(address, cabinet, camera_name, dt)
    ):
        result = result.replace(token, value)
    return result


def build_file_stem(
    camera_name: str,
    address: str,
    cabinet: str,
    file_mask: str,
    dt: datetime.datetime,
) -> str:
    """Имя файла записи (без расширения) по шаблону.

    Те же %токены%, что в шаблоне пути (см. ``apply_camera_tokens``),
    плюс strftime-спецификаторы (%Y и пр.). Подстановки экранируются
    на время strftime (иначе %camera% → %c) и восстанавливаются после.

    Пустой шаблон — ``DEFAULT_FILE_MASK``: имя камеры + дата/время.
    """
    mask = (file_mask or "").strip()
    if not mask:
        mask = DEFAULT_FILE_MASK
    # Экранируем подстановки до strftime (%camera% содержит %c — съелся бы).
    escaped = mask
    marker = f"DNO_TOKEN_{uuid4().hex}_"
    for i, token in enumerate(FILE_MASK_TOKENS):
        escaped = escaped.replace(token, f"{marker}{i}_END")
    remainder = re.sub(
        r"%(?:[-_0^#]*\d*)?[aAbBcCdDeFgGhHIjklmMnprRStTuUVwWxXyYzZfs%]", "", escaped
    )
    if "%" in remainder:
        raise ValueError("Неизвестная подстановка в шаблоне имени файла")
    try:
        formatted = dt.strftime(escaped)
    except (ValueError, OSError) as exc:
        raise ValueError("Некорректный формат даты в имени файла") from exc
    # Восстанавливаем значения (тот же словарь, что в apply_camera_tokens).
    for i, value in enumerate(_token_values(address, cabinet, camera_name, dt)):
        formatted = formatted.replace(f"{marker}{i}_END", value)
    return formatted


@dataclass
class CameraModel:
    """Камера видеонаблюдения ЕЦХД.

    Иерархия: ``Адрес → Кабинет → Камера``. Сохраняемый UUID переживает
    переименование и перенос; старый составной ключ нужен для миграции.
    """

    address: str
    cabinet: str
    camera_name: str
    ip_address: str
    login: str = ""
    password: str = field(default="", repr=False)
    player: str = "vlc"
    output_path: str = ""
    path_mask: str = ""
    file_mask: str = ""
    iid: str = ""

    @property
    def identifier(self) -> str:
        return self.iid or self.legacy_identifier

    @property
    def legacy_identifier(self) -> str:
        return f"{self.address}::{self.cabinet}::{self.camera_name}"

    @property
    def caption(self) -> str:
        return f"{self.address}, {self.cabinet}, {self.camera_name}"

    @property
    def rtsp_url(self) -> str:
        scheme, host, port, path = parse_camera_endpoint(self.ip_address)
        userinfo = (
            f"{quote(self.login, safe='')}:{quote(self.password, safe='')}@"
            if self.login or self.password
            else ""
        )
        authority = f"[{host}]" if ":" in host else host
        return f"{scheme}://{userinfo}{authority}:{port}{path}"

    @property
    def rtsp_host(self) -> str:
        """Имя узла без порта и пути для проверки доступности."""
        return parse_camera_endpoint(self.ip_address)[1]

    @property
    def rtsp_port(self) -> int:
        """Фактический порт RTSP, включая нестандартный порт камеры."""
        return parse_camera_endpoint(self.ip_address)[2]

    def _apply_mask(self, mask: str, dt: datetime.datetime) -> str:
        return apply_camera_tokens(
            mask, self.address, self.cabinet, self.camera_name, dt
        )

    def _relative_path(self, now: datetime.datetime) -> str:
        if self.path_mask:
            return self._apply_mask(self.path_mask, now).rstrip("/\\")
        addr = sanitize_token(self.address)
        cab = sanitize_token(self.cabinet)
        return f"{addr}/{now.strftime('%Y/%m/%d')}/{cab}"

    def record_file(self) -> str:
        """Рассчитать имя; эксклюзивное создание выполняет поток записи."""
        now = datetime.datetime.now()
        path = self.record_dir(now)
        stem = self.file_stem(now)
        validate_record_path(Path(stem))
        if Path(stem).name != stem or not stem:
            raise ValueError("Шаблон имени должен задавать только имя файла")
        return str(path / f"{stem}.mkv")

    def file_stem(self, now: datetime.datetime | None = None) -> str:
        """Имя файла записи (без расширения), как его построит запись."""
        if now is None:
            now = datetime.datetime.now()
        return build_file_stem(
            self.camera_name, self.address, self.cabinet, self.file_mask, now
        )

    def record_dir(self, now: datetime.datetime | None = None) -> Path:
        """Каталог, куда ``record_file`` сохраняет запись прямо сейчас.

        Логика пути полностью совпадает с ``record_file`` —
        включая компонент даты/времени, чтобы «Открыть папку» показывал
        реально созданные файлы.
        """
        if now is None:
            now = datetime.datetime.now()
        root = self._record_root(now).expanduser().absolute()
        relative = Path(self._relative_path(now))
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("Шаблон пути должен оставаться внутри каталога записи")
        validate_record_path(relative)
        validate_record_path(root)
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Путь записи выходит за корневой каталог")
        return path

    def _record_root(self, now: datetime.datetime | None = None) -> Path:
        """Корневой каталог записи; поддерживает те же %подстановки%."""
        if not self.output_path:
            return Path.home()
        if now is None:
            now = datetime.datetime.now()
        expanded = self._apply_mask(self.output_path, now)
        return Path(expanded) if expanded else Path.home()
