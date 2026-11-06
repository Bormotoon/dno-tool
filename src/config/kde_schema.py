"""Проверенные адреса настроек и статус исторического каталога KDE."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from src.config.kde_catalog_audit import CATALOG_AUDIT
from src.config.kde_help import AUDIT_STATUSES, description_for
from src.config.kde_labels import title_for
from src.config.kde_settings import SECTIONS
from src.domain.kde import KdeKey, KdeSnapshot


@dataclass(frozen=True)
class KdeSetting:
    """Семантическое описание параметра без предполагаемого значения по умолчанию."""

    filename: str
    key: KdeKey
    title: str
    description: str
    category: str
    kind: str = "string"
    resource: str = ""
    support: str = "Маршрут подтверждён; действие зависит от версии и приложения KDE."
    choices: tuple[tuple[str, str], ...] = ()
    source: str = ""
    editable: bool = True

    @property
    def apply_hint(self) -> str:
        if self.category == "expert":
            return "Способ применения не подтверждён."
        if self.filename == "dolphinrc":
            return "Новый запуск Dolphin; при необходимости закройте его после сохранения работы."
        if self.filename == "kwinrc":
            return "Повторный вход в сеанс KDE. KWin автоматически не перезапускается."
        if self.filename == "konsolerc":
            return (
                "Новые окна Konsole; для существующих может потребоваться новый запуск."
            )
        if self.filename in ("plasmarc", "krunnerrc"):
            return "Компонент может перечитать настройки автоматически; при необходимости повторный вход."
        if self.kind == "restriction":
            return "Новый запуск поддерживающего правило компонента KDE; может потребоваться повторный вход."
        return "Новые приложения KDE; для открытых приложений может потребоваться повторный вход."

    def validate(self, value: str | None) -> None:
        """Проверить явную правку; исходное неизвестное значение не нормализуется."""
        if value is None:
            return
        if any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError("Значение должно занимать одну строку.")
        if self.kind in ("bool", "restriction") and value not in ("true", "false"):
            raise ValueError("Выберите одно из предложенных значений.")
        if self.kind == "enum" and value not in {stored for _, stored in self.choices}:
            raise ValueError("Выберите значение из списка поддерживаемых вариантов.")
        if self.kind == "color":
            rgb = re.fullmatch(r"(\d{1,3}),(\d{1,3}),(\d{1,3})(?:,(\d{1,3}))?", value)
            if not rgb or any(
                int(part) > 255 for part in rgb.groups() if part is not None
            ):
                raise ValueError(
                    "Ожидается цвет R,G,B или R,G,B,A; компоненты от 0 до 255."
                )
        if self.kind == "font":
            parts = value.split(",")
            if len(parts) < 10 or not parts[0].strip():
                raise ValueError(
                    "Ожидается сохранённая строка QFont с семейством и размером."
                )
            try:
                size = float(parts[1])
            except ValueError as error:
                raise ValueError("Некорректный размер шрифта.") from error
            if not 1 <= size <= 512:
                raise ValueError("Размер шрифта должен быть от 1 до 512 пунктов.")


def _build() -> tuple[KdeSetting, ...]:
    settings: list[KdeSetting] = []
    action_keys = (
        "shell_access",
        "run_command",
        "logout",
        "lock_screen",
        "switch_user",
        "start_new_session",
        "editable_desktop_icons",
        "movable_toolbars",
        "ghns",
        "lineedit_reveal_password",
        "lineedit_text_completion",
        "action/configdesktop",
        "action/run_command",
        "action/lock_screen",
        "action/switch_user",
        "action/start_new_session",
        "action/logout",
        "action/file_open",
        "action/file_save",
        "action/file_print",
        "action/edit_copy",
        "action/edit_paste",
        "action/settings",
        "action/options_configure",
        "action/options_configure_keybinding",
        "action/options_configure_toolbars",
        "plasma/containment_actions",
        "plasma/plasmashell/unlockedDesktop",
    )
    descriptions = SECTIONS["KDE Action Restrictions][$i"]
    for name in action_keys:
        title, description = descriptions.get(
            name, (name, "Ограничение возможности поддерживающего его компонента KDE.")
        )
        title = {
            "action/logout": "Выход из сеанса через интерфейс",
            "action/options_configure": "Настройка приложения",
            "action/switch_user": "Переключение пользователей",
            "action/start_new_session": "Новый графический сеанс",
        }.get(name, title)
        if name == "ghns":
            title, description = (
                "Загрузка дополнений",
                "Загрузка дополнений через интерфейс KDE Get Hot New Stuff.",
            )
        elif name == "plasma/containment_actions":
            title, description = (
                "Действия мыши рабочего стола",
                "Действия мыши на рабочем столе и панелях Plasma, включая контекстные меню.",
            )
        settings.append(
            KdeSetting(
                "kdeglobals",
                KdeKey("KDE Action Restrictions", name),
                title,
                description,
                "restrictions",
                "restriction",
            )
        )
    settings.append(
        KdeSetting(
            "kdeglobals",
            KdeKey("KDE Resource Restrictions", "wallpaper"),
            "Обои рабочего стола",
            "Доступ к пользовательским ресурсам обоев; не является универсальным запретом изменения фона.",
            "restrictions",
            "restriction",
        )
    )
    for module in (
        "kcm_mouse",
        "kcm_keyboard",
        "kcm_fonts",
        "kcm_colors",
        "kcm_lookandfeel",
        "kcm_kscreen",
    ):
        title, description = SECTIONS["KDE Control Module Restrictions][$i"].get(
            module + ".desktop", (module, "Модуль системных настроек KDE.")
        )
        settings.append(
            KdeSetting(
                "kdeglobals",
                KdeKey("KDE Control Module Restrictions", module),
                title,
                description,
                "restrictions",
                "restriction",
                support="Шаблон Kiosk по pluginId; наличие модуля и действие на этой версии нужно проверить.",
            )
        )
    for name in ("font", "fixed", "menuFont", "toolBarFont", "smallestReadableFont"):
        title, description = SECTIONS["General"][name]
        settings.append(
            KdeSetting(
                "kdeglobals",
                KdeKey("General", name),
                title,
                description,
                "appearance",
                "font",
                "fonts",
            )
        )
    settings.extend(
        (
            KdeSetting(
                "kdeglobals",
                KdeKey("General", "ColorScheme"),
                "Цветовая схема",
                "Идентификатор схемы на удалённом компьютере. Отдельные цветовые значения могут переопределять схему.",
                "appearance",
                resource="colors",
            ),
            KdeSetting(
                "kdeglobals",
                KdeKey("Icons", "Theme"),
                "Тема значков",
                "Имя установленной на хосте темы значков.",
                "appearance",
                resource="icons",
            ),
            KdeSetting(
                "kdeglobals",
                KdeKey("KDE", "widgetStyle"),
                "Стиль приложений",
                "Имя стиля Qt; доступность проверяется приложениями на хосте.",
                "appearance",
            ),
            KdeSetting(
                "kdeglobals",
                KdeKey("KDE", "SingleClick"),
                "Открывать одним щелчком",
                "Предпочтение приложений KDE; поддержка зависит от версии файлового менеджера.",
                "appearance",
                "bool",
            ),
        )
    )
    roles = (
        "BackgroundNormal",
        "BackgroundAlternate",
        "ForegroundNormal",
        "ForegroundInactive",
        "ForegroundActive",
        "ForegroundLink",
        "ForegroundVisited",
        "ForegroundNegative",
        "ForegroundNeutral",
        "ForegroundPositive",
        "DecorationFocus",
        "DecorationHover",
    )
    for group in (
        "Colors:Window",
        "Colors:Button",
        "Colors:Selection",
        "Colors:Tooltip",
        "Colors:View",
        "Colors:Complementary",
        "Colors:Header",
    ):
        for name in roles:
            if name in SECTIONS.get(group, {}):
                title, description = SECTIONS[group][name]
                settings.append(
                    KdeSetting(
                        "kdeglobals",
                        KdeKey(group, name),
                        title,
                        f"{group}: {description}",
                        "appearance",
                        "color",
                    )
                )
    for name, title, description in (
        (
            "ShowFullPath",
            "Полный путь в адресной строке",
            "Показывать полный путь в строке расположения Dolphin.",
        ),
        (
            "ShowFullPathInTitlebar",
            "Полный путь в заголовке",
            "Показывать полный путь в заголовке Dolphin.",
        ),
        (
            "AutoExpandFolders",
            "Автоматически раскрывать папки",
            "Автоматическое раскрытие папок в представлении Dolphin.",
        ),
        (
            "GlobalViewProps",
            "Общие настройки вида",
            "Использовать общие свойства вида для всех папок.",
        ),
    ):
        settings.append(
            KdeSetting(
                "dolphinrc",
                KdeKey("General", name),
                title,
                description,
                "applications",
                "bool",
            )
        )
    for name, title in (
        ("NextFocusPrefersMouse", "Следующее окно под указателем"),
        ("SeparateScreenFocus", "Раздельный фокус экранов"),
        ("RollOverDesktops", "Циклическое переключение рабочих столов"),
        ("CondensedTitle", "Сокращать заголовки окон"),
    ):
        settings.append(
            KdeSetting(
                "kwinrc",
                KdeKey("Windows", name),
                title,
                "Поведение окон KWin; доступность зависит от версии KDE.",
                "applications",
                "bool",
            )
        )
    settings.append(
        KdeSetting(
            "kwinrc",
            KdeKey("Windows", "FocusPolicy"),
            "Переключение фокуса",
            "Когда окно получает фокус клавиатуры.",
            "applications",
            "enum",
            choices=(
                ("По щелчку", "ClickToFocus"),
                ("За указателем мыши", "FocusFollowsMouse"),
                ("Под указателем мыши", "FocusUnderMouse"),
                ("Строго под указателем", "FocusStrictlyUnderMouse"),
            ),
        )
    )
    for name, title in (
        ("RememberWindowSize", "Запоминать размер окна"),
        ("UseSingleInstance", "Общий процесс для окон"),
        ("ForceNewTabs", "Новые вкладки вместо окон"),
        ("ShowProgressInTaskBar", "Прогресс в панели задач"),
        ("FocusFollowsMouse", "Фокус терминала за мышью"),
    ):
        settings.append(
            KdeSetting(
                "konsolerc",
                KdeKey("KonsoleWindow", name),
                title,
                "Поведение окон Konsole; существующие окна могут потребовать нового запуска.",
                "applications",
                "bool",
            )
        )
    settings.append(
        KdeSetting(
            "plasmarc",
            KdeKey("Theme", "name"),
            "Тема оболочки Plasma",
            "Идентификатор установленной на хосте темы рабочего стола и панелей.",
            "appearance",
            resource="desktopthemes",
        )
    )
    for name, title in (
        ("FreeFloating", "Плавающее окно KRunner"),
        ("RetainPriorSearch", "Сохранять предыдущий поисковый запрос"),
    ):
        settings.append(
            KdeSetting(
                "krunnerrc",
                KdeKey("General", name),
                title,
                "Поведение окна KRunner; настройка может перечитываться компонентом автоматически.",
                "applications",
                "bool",
            )
        )
    audited = {
        (entry["file"], entry["group"], entry["key"]): entry
        for entry in CATALOG_AUDIT["active"]
    }
    result: list[KdeSetting] = []
    for setting in settings:
        entry = audited.get((setting.filename, setting.key.group, setting.key.name))
        evidence = entry["evidence"] if entry else []
        support = setting.support
        source = ""
        if entry:
            versions = ", ".join(sorted({item["version"] for item in evidence}))
            support = (
                AUDIT_STATUSES[entry["status"]]
                + ". Сверено 26.10.2026"
                + (": " + versions if versions else "")
                + ". Действие зависит от версии и приложения на хосте."
            )
            source = "\n".join(dict.fromkeys(item["source"] for item in evidence[:2]))
        unsupported = (
            setting.key.group == "KDE Resource Restrictions"
            or setting.key.name
            in (
                "action/configdesktop",
                "lock_screen",
                "switch_user",
                "start_new_session",
                "action/settings",
            )
        )
        if unsupported:
            support = "Только просмотр: потребитель этого правила не подтверждён в проверенных выпусках KDE 6. Не использовать как работающий запрет."
        description = description_for(
            setting.filename, setting.key.group, setting.key.name, setting.description
        )
        if setting.key.group == "KDE Control Module Restrictions":
            description += " Запрет ограничивает доступ к модулю системных настроек. Компонент и его сохранённые настройки не удаляются; прямое редактирование файлов этим не запрещается."
        if setting.key.group == "KDE Resource Restrictions":
            description = "Историческое ограничение доступа к ресурсу обоев. Потребитель этой группы не найден в проверенных исходниках KDE 6; это не подтверждённый запрет смены фона."
        result.append(
            replace(
                setting,
                title=title_for(setting.key.group, setting.key.name, setting.title),
                description=description,
                support=support,
                source=source,
                editable=not unsupported,
                category="expert" if unsupported else setting.category,
            )
        )
    return tuple(result)


KDE_SETTINGS = _build()
KDE_SETTING_INDEX = {
    (setting.filename, setting.key): setting for setting in KDE_SETTINGS
}
KDE_FILES = {
    "kdeglobals": "Общие настройки KDE",
    "dolphinrc": "Файлы и папки · Dolphin",
    "kwinrc": "Управление окнами · KWin",
    "konsolerc": "Терминал · Konsole",
    "plasmarc": "Тема рабочего стола · Plasma",
    "krunnerrc": "Поиск и команды · KRunner",
}


def setting_for(filename: str, key: KdeKey) -> KdeSetting:
    """Неизвестная запись остаётся видимой с честным статусом поддержки."""
    known = KDE_SETTING_INDEX.get((filename, key))
    if known:
        return known
    descriptions = SECTIONS.get(key.group, SECTIONS.get(key.group + "][$i", {}))
    title, description = descriptions.get(
        key.name,
        (key.name, "Запись присутствует в файле, но её семантика не подтверждена."),
    )
    audited = next(
        (
            entry
            for entry in CATALOG_AUDIT["historical"]
            if entry["group"].removesuffix("][$i") == key.group
            and entry["key"] == key.name
        ),
        None,
    )
    if audited:
        description = audited["description"]
    title = title_for(key.group, key.name, title)
    return KdeSetting(
        filename,
        key,
        title,
        description,
        "expert",
        support="Непроверенный параметр: доступен просмотр исходного значения."
        + (" " + AUDIT_STATUSES[audited["status"]] if audited else ""),
        source=(
            "\n".join(item["source"] for item in audited["evidence"][:2])
            if audited
            else ""
        ),
        editable=False,
    )


def settings_for_snapshot(snapshot: KdeSnapshot) -> tuple[KdeSetting, ...]:
    """Каталог существует независимо от наличия личного файла на диске."""
    from pathlib import PurePosixPath

    filename = PurePosixPath(snapshot.target.path).name
    settings = {
        setting.key: setting for setting in KDE_SETTINGS if setting.filename == filename
    }
    if filename == "kdeglobals":
        for module in snapshot.resources.get("modules", ()):
            key = KdeKey("KDE Control Module Restrictions", module)
            title, description = SECTIONS["KDE Control Module Restrictions][$i"].get(
                module + ".desktop", (module, "Модуль системных настроек KDE.")
            )
            settings[key] = KdeSetting(
                filename,
                key,
                title_for(key.group, module, title),
                description
                + " Запрет ограничивает доступ к этому модулю системных настроек; сам компонент и уже сохранённые настройки не удаляются.",
                "restrictions",
                "restriction",
                support="PluginId получен от kcmshell на хосте; применение ограничения нужно проверить отдельно.",
            )
    return tuple(settings.values())


def editable_setting(snapshot: KdeSnapshot, key: KdeKey) -> KdeSetting | None:
    return next(
        (
            setting
            for setting in settings_for_snapshot(snapshot)
            if setting.key == key and setting.editable
        ),
        None,
    )


def catalog_status() -> list[dict[str, str]]:
    """Аудируемый статус каждой записи прежнего каталога без догадок о типе."""
    result: list[dict[str, str]] = []
    for entry in CATALOG_AUDIT["historical"]:
        section, name = entry["group"], entry["key"]
        canonical = section.removesuffix("][$i")
        definition = KDE_SETTING_INDEX.get(("kdeglobals", KdeKey(canonical, name)))
        result.append(
            {
                "group": section,
                "key": name,
                "title": (
                    definition.title
                    if definition
                    else title_for(canonical, name, entry["title"])
                ),
                "status": (
                    "route_checked"
                    if definition and definition.editable
                    else "unverified_read_only"
                ),
                "file": "kdeglobals" if definition else "",
                "type": definition.kind if definition else "raw",
                "description": (
                    definition.description if definition else entry["description"]
                ),
                "audit_status": AUDIT_STATUSES[entry["status"]],
                "source": "\n".join(item["source"] for item in entry["evidence"]),
                "routes": "\n".join(
                    dict.fromkeys(
                        str(item.get("file", ""))
                        + " ["
                        + str(item.get("group", ""))
                        + "] "
                        + str(item.get("key", ""))
                        for item in entry["evidence"]
                        if item.get("file") or item.get("group")
                    )
                ),
            }
        )
    return result
