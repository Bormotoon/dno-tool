"""Русские подписи; перевод не подтверждает маршрут или поддержку параметра."""

from __future__ import annotations

import re

from src.config.kde_settings import SECTION_NAMES, SECTIONS

COMMON_TITLES = {
    "BrowserApplication": "Веб-браузер по умолчанию",
    "TerminalApplication": "Терминал по умолчанию",
    "TerminalService": "Служба терминала по умолчанию",
    "LookAndFeelPackage": "Общее оформление рабочего стола",
    "ColorScheme": "Цветовая схема приложений",
    "ColorSchemeHash": "Контрольная сумма цветовой схемы",
    "AccentColor": "Цвет выделения",
    "LastUsedCustomAccentColor": "Последний выбранный цвет выделения",
    "AccentColorFromWallpaper": "Цвет выделения из обоев",
    "AllowKDEAppsToRememberWindowPositions": "Запоминание положения окон приложений",
    "ShowDeleteCommand": "Команда безвозвратного удаления",
    "ShowIconsInMenuItems": "Значки в пунктах меню",
    "ShowIconsOnPushButtons": "Значки на кнопках",
    "ShowMenubar": "Строка меню",
    "ShowStatusbar": "Строка состояния",
    "ShowStatusBar": "Строка состояния",
    "ToolButtonStyle": "Подписи кнопок панели инструментов",
    "ToolButtonStyleOtherToolbars": "Подписи кнопок дополнительных панелей",
    "XftAntialias": "Сглаживание шрифтов",
    "XftHintStyle": "Уточнение контуров шрифта",
    "XftSubPixel": "Субпиксельное сглаживание шрифтов",
    "forceFontDPI": "Принудительная плотность шрифтов",
    "AnimationDurationFactor": "Скорость анимации интерфейса",
    "animationDurationFactor": "Скорость анимации интерфейса",
    "DoubleClickInterval": "Интервал двойного щелчка",
    "CursorBlinkRate": "Частота мигания текстового курсора",
    "SmoothScroll": "Плавная прокрутка",
    "ScrollbarLeftClickNavigatesByPage": "Прокрутка на страницу при щелчке по полосе",
    "DndBehavior": "Действие при перетаскивании файлов",
    "DefaultProfile": "Профиль терминала по умолчанию",
    "DefaultPage": "Начальная страница",
    "LastPage": "Последняя открытая страница",
    "RememberOpenedTabs": "Восстановление открытых вкладок",
    "OpenExternallyCalledFolderInNewTab": "Папки из других приложений в новой вкладке",
    "AlwaysShowTabBar": "Постоянное отображение панели вкладок",
    "ShowCloseButtonOnTabs": "Кнопки закрытия на вкладках",
    "EditableUrl": "Редактируемая строка адреса",
    "UrlCompletionMode": "Автодополнение адреса",
    "HomeUrl": "Начальная папка",
    "SplitView": "Две области просмотра",
    "FilterBar": "Панель фильтра файлов",
    "BrowseThroughArchives": "Просмотр архивов как папок",
    "ConfirmClosingMultipleTabs": "Подтверждение закрытия нескольких вкладок",
    "RenameInline": "Переименование в списке файлов",
    "ShowToolTips": "Всплывающие сведения об элементах",
    "SortRole": "Поле сортировки",
    "SortOrder": "Порядок сортировки",
    "SortingChoice": "Способ сортировки",
    "ViewMode": "Режим просмотра файлов",
    "IconSize": "Размер значков",
    "PreviewSize": "Размер миниатюр",
    "TextWidthIndex": "Ширина подписей значков",
    "UseSystemFont": "Использование системного шрифта",
    "WindowState": "Сохранённое состояние окна",
    "State": "Сохранённое состояние",
    "MenuBar": "Отображение строки меню",
    "TabBarPosition": "Расположение панели вкладок",
    "TabBarVisibility": "Видимость панели вкладок",
    "TabBarUseUserStyleSheet": "Собственный стиль панели вкладок",
    "SearchBarPosition": "Расположение строки поиска",
    "AllowMenuAccelerators": "Доступ к меню сочетаниями клавиш",
    "RemoveWindowTitleBarAndFrame": "Скрытие заголовка и рамки окна",
    "ShowWindowTitleOnTitleBar": "Заголовок терминала в заголовке окна",
    "historyBehavior": "Поведение истории поиска",
    "HistoryBehavior": "Поведение истории поиска",
    "ActivateWhenTypingOnDesktop": "Открытие поиска при вводе на рабочем столе",
    "MaxRecentResults": "Число недавних результатов поиска",
    "PrioritizeLocalResults": "Приоритет локальных результатов поиска",
    "FocusStealingPreventionLevel": "Защита от перехвата фокуса",
    "Placement": "Размещение новых окон",
    "BorderlessMaximizedWindows": "Максимизированные окна без рамок",
    "ElectricBorderTiling": "Размещение окон у краёв экрана",
    "ElectricBorderMaximize": "Разворачивание окна у верхнего края",
    "Enabled": "Включение возможности",
    "enabled": "Включение возможности",
    "Version": "Версия формата настроек",
    "version": "Версия формата настроек",
    "Name": "Название",
    "name": "Название",
}

