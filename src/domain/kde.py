"""Снимки, черновики и сохраняющий исходный текст редактор KConfig."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any


@dataclass(frozen=True, order=True)
class KdeKey:
    """Адрес параметра внутри одного файла, без флагов группы."""

    group: str
    name: str

    @property
    def label(self) -> str:
        return f"[{self.group}] / {self.name}"


@dataclass(frozen=True)
class KdeEntry:
    """Исходная запись с позицией и флагами KConfig."""

    key: KdeKey
    value: str
    line: int
    prefix: str
    immutable: bool = False
    editable: bool = True


class KdeDocument:
    """Редактирует поддержанный поднабор KConfig без INI-интерполяции.

    Неизменённые строки сохраняются побайтно. Неоднозначный синтаксис
    запрещает запись документа, но не препятствует просмотру исходника.
    """

    def __init__(self, raw: bytes) -> None:
        self.raw = bytes(raw)
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        self.issues: list[str] = []
        try:
            self.text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.text = raw.decode("utf-8-sig", errors="replace")
            self.issues.append(
                "Файл содержит данные вне UTF-8; доступен только просмотр."
            )
        self.lines = self.text.splitlines(keepends=True)
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        self.entries: dict[KdeKey, KdeEntry] = {}
        self.groups: dict[str, list[int]] = {}
        self.group_immutable: set[str] = set()
        self.file_immutable = False
        self._parse()

    @property
    def writable(self) -> bool:
        return not self.issues

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def _parse(self) -> None:
        group = ""
        locked = False
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                if not re.fullmatch(r"(?:\[[^\]\r\n]*\])+", stripped):
                    self.issues.append(
                        f"Строка {index + 1}: неподдерживаемый заголовок группы."
                    )
                    continue
                tokens = re.findall(r"\[([^\]]*)\]", stripped)
                # KConfig считает флагом группы только завершающий [$i].
                # [$Version] и другие имена с '$' остаются обычными группами.
                group_locked = tokens[-1] == "$i"
                names = tokens[:-1] if group_locked else tokens
                if not names:
                    self.file_immutable = True
                    group = ""
                    locked = True
                    continue
                if any(not name for name in names):
                    self.issues.append(f"Строка {index + 1}: пустая группа.")
                    continue
                group = "][".join(names)
                locked = group_locked or self.file_immutable
                self.groups.setdefault(group, []).append(index)
                if locked:
                    self.group_immutable.add(group)
                continue
            if "=" not in line or "\x00" in line or not group:
                self.issues.append(
                    f"Строка {index + 1}: неподдерживаемая запись KConfig."
                )
                continue
            left, value = line.rstrip("\r\n").split("=", 1)
            name = left.strip()
            key_flags = re.findall(r"\[\$([^\]]+)\]", name)
            immutable = any("i" in flag for flag in key_flags) or locked
            editable = all(flag == "i" for flag in key_flags)
            name = re.sub(r"\[\$[^\]]+\]", "", name)
            if not name or "\\" in name:
                self.issues.append(f"Строка {index + 1}: неподдерживаемое имя ключа.")
                continue
            key = KdeKey(group, name)
            if key in self.entries:
                self.issues.append(
                    f"Повторяющийся ключ {key.label}; запись неоднозначна."
                )
            self.entries[key] = KdeEntry(
                key, value, index, left + "=", immutable, editable
            )

    def patch(self, changes: dict[KdeKey, str | None]) -> bytes:
        """Применить только явные изменения, сохранив остальной документ."""
        if not changes:
            return self.raw
        if not self.writable:
            raise ValueError("Запись недоступна: " + " ".join(self.issues))
        replacements: dict[int, str] = {}
        additions: dict[str, list[tuple[str, str]]] = {}
        for key, value in sorted(changes.items()):
            self.validate_key(key)
            if value is not None and any(c in value for c in ("\n", "\r", "\x00")):
                raise ValueError(f"{key.label}: значение должно занимать одну строку.")
            entry = self.entries.get(key)
            if entry:
                if not entry.editable:
                    raise ValueError(
                        f"{key.label}: запись с [$e]/[$d] доступна только для просмотра."
                    )
                if value is None:
                    replacements[entry.line] = ""
                elif value != entry.value:
                    original = self.lines[entry.line]
                    ending = (
                        "\r\n"
                        if original.endswith("\r\n")
                        else "\n" if original.endswith("\n") else ""
                    )
                    replacements[entry.line] = entry.prefix + value + ending
            elif value is not None:
                additions.setdefault(key.group, []).append((key.name, value))
        insertions: dict[int, list[str]] = {}
        new_groups: list[str] = []
        headers = sorted(i for positions in self.groups.values() for i in positions)
        for group, values in additions.items():
            if group in self.groups:
                last = self.groups[group][-1]
                end = next((i for i in headers if i > last), len(self.lines))
                insertions.setdefault(end, []).extend(
                    f"{key}={value}{self.newline}" for key, value in values
                )
            else:
                new_groups.append(f"[{group}]{self.newline}")
                new_groups.extend(
                    f"{key}={value}{self.newline}" for key, value in values
                )
        output: list[str] = []
        for index in range(len(self.lines) + 1):
            if index in insertions:
                if output and output[-1] and not output[-1].endswith(("\n", "\r")):
                    output.append(self.newline)
                output.extend(insertions[index])
            if index < len(self.lines):
                output.append(replacements.get(index, self.lines[index]))
        if new_groups:
            if output and output[-1] and not output[-1].endswith(("\n", "\r")):
                output.append(self.newline)
            output.extend(new_groups)
        text = "".join(output)
        return (b"\xef\xbb\xbf" if self.bom else b"") + text.encode("utf-8")

    @staticmethod
    def validate_key(key: KdeKey) -> None:
        if (
            not key.group
            or not key.name
            or any(c in key.name for c in ("=", "\n", "\r", "\x00", "\\"))
            or any(c in key.group for c in ("\n", "\r", "\x00", "\\"))
            or any(
                not part or "[" in part or "]" in part or part.startswith("$")
                for part in key.group.split("][")
            )
            or key.name.startswith(("#", "["))
        ):
            raise ValueError("Недопустимое имя группы или ключа KConfig.")


@dataclass(frozen=True)
class KdeProfile:
    """Пользователь, подтверждённый NSS на удалённой машине."""

    username: str
    uid: int
    gid: int
    home: str
    sessions: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> KdeProfile:
        return cls(
            str(value["username"]),
            int(value["uid"]),
            int(value["gid"]),
            str(value["home"]),
            tuple(value.get("sessions", ())),
        )


@dataclass(frozen=True)
class KdeTarget:
    """Неизменяемый адрес снимка; не зависит от текущего combo в GUI."""

    identity: tuple[str, str]
    host: str
    profile: KdeProfile
    path: str


@dataclass(frozen=True)
class KdeSource:
    """Системный документ или причина невозможности прочитать источник."""

    path: str
    raw: bytes
    fingerprint: str
    error: str = ""


@dataclass(frozen=True)
class KdeSnapshot:
    """Один прочитанный файл с окружением и источниками политики."""

    target: KdeTarget
    raw: bytes
    exists: bool
    fingerprint: str
    sources: tuple[KdeSource, ...] = ()
    version: str = ""
    environment_note: str = ""
    resources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    loaded_at: str = ""

    @cached_property
    def document(self) -> KdeDocument:
        return KdeDocument(self.raw)

    @cached_property
    def source_documents(self) -> tuple[tuple[KdeSource, KdeDocument], ...]:
        return tuple((source, KdeDocument(source.raw)) for source in self.sources)

    def policy(self, key: KdeKey) -> tuple[str | None, str, bool]:
        """Возвращает известное значение/источник, не обещая эффект приложения."""
        value: str | None = None
        source = "Не задано"
        locked = False
        incomplete = False
        for item, document in self.source_documents:
            if item.error or document.issues:
                incomplete = True
            entry = document.entries.get(key)
            if locked:
                continue
            if entry:
                value, source = entry.value, item.path
            if (
                document.file_immutable
                or key.group in document.group_immutable
                or (entry and entry.immutable)
            ):
                locked = True
                source = item.path
        local = self.document.entries.get(key)
        if not locked and local:
            value, source = local.value, self.target.path
        if incomplete:
            source += " · не все источники прочитаны"
        return value, source, locked


@dataclass
class KdeDraft:
    """Явные правки поверх неизменяемого снимка."""

    snapshot: KdeSnapshot
    changes: dict[KdeKey, str | None] = field(default_factory=dict)

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    def value(self, key: KdeKey) -> str | None:
        if key in self.changes:
            return self.changes[key]
        entry = self.snapshot.document.entries.get(key)
        return entry.value if entry else None

    def set(self, key: KdeKey, value: str | None) -> None:
        document = self.snapshot.document
        if not document.writable:
            raise ValueError("Документ доступен только для просмотра.")
        if self.snapshot.policy(key)[2]:
            raise ValueError("Параметр заблокирован системной политикой.")
        KdeDocument.validate_key(key)
        if value is not None and any(c in value for c in ("\r", "\n", "\x00")):
            raise ValueError("Значение должно занимать одну строку.")
        entry = document.entries.get(key)
        if entry and not entry.editable:
            raise ValueError(
                "Запись с динамическими флагами доступна только для просмотра."
            )
        original = entry.value if entry else None
        if value == original:
            self.changes.pop(key, None)
        else:
            self.changes[key] = value

    def render(self) -> bytes:
        return self.snapshot.document.patch(self.changes)

    def rebase(self, remote: KdeSnapshot) -> tuple[KdeDraft, tuple[KdeKey, ...]]:
        """Переносит независимые правки, возвращая конфликтующие ключи отдельно."""
        old_target = self.snapshot.target
        new_target = remote.target
        if (
            new_target.identity,
            new_target.host,
            new_target.profile.username,
            new_target.profile.uid,
            new_target.profile.home,
            new_target.path,
        ) != (
            old_target.identity,
            old_target.host,
            old_target.profile.username,
            old_target.profile.uid,
            old_target.profile.home,
            old_target.path,
        ):
            raise ValueError("Нельзя объединить разные профили или файлы.")
        result = KdeDraft(remote)
        conflicts: list[KdeKey] = []
        old = self.snapshot.document.entries
        new = remote.document.entries
        for key, value in self.changes.items():
            before = old[key].value if key in old else None
            after = new[key].value if key in new else None
            if after == value:
                continue
            if before != after or remote.policy(key)[2]:
                conflicts.append(key)
            else:
                result.set(key, value)
        return result, tuple(conflicts)