COLOR_AREAS = {
    "Window": "Окна",
    "Button": "Кнопки",
    "Selection": "Выделение",
    "Tooltip": "Подсказки",
    "View": "Области просмотра",
    "Complementary": "Дополнительные области",
    "Header": "Заголовки",
}
COLOR_TITLES = {
    "BackgroundNormal": "Основной фон",
    "BackgroundAlternate": "Чередующийся фон",
    "ForegroundNormal": "Основной текст",
    "ForegroundInactive": "Второстепенный текст",
    "ForegroundActive": "Активный текст",
    "ForegroundLink": "Ссылки",
    "ForegroundVisited": "Посещённые ссылки",
    "ForegroundNegative": "Ошибки",
    "ForegroundNeutral": "Предупреждения",
    "ForegroundPositive": "Успех",
    "DecorationFocus": "Рамка фокуса",
    "DecorationHover": "Подсветка при наведении",
}


def title_for(group: str, key: str, fallback: str = "") -> str:
    """Техническое имя сохраняется отдельно и не становится выдуманным переводом."""
    if group == "KDE Control Module Restrictions":
        module = key.removesuffix(".desktop")
        if module in MODULE_TITLES:
            title = MODULE_TITLES[module]
            return title if re.search(r"[А-Яа-яЁё]", title) else "Настройки " + title
        if fallback and fallback not in (key, module):
            return (
                fallback
                if re.search(r"[А-Яа-яЁё]", fallback)
                else "Настройки " + fallback
            )
    if group.startswith("Colors:") and key in COLOR_TITLES:
        area = COLOR_AREAS.get(group.partition(":")[2], "Дополнительные цвета")
        return area + ": " + COLOR_TITLES[key].lower()
    if re.search(r"[А-Яа-яЁё]", fallback):
        return fallback
    if key in COMMON_TITLES:
        return COMMON_TITLES[key]
    titles = {values[key][0] for values in SECTIONS.values() if key in values}
    translated = {title for title in titles if re.search(r"[А-Яа-яЁё]", title)}
    if len(translated) == 1:
        return translated.pop()
    return (
        "Дополнительный модуль настроек"
        if group == "KDE Control Module Restrictions"
        else "Дополнительный параметр"
    )


def group_title(group: str) -> str:
    """Подпись группы для фильтра; исходное имя остаётся в подробностях."""
    known = SECTION_NAMES.get(group, SECTION_NAMES.get(group + "][$i", ""))
    if known:
        return re.sub(r"^[^A-Za-zА-Яа-яЁё]+", "", known)
    return {
        "Windows": "Поведение окон",
        "KonsoleWindow": "Окно терминала",
        "Theme": "Тема рабочего стола",
        "Toolbar style": "Панели инструментов",
        "KFileDialog Settings": "Диалог выбора файлов",
        "Notification Messages": "Сообщения и предупреждения",
        "TabBar": "Панель вкладок",
        "SearchSettings": "Поиск в терминале",
    }.get(group, "Дополнительная группа · " + group)


# Русские Name[ru] из официальных метаданных KDE; идентификаторы на диске не меняются.
MODULE_TITLES: dict[str, str] = {
    "accentColorService": "Служба цвета выделения",
    "fontthumbnail": "Файлы шрифтов",
    "kcm_access": "Специальные возможности",
    "kcm_activities": "Комнаты",
    "kcm_animations": "Анимация",
    "kcm_autostart": "Автозапуск",
    "kcm_baloofile": "Поиск файлов",
    "kcm_clock": "Дата и время",
    "kcm_colors": "Цвета",
    "kcm_componentchooser": "Приложения по умолчанию",
    "kcm_cursortheme": "Курсоры",
    "kcm_desktoppaths": "Пути",
    "kcm_desktoptheme": "Оформление рабочего стола",
    "kcm_feedback": "Обратная связь",
    "kcm_filetypes": "Привязки файлов",
    "kcm_fontinst": "Управление шрифтами",
    "kcm_fonts": "Шрифты",
    "kcm_gamecontroller": "Игровой контроллер",
    "kcm_icons": "Значки",
    "kcm_kded": "Управление службами",
    "kcm_keyboard": "Клавиатура",
    "kcm_keys": "Комбинации клавиш",
    "kcm_krunnersettings": "Параметры KRunner",
    "kcm_kscreen": "Настройка экранов",
    "kcm_kwin_effects": "Эффекты рабочего стола",
    "kcm_kwin_scripts": "Сценарии KWin",
    "kcm_kwin_virtualdesktops": "Виртуальные рабочие столы",
    "kcm_kwindecoration": "Оформление окон",
    "kcm_kwinoptions": "Поведение окон",
    "kcm_kwinrules": "Особые параметры окон",
    "kcm_kwinscreenedges": "Края экрана",
    "kcm_kwintabbox": "Переключение окон",
    "kcm_kwintouchscreen": "Поддержка жестов для сенсорных экранов",
    "kcm_kwinxwayland": "Поддержка устаревших приложений X11",
    "kcm_landingpage": "Быстрая настройка",
    "kcm_lookandfeel": "Оформление рабочей среды",
    "kcm_mobile_power": "Энергопотребление",
    "kcm_mouse": "Мышь",
    "kcm_nightlight": "Ночная цветовая схема",
    "kcm_nighttime": "Смена дня и ночи",
    "kcm_notifications": "Уведомления",
    "kcm_plasmasearch": "Поиск Plasma",
    "kcm_powerdevilprofilesconfig": "Управление питанием",
    "kcm_qtquicksettings": "Отрисовка рабочего стола Plasma",
    "kcm_recentFiles": "Последние файлы",
    "kcm_regionandlang": "Регион и язык",
    "kcm_smserver": "Управление сеансами",
    "kcm_solid_actions": "Действия для устройств",
    "kcm_soundtheme": "Системные звуки",
    "kcm_spellchecking": "Проверка орфографии",
    "kcm_splashscreen": "Заставка",
    "kcm_style": "Оформление приложений",
    "kcm_tablet": "Планшет для рисования",
    "kcm_touchpad": "Сенсорная панель",
    "kcm_touchscreen": "Сенсорный экран",
    "kcm_users": "Пользователи",
    "kcm_virtualkeyboard": "Виртуальная клавиатура",
    "kcm_wallpaper": "Обои",
    "kcm_workspace": "Основные параметры",
    "kfontviewpart": "Просмотр шрифтов",
    "lookandfeelautoswitcher": "Автоматическое переключение оформления рабочей среды",
}
