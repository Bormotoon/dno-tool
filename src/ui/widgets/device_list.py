"""Виджет списка устройств с категориями, контекстным меню и мониторингом состояния."""

import math
from PySide6.QtGui import (
    QStandardItem,
    QStandardItemModel,
    QIcon,
    QAction,
    QPixmap,
    QPainter,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import (
    QTimer,
    Qt,
    Signal,
    QSize,
    QModelIndex,
    QPoint,
    QSortFilterProxyModel,
    QByteArray,
    QRectF,
    QEvent,
)
from PySide6.QtWidgets import (
    QListView,
    QMenu,
    QScrollArea,
    QVBoxLayout,
    QLabel,
    QWidget,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QAbstractScrollArea,
    QToolTip,
)
from PySide6.QtGui import QCursor
from typing import Optional

QWIDGETSIZE_MAX = 16777215

# Подменю «Управление»: функции через SSH/UDP/DBus, Veyon не нужен.
# Порядок групп как на панели Veyon: блокировка, сеанс, питание.
# Общее для одиночного и группового контекстного меню.
CONTROL_FEATURE_GROUPS = (
    (
        ("ssh_lock", "veyon_lock", "Заблокировать экран"),
        ("ssh_unlock", "veyon_unlock", "Разблокировать экран"),
        ("ssh_lock_input", "veyon_lock_input", "Заблокировать ввод"),
        ("ssh_unlock_input", "veyon_unlock_input", "Разблокировать ввод"),
    ),
    (
        ("login_ssh", "veyon_login", "Вход в систему"),
        ("logoff", "veyon_logoff", "Завершить сеанс пользователя"),
    ),
    (
        ("power_on", "veyon_power_on", "Включить (Wake-on-LAN)"),
        ("reboot", "veyon_reboot", "Перезагрузить"),
        ("powerdown", "veyon_powerdown", "Выключить питание"),
    ),
)
from src.config import ICONS, config
from src.config.settings import DEFAULT_VNC_SERVER
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.domain.models.device import DeviceModel
from src.domain.device_identity import device_identity, device_key
from src.utils.text.natural_sort import natural_key

import os
import subprocess
from src.logger import logger
from src.data import datastore
from src.workers.network import get_host_ping_timer_manager
from src.workers.veyon import get_thumbnail_timer_manager
from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus
from src.services.hosts.host_info_service import get_host_info_service
from src.ui.widgets.device_tree import LOCAL_NODE_IID


# Кэш оформительских слоёв рамки миниатюр: SVG-оверлей (рамка поверх
# скриншота) и картинка-подложка (под скриншотом) из папки активной темы
# иконок (theme.json -> thumbnail.overlay/underlay). Ключ кэша - путь файла,
# поэтому смена темы подхватывается автоматически; reset_thumb_renderer -
# явный сброс (live-переключение темы).
_GLOBAL_THUMB_RENDERER: Optional[QSvgRenderer] = None
_GLOBAL_THUMB_RENDERER_PATH: str = ""
_GLOBAL_THUMB_UNDERLAY: Optional[QPixmap] = None
_GLOBAL_THUMB_UNDERLAY_PATH: str = ""


def reset_thumb_renderer() -> None:
    """Сбросить кэш рамки/подложки миниатюр (смена темы иконок)."""
    global _GLOBAL_THUMB_RENDERER, _GLOBAL_THUMB_RENDERER_PATH
    global _GLOBAL_THUMB_UNDERLAY, _GLOBAL_THUMB_UNDERLAY_PATH
    _GLOBAL_THUMB_RENDERER = None
    _GLOBAL_THUMB_RENDERER_PATH = ""
    _GLOBAL_THUMB_UNDERLAY = None
    _GLOBAL_THUMB_UNDERLAY_PATH = ""


def _resolve_theme_file(filename: str) -> str:
    """Путь к файлу в папке активной темы иконок (fallback - flat).

    Оставлен для совместимости: zip-темы отдают байты через
    ``_resolve_theme_bytes``, путь нужен лишь как fallback и ключ кэша.
    """
    from src.config import get_icon_theme, themed_icon_path

    if not filename:
        return ""
    candidate = themed_icon_path(filename, get_icon_theme())
    if candidate and os.path.exists(candidate):
        return candidate
    return ""


def _resolve_theme_bytes(filename: str) -> bytes | None:
    """Байты файла из zip активной темы (fallback - flat, затем папка)."""
    from src.config import get_themed_icon_bytes

    if not filename:
        return None
    try:
        return get_themed_icon_bytes(filename)
    except Exception:  # noqa: BLE001
        return None


def _get_thumb_renderer() -> Optional[QSvgRenderer]:
    """Вернуть кэшированный QSvgRenderer оверлея рамки (создаёт при первом вызове)."""
    from src.config import get_icon_theme, get_thumbnail_layout
    from PySide6.QtCore import QByteArray

    overlay = get_thumbnail_layout().get("overlay") or ""
    if not overlay:
        return None
    # Ключ кэша - тема+имя (один и тот же файл из zip или папки).
    cache_key = f"zip:{get_icon_theme()}/{overlay}"
    global _GLOBAL_THUMB_RENDERER, _GLOBAL_THUMB_RENDERER_PATH
    if (
        _GLOBAL_THUMB_RENDERER is not None
        and _GLOBAL_THUMB_RENDERER.isValid()
        and _GLOBAL_THUMB_RENDERER_PATH == cache_key
    ):
        return _GLOBAL_THUMB_RENDERER
    try:
        data = _resolve_theme_bytes(overlay)
        if data:
            renderer = QSvgRenderer(QByteArray(data))
            template_path = cache_key
        else:
            template_path = _resolve_theme_file(overlay)
            if not template_path:
                return None
            renderer = QSvgRenderer(template_path)
        if not renderer.isValid():
            logger.warning(f"Thumb template invalid SVG: {template_path}")
            return None
        _GLOBAL_THUMB_RENDERER = renderer
        _GLOBAL_THUMB_RENDERER_PATH = template_path
        return renderer
    except Exception as e:
        logger.error(f"Failed to load thumb template: {e}")
        return None


def _get_thumb_underlay() -> Optional[QPixmap]:
    """Картинка-подложка миниатюры из папки темы (png/jpg/svg), под скриншотом."""
    from src.config import get_icon_theme, get_thumbnail_layout

    underlay = get_thumbnail_layout().get("underlay") or ""
    if not underlay:
        return None
    cache_key = f"zip:{get_icon_theme()}/{underlay}"
    global _GLOBAL_THUMB_UNDERLAY, _GLOBAL_THUMB_UNDERLAY_PATH
    if (
        _GLOBAL_THUMB_UNDERLAY is not None
        and not _GLOBAL_THUMB_UNDERLAY.isNull()
        and _GLOBAL_THUMB_UNDERLAY_PATH == cache_key
    ):
        return _GLOBAL_THUMB_UNDERLAY
    try:
        pixmap = QPixmap()
        data = _resolve_theme_bytes(underlay)
        if data:
            pixmap.loadFromData(data)
            underlay_path = cache_key
        else:
            underlay_path = _resolve_theme_file(underlay)
            if not underlay_path:
                return None
            pixmap = QPixmap(underlay_path)
        if pixmap.isNull():
            logger.warning(f"Thumb underlay unreadable image: {underlay_path}")
            return None
        _GLOBAL_THUMB_UNDERLAY = pixmap
        _GLOBAL_THUMB_UNDERLAY_PATH = underlay_path
        return pixmap
    except Exception as e:
        logger.error(f"Failed to load thumb underlay: {e}")
        return None


def _compose_thumbnail(pixmap: QPixmap, target_w: int, target_h: int) -> QPixmap:
    """Собрать составную миниатюру согласно настройке thumbnail_style.

    - 'monitor': 16:9 картинка вписывается в экран SVG-шаблона монитора (рамка).
    - 'plain' (default fallback): голая 16:9 картинка на прозрачном квадрате, по центру.
    """
    style = config.app.thumbnails.style
    if style == "plain":
        return _compose_plain_thumbnail(pixmap, target_w, target_h)
    return _compose_monitor_thumbnail(pixmap, target_w, target_h)


def _compose_plain_thumbnail(pixmap: QPixmap, target_w: int, target_h: int) -> QPixmap:
    """Голая 16:9 миниатюра на прозрачном квадрате, центрированная по вертикали."""
    thumb_h = int(target_w * 9 / 16)
    if thumb_h > target_h:
        thumb_h = target_h
    scaled = pixmap.scaled(
        QSize(target_w, thumb_h),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    result = QPixmap(target_w, target_h)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    y = (target_h - scaled.height()) // 2
    painter.drawPixmap(0, y, scaled)
    painter.end()
    return result


def _compose_monitor_thumbnail(
    pixmap: QPixmap, target_w: int, target_h: int
) -> QPixmap:
    """Скриншот в рамке из папки активной темы иконок (theme.json -> thumbnail).

    Геометрия берётся из свойств темы: viewBox оверлея и отверстие экрана
    (hole) в его координатах SVG. Порядок слоёв: подложка-подложка (underlay,
    картинка из папки темы) -> скриншот, обрезанный по отверстию -> оверлей
    рамки (overlay) поверх. У тем без theme.json - встроенная геометрия
    старого шаблона и оверлей thumb_template.svg (поведение как раньше).

    QSvgRenderer.render по умолчанию сохраняет aspect ratio viewBox, из-за
    чего остаются поля; рендерим с IgnoreAspectRatio, а координаты отверстия
    пересчитываем через раздельные scale_x/scale_y.
    """
    from src.config import get_thumbnail_layout

    layout = get_thumbnail_layout()
    vb_x, vb_y, vb_w, vb_h = layout["viewBox"]
    hole_x0, hole_y0, hole_w0, hole_h0 = layout["hole"]

    # Растягиваем viewBox в квадрат target×target (IgnoreAspectRatio):
    # svg_coord → target: (svg_coord - VB) * scale
    scale_x = target_w / vb_w
    scale_y = target_h / vb_h
    hole = QRectF(
        (hole_x0 - vb_x) * scale_x,
        (hole_y0 - vb_y) * scale_y,
        hole_w0 * scale_x,
        hole_h0 * scale_y,
    )

    result = QPixmap(target_w, target_h)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Слой 1: подложка темы (картинка) - под скриншотом.
    underlay = _get_thumb_underlay()
    if underlay is not None:
        painter.drawPixmap(0, 0, target_w, target_h, underlay)

    # Слой 2: скриншот в отверстие (заполняет целиком, лишнее обрезается).
    scaled = pixmap.scaled(
        QSize(int(hole.width()), int(hole.height())),
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter.save()
    painter.setClipRect(hole)
    tx = int(hole.x() + (hole.width() - scaled.width()) / 2)
    ty = int(hole.y() + (hole.height() - scaled.height()) / 2)
    painter.drawPixmap(tx, ty, scaled)
    painter.restore()

    # Слой 3: рамка темы поверх. Растягиваем viewBox в квадрат без полей
    # (IgnoreAspectRatio), как QIcon растягивает online/offline.
    renderer = _get_thumb_renderer()
    if renderer is not None:
        renderer.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
        renderer.render(painter, QRectF(0, 0, target_w, target_h))
    painter.end()
    return result


class ClickableHeader(QFrame):
    """Заголовок группы, излучающий сигнал clicked при клике мышью."""

    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class OnlineFilterProxyModel(QSortFilterProxyModel):
    """Прокси-модель для фильтрации устройств по статусу онлайн."""

    def __init__(self, online_only: bool = False, parent=None):
        super().__init__(parent)
        self._online_only = online_only

    def set_online_only(self, value: bool) -> None:
        self._online_only = value
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._online_only:
            return True
        item = self.sourceModel().item(source_row)
        if item is None or not hasattr(item, "device"):
            return True
        return bool(item.device.is_online)


class CustomListItem(QStandardItem):
    """
    Кастомный элемент списка с поддержкой пинга устройства.

    Пинг управляется централизованно через HostPingTimerManager.
    """

    def __init__(self, text: str = "", device: DeviceModel = None, tree_item=None):
        """
        Инициализация элемента списка

        Args:
            text: Отображаемый текст
            device: Объект устройства
            tree_item: Связанный элемент дерева
        """
        icon = qicon_from_path(device.icon)
        super().__init__(qicon_from_path(device.icon), text)
        self.device = device
        self.tree_item = tree_item
        self._original_icon = device.icon  # Сохраняем путь к стандартной иконке
        self._thumbnail_pixmap = None  # Миниатюра экрана (QPixmap или None)
        self._thumbnail_raw_pixmap = None
        self._thumbnail_error = ""
        self._thumbnail_model_signature = self._model_signature(device)
        self._thumbnail_capture_revision = None
        self._thumbnail_active = False
        self._veyon_down = False  # Legacy flag for the neutral unavailable-source icon.

        if isinstance(icon, QIcon):
            self.setIcon(icon)
        else:
            self.setIcon(qicon_from_path(self.device.icon))

        self.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

        # Тултип собирается HostInfoService (статика + поля из кэша).
        # Живые данные запрашиваются при наведении — DeviceListView.event().

    @staticmethod
    def _model_signature(device: DeviceModel) -> tuple:
        return (
            device.host,
            device.vnc_port,
            device.thumbnail_backend,
            getattr(device, "node_iid", None),
        )

    def thumbnail_tooltip(self, text: str) -> str:
        """Append backend diagnostics without replacing ordinary host information."""
        return f"{text}\n{self._thumbnail_error}" if self._thumbnail_error else text

    def update_online_icon(self, device: DeviceModel, is_online: bool):
        """Обновление иконки при изменении статуса онлайн (вызывается из DeviceListView)"""
        # Связь по iid (первичный), fallback по host (для данных без iid)
        if self.device:
            owner = getattr(self.device, "node_iid", None) or LOCAL_NODE_IID
            source_owner = getattr(device, "node_iid", None) or LOCAL_NODE_IID
            iid = getattr(self.device, "iid", None)
            source_iid = getattr(device, "iid", None)
            same = owner == source_owner and (
                iid == source_iid
                if iid and source_iid
                else self.device.host == device.host
            )
            if same:
                self._original_icon = device.icon
                if not is_online and (
                    owner == LOCAL_NODE_IID and config.app.thumbnails.require_online
                ):
                    self.clear_thumbnail()
                    return
                # Keep the neutral source indicator until capture succeeds.
                if self._veyon_down and is_online:
                    return
                if not is_online:
                    self._veyon_down = False
                if self._thumbnail_pixmap is None:
                    try:
                        self.setIcon(qicon_from_path(device.icon))
                    except (RuntimeError, AttributeError):
                        pass

    def set_thumbnail(self, pixmap: QPixmap, raw_pixmap: QPixmap = None):
        """Установить миниатюру экрана вместо стандартной иконки."""
        self._thumbnail_pixmap = pixmap
        self._veyon_down = False
        self._thumbnail_error = ""
        if raw_pixmap is not None:
            self._thumbnail_raw_pixmap = raw_pixmap
        try:
            self.setIcon(QIcon(pixmap))
            if self.model() is not None:
                idx = self.index()
                if idx.isValid():
                    self.model().dataChanged.emit(
                        idx, idx, [Qt.ItemDataRole.DecorationRole]
                    )
        except (RuntimeError, AttributeError):
            pass

    def re_render_thumbnail(self, icon_size: QSize):
        """Перерисовать сохранённую миниатюру под новый размер иконки (без сетевого запроса).

        Использует общую модульную _compose_thumbnail, чтобы рамка монитора сохранялась
        при изменении размера иконок (как и в _apply_thumbnail).
        """
        if self._thumbnail_raw_pixmap is None:
            return
        try:
            target_w = icon_size.width()
            target_h = icon_size.height()
            result = _compose_thumbnail(self._thumbnail_raw_pixmap, target_w, target_h)
            self._thumbnail_pixmap = result
            self.setIcon(QIcon(result))
            if self.model() is not None:
                idx = self.index()
                if idx.isValid():
                    self.model().dataChanged.emit(
                        idx, idx, [Qt.ItemDataRole.DecorationRole]
                    )
        except (RuntimeError, AttributeError):
            pass

    def set_veyon_down(self):
        """Compatibility entry for legacy node responses."""
        self.set_thumbnail_error("veyon", "unavailable")

    def set_thumbnail_error(self, backend: str, status: str) -> None:
        """Mark the selected source unavailable, never mark the host offline."""
        messages = {
            "unavailable": "источник недоступен",
            "auth_failed": "ошибка аутентификации",
            "timeout": "таймаут получения кадра",
            "protocol_error": "ошибка протокола",
            "unsupported": "неподдерживаемый протокол или источник",
        }
        self._thumbnail_error = (
            f"Миниатюры {backend.upper() if backend == 'vnc' else 'Veyon'}: "
            f"{messages.get(str(status), 'кадр недоступен')}"
        )
        self._veyon_down = self.device.is_online
        self._thumbnail_pixmap = None
        self._thumbnail_raw_pixmap = None
        try:
            self.setIcon(
                _icon_from_key("veyon_down")
                if self.device.is_online
                else qicon_from_path(self._original_icon)
            )
            if self.model() is not None:
                idx = self.index()
                if idx.isValid():
                    self.model().dataChanged.emit(
                        idx, idx, [Qt.ItemDataRole.DecorationRole]
                    )
        except (RuntimeError, AttributeError):
            pass

    def clear_thumbnail(self):
        """Вернуть стандартную иконку, убрав миниатюру."""
        self._thumbnail_pixmap = None
        self._thumbnail_raw_pixmap = None
        self._veyon_down = False
        self._thumbnail_error = ""
        try:
            self.setIcon(qicon_from_path(self._original_icon))
            if self.model() is not None:
                idx = self.index()
                if idx.isValid():
                    self.model().dataChanged.emit(
                        idx, idx, [Qt.ItemDataRole.DecorationRole]
                    )
        except (RuntimeError, AttributeError):
            pass

    def cleanup(self):
        """Очистка ресурсов перед удалением"""
        self._thumbnail_active = False
        if self.device and self.device.iid:
            niid = getattr(self.device, "node_iid", None)
            if niid and niid != LOCAL_NODE_IID:
                # Node-хост удалённой ноды: стоп статус-таймера (зеркало
                # локального stop_ping) и подписки миниатюр.
                try:
                    from src.workers.node.status import get_node_status_manager

                    get_node_status_manager().stop_ping(self.device.iid, niid)
                except Exception as e:
                    logger.debug(f"CustomListItem.cleanup: node status stop: {e}")
                try:
                    from src.workers.node.thumbnails import (
                        get_node_thumbnail_manager,
                    )

                    get_node_thumbnail_manager().stop_thumbnail(self.device.iid, self)
                except Exception:
                    pass
                return
            # Локальный хост (в т.ч. node_iid == LOCAL_NODE_IID при
            # включённом показе нод) — обычные локальные таймеры.
            get_host_ping_timer_manager().stop_ping(self.device.iid)
            get_thumbnail_timer_manager().stop_thumbnail(self.device.iid, self)

    def open_terminal(self):
        """Открывает новое окно терминала с SSH подключением (Windows и Linux)"""
        import platform

        try:
            system = platform.system().lower()
            if system == "windows":
                import shutil

                from src.utils.system.subprocess_env import _ENV_LEAKS

                ssh_cmd = _build_ssh_command_string(self.device)
                # Попробовать Windows Terminal (wt.exe), иначе cmd
                wt_path = shutil.which("wt")
                logger.info(f"Открытие терминала Windows: wt={wt_path!r} cmd={ssh_cmd}")
                # Frozen-бинарь (console=False) наследует _MEIPASS/PYTHON*/QT*-утечки.
                # Чистим только их, сохраняя SystemRoot/PATH/PATHEXT для поиска DLL.
                env = dict(os.environ)
                for _key in _ENV_LEAKS:
                    env.pop(_key, None)
                creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                if wt_path:
                    proc = subprocess.Popen(
                        ["wt", "cmd", "/k", ssh_cmd],
                        env=env,
                        creationflags=creationflags,
                    )
                else:
                    proc = subprocess.Popen(
                        ["cmd", "/c", f"start cmd /k {ssh_cmd}"],
                        env=env,
                        creationflags=creationflags,
                    )
                logger.info(f"Терминал запущен pid={proc.pid}")
            else:
                import shlex

                from src.utils.system.subprocess_env import (
                    build_clean_env,
                    safe_cwd_for_user,
                    wrap_as_user,
                )

                argv = build_terminal_argv(self.device)
                logger.info(f"Открытие терминала: {shlex.join(argv)}")
                # build_clean_env убирает LD_LIBRARY_PATH/_MEIPASS/QT_*-утечки
                # frozen-сборки, иначе системный эмулятор грузит бандловый
                # libstdc++.so.6 и падает (GLIBCXX_3.4.x not found).
                # wrap_as_user — запуск от исходного пользователя при pkexec-root.
                # cwd - HOME пользователя: runuser не меняет cwd, а Konsole
                # делает chdir до exec и падает с Permission denied на /root.
                run_cmd = wrap_as_user(argv)
                cwd = safe_cwd_for_user()
                try:
                    euid = os.geteuid()
                except AttributeError:
                    euid = -1
                logger.info(
                    f"Терминал: euid={euid} cwd={cwd} cmd={shlex.join(run_cmd)}"
                )
                proc = subprocess.Popen(run_cmd, env=build_clean_env(), cwd=cwd)
                logger.info(f"Терминал запущен pid={proc.pid}: {shlex.join(run_cmd)}")
        except Exception as e:
            logger.error(f"Ошибка открытия терминала: {e}", exc_info=True)
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, "Ошибка", f"Не удалось открыть терминал:\n{e}")

    def open_sftp(self):
        """Opens SFTP dialog for file transfer"""
        from src.ui.dialogs.tools.sftp import SFTPDialog

        dialog = SFTPDialog(self.device, None)
        dialog.exec()


# Матрица системных эмуляторов терминала Linux: (binary, exec_flag).
# exec_flag отделяет команду от опций самого эмулятора (сверено с исходниками
# upstream каждого эмулятора):
#   - "-e" с ПРИЁМОМ остатка командной строки: konsole, lxterminal, xterm
#     (и x-terminal-emulator, если alternatives указывает на них);
#   - gnome-terminal: только "--" (или сломанный -x, пре-скан в
#     terminal-options.cc собирает argv после "--");
#   - mate-terminal: "--" (та же схема пре-скана, что и у gnome-terminal;
#     -e/--command принимает РОВНО один аргумент через g_shell_parse_argv —
#     составная команда `bash -c '...'` с ним не работает);
#   - xfce4-terminal: только "-x/--execute" (remainder of the command line,
#     g_strdupv(argv+n)); "-e/--command" принимает один аргумент, а хвост
#     после него уходит в unknown_option → терминал не открывается вовсе.
_LINUX_TERMINALS = (
    ("konsole", "-e"),
    ("gnome-terminal", "--"),
    ("x-terminal-emulator", "-e"),
    ("xfce4-terminal", "-x"),
    ("mate-terminal", "--"),
    ("lxterminal", "-e"),
    ("xterm", "-e"),
)


def _build_ssh_command_string(device: DeviceModel) -> str:
    """Собирает однострочную SSH-команду для запуска в терминале Windows."""
    from src.workers.command.executor_base import (
        get_credentials,
        get_default_key_path,
    )

    hostname = device.host
    creds = get_credentials(device)
    port = device.port or config.app.ssh.port
    strict = "yes" if config.app.ssh.strict_host_checking else "no"
    ssh_cmd = (
        f"ssh -o StrictHostKeyChecking={strict} -p {port} {creds.username}@{hostname}"
    )

    key_path = get_default_key_path()
    if creds.private_key and os.path.exists(key_path):
        ssh_cmd += f' -i "{key_path}"'
    return ssh_cmd


def build_terminal_argv(device: DeviceModel) -> list:
    """Собирает argv запуска системного терминала Linux с SSH-сессией.

    Окно НЕ должно исчезать при мгновенной ошибке ssh (connection refused,
    обрыв, неверный ключ): раньше `konsole -e ssh ...` давал вспышку ~1 сек,
    неотличимую от «терминал не запускается», без какой-либо диагностики.
    Поэтому ssh выполняется через `bash -c` обёртку: при ненулевом коде
    выхода текст ошибки остаётся в окне + пауза «Нажмите Enter...».
    При чистом выходе из сессии (exit, код 0) окно закрывается само.

    Raises:
        RuntimeError: ни один известный эмулятор не найден в PATH.
    """
    import shlex
    import shutil
    from src.workers.command.executor_base import (
        get_credentials,
        get_default_key_path,
    )

    hostname = device.host
    creds = get_credentials(device)
    port = device.port or config.app.ssh.port
    strict = "yes" if config.app.ssh.strict_host_checking else "no"
    ssh_args = [
        "ssh",
        "-o",
        f"StrictHostKeyChecking={strict}",
        "-p",
        str(port),
        f"{creds.username}@{hostname}",
    ]

    key_path = get_default_key_path()
    if creds.private_key and os.path.exists(key_path):
        ssh_args += ["-i", key_path]

    # Пауза только при ошибке: успешный exit закрывает окно как раньше.
    # Пароль в команду не подставляется (интерактивный ввод в окне).
    wrapper = (
        f"{shlex.join(ssh_args)}; rc=$?; "
        'if [ $rc -ne 0 ]; then echo ""; '
        'echo "[dno-tool] SSH завершился с кодом $rc"; '
        'read -r -p "Нажмите Enter, чтобы закрыть окно..." _; fi'
    )

    for binary, exec_flag in _LINUX_TERMINALS:
        if shutil.which(binary):
            return [binary, exec_flag, "bash", "-c", wrapper]
    raise RuntimeError("No supported terminal emulator found.")


class DynamicHeightListView(QListView):
    """QListView с динамической высотой на основе содержимого"""

    # Нажатие мыши до обработки выделения Qt: (вид, сохранить выделение
    # в остальных категориях). Каждая категория — отдельный вид, поэтому
    # обычный клик в одной категории снимает выделение в других.
    selectionPressed = Signal(object, bool)

    def __init__(self, parent=None, icon_size: QSize = QSize(64, 64), spacing: int = 4):
        super().__init__(parent)
        self._icon_size = icon_size
        self._spacing = spacing
        self._grid_size = None
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def setGridSize(self, size: QSize):
        """Переопределяем для отслеживания размера сетки"""
        super().setGridSize(size)
        self._grid_size = size
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        """Возвращает оптимальный размер на основе содержимого"""
        if self.model() is None or self._grid_size is None:
            return QSize(100, 100)

        count = self.model().rowCount()
        if count == 0:
            return QSize(100, self._grid_size.height())

        # Получаем доступную ширину от родителя
        viewport_width = self.parent().width() if self.parent() else 400
        if viewport_width <= 0:
            viewport_width = 400

        # Вычисляем количество колонок
        item_width = self._grid_size.width()
        columns = max(1, viewport_width // item_width)

        # Вычисляем количество строк
        rows = (count + columns - 1) // columns

        # Вычисляем высоту
        item_height = self._grid_size.height()
        total_height = rows * item_height + self._spacing * 2

        return QSize(viewport_width, total_height)

    def minimumSizeHint(self) -> QSize:
        """Минимальный размер - одна строка"""
        if self._grid_size is None:
            return QSize(100, 100)
        return QSize(
            self._grid_size.width(), self._grid_size.height() + self._spacing * 2
        )

    def resizeEvent(self, event):
        """При изменении размера пересчитываем высоту"""
        super().resizeEvent(event)
        # Обновляем геометрию для пересчета sizeHint
        self.updateGeometry()

    def mousePressEvent(self, event):
        """Сообщить о нажатии до того, как Qt изменит выделение.

        Выделение в других категориях сохраняется при Ctrl/Shift и при ПКМ
        по уже выделенному хосту: Qt тогда выделение не трогает, и групповое
        меню должно получить всю группу.
        """
        modifiers = event.modifiers()
        keep = bool(
            modifiers
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
            )
        )
        if not keep and event.button() == Qt.MouseButton.RightButton:
            index = self.indexAt(event.position().toPoint())
            sel = self.selectionModel()
            keep = bool(index.isValid() and sel is not None and sel.isSelected(index))
        self.selectionPressed.emit(self, keep)
        super().mousePressEvent(event)

    def event(self, ev):
        """Перехват ToolTip: живой тултип из HostInfoService.

        При наведении на устройство тултип собирается из кэша; для онлайн-хоста
        с протухшим/отсутствующим кэшем запускается фоновый запрос — когда
        данные придут, тултип перерисуется (если курсор ещё на этом элементе).
        Node-хосты удалённых нод обслуживаются тем же сервисом (маршрутизация
        в request_info: live-данные через ноду-владельца, не SSH).
        """
        if ev.type() == QEvent.Type.ToolTip:
            try:
                idx = self.indexAt(ev.pos())
                if idx.isValid():
                    model = self.model()
                    # Прокси (OnlineFilterProxyModel) маппит в исходную модель.
                    if model is not None and hasattr(model, "mapToSource"):
                        idx = model.mapToSource(idx)
                        model = model.sourceModel()
                    item = model.itemFromIndex(idx) if model else None
                    device = getattr(item, "device", None) if item else None
                    if device is not None:
                        svc = get_host_info_service()
                        QToolTip.showText(
                            ev.globalPos(),
                            item.thumbnail_tooltip(svc.build_tooltip(device)),
                            self,
                        )
                        svc.request_info(device)
                        return True
            except RuntimeError:
                return super().event(ev)
        return super().event(ev)


class DeviceListView(QWidget):
    itemDoubleClicked = Signal(object)  # Will emit the clicked CustomListItem
    deviceInfoRequested = Signal(object)
    commandsExecuted = Signal(DeviceModel, dict)
    customCommandRequested = Signal(DeviceModel)  # ПКМ -> Своя команда…
    clicked = Signal(QModelIndex)  # For compatibility with previous QListView API
    remoteRecordingRequested = Signal(DeviceModel)  # ПКМ -> Удалённая запись
    polkitEditorRequested = Signal(DeviceModel)  # ПКМ -> Редактор политик
    veyonAccessRequested = Signal(
        DeviceModel, str
    )  # ПКМ -> Veyon (mode: "view"|"control")
    veyonFeatureRequested = Signal(
        DeviceModel, str
    )  # ПКМ -> Veyon feature (lock/unlock/powerdown/...)
    # Групповое ПКМ по нескольким выделенным хостам (list[DeviceModel]).
    commandsExecutedForDevices = Signal(list, dict)
    customCommandRequestedForDevices = Signal(list)
    veyonFeatureRequestedForDevices = Signal(list, str)

    """Widget: sectioned icon view adapted for devices"""

    def __init__(self, parent=None, icon_size: QSize = QSize(64, 64), spacing: int = 4):
        super().__init__(parent)
        self._icon_size = icon_size
        self._spacing = spacing
        self._categories = {}  # name -> {widget, label, chevron, count_label, view, model, proxy}
        self._default_category = "Все устройства"
        self._category_order = []  # Порядок добавления категорий
        self._online_only = False
        self._thumbnail_mode = False
        self._collapsed: set[str] = set()  # свёрнутые группы (ручные + авто)
        self._auto_collapsed: set[str] = (
            set()
        )  # подмножество _collapsed, скрытое фильтром «только онлайн»

        self._setup_ui()
        get_host_ping_timer_manager().online_updated.connect(
            self._on_ping_online_updated
        )

        # Статусы хостов нод (пер-host таймеры у нод-владельцев) — тот же
        # путь отрисовки, что и локальный пинг (_on_ping_online_updated
        # маршрутизирует в NodeThumbnailManager по node_iid).
        try:
            from src.workers.node.status import get_node_status_manager

            get_node_status_manager().status_updated.connect(
                self._on_ping_online_updated
            )
        except Exception as e:
            logger.debug(f"DeviceListView: node status manager недоступен: {e}")

        # Подключаем менеджер миниатюр Veyon
        thumb_mgr = get_thumbnail_timer_manager()
        thumb_mgr.update_icon_width(self._icon_size.width())
        thumb_mgr.thumbnail_ready.connect(self._apply_thumbnail)

        # Подключаем менеджер миниатюр нод (федерация)
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            node_thumb_mgr = get_node_thumbnail_manager()
            node_thumb_mgr.thumbnail_ready.connect(self._apply_thumbnail)
        except Exception as e:
            logger.debug(f"DeviceListView: node thumbnail manager недоступен: {e}")

        # Живой тултип: при приходе данных хоста перерисовать, если курсор
        # ещё на соответствующем элементе (DeviceListView/DeviceTreeView).
        get_host_info_service().info_updated.connect(self._on_host_info_updated)

    def _on_host_info_updated(self, host: str):
        """Данные хоста пришли — перерисовать тултип, если курсор на этом хосте."""
        if not host:
            return
        try:
            view = self._hovered_view()
            if view is None:
                return
            idx = view.indexAt(view.viewport().mapFromGlobal(QCursor.pos()))
            if not idx.isValid():
                return
            model = view.model()
            # Прокси (OnlineFilterProxyModel) маппит в исходную модель.
            if model is not None and hasattr(model, "mapToSource"):
                idx = model.mapToSource(idx)
                model = model.sourceModel()
            item = model.itemFromIndex(idx) if model else None
            device = getattr(item, "device", None) if item else None
            if device is None or device.host != host:
                return
            svc = get_host_info_service()
            if not svc.get_info(getattr(device, "iid", None) or "", getattr(device, "node_iid", None)):
                return
            QToolTip.showText(
                QCursor.pos(), item.thumbnail_tooltip(svc.build_tooltip(device)), view
            )
        except RuntimeError:
            pass
        except Exception as e:
            logger.debug(f"DeviceListView: tooltip refresh failed: {e}")

    def _hovered_view(self):
        """Вернуть QListView под курсором мыши (среди секций), иначе None."""
        for info in self._categories.values():
            view = info.get("view")
            if view is not None and view.isVisible() and view.underMouse():
                return view
        return None

    def _on_ping_online_updated(self, device: DeviceModel, is_online: bool):
        """Обновление иконок элементов списка при изменении статуса пинга"""
        for name, info in self._categories.items():
            model = info["model"]
            proxy = info["proxy"]
            for i in range(model.rowCount()):
                item = model.item(i)
                if hasattr(item, "update_online_icon"):
                    item.update_online_icon(device, is_online)
            if self._online_only:
                proxy.invalidateFilter()
            # Обновляем счётчик «онлайн/всего»
            self._update_header_count(name)
        if self._online_only:
            # Авто-сворачивание/разворот по изменению видимости + высоты
            for name in self._category_order:
                self._sync_auto_collapse(name)
            self._update_all_sections_height()

        # Уведомляем менеджер миниатюр об изменении статуса хоста.
        # Локальная нода (LOCAL_NODE_IID) — обычный Veyon-менеджер.
        niid = getattr(device, "node_iid", None)
        if niid and niid != LOCAL_NODE_IID:
            try:
                from src.workers.node.thumbnails import get_node_thumbnail_manager

                get_node_thumbnail_manager().on_host_online(device, is_online)
            except Exception:
                pass
        else:
            get_thumbnail_timer_manager().on_host_online(device, is_online)

    def _setup_ui(self):
        # Main layout
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Scroll area + container
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        # self._scroll.setFrameShape(QScrollArea.Shape.Box)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(4, 4, 4, 4)
        self._vbox.setSpacing(2)
        # Финальный stretch поглощает свободное место, прижимая секции кверху.
        # Категории вставляются перед ним (insertWidget в add_category).
        self._vbox.addStretch(1)

        self._scroll.setWidget(self._container)
        main.addWidget(self._scroll)

    def _calculate_section_height(
        self, view: DynamicHeightListView, item_count: int
    ) -> int:
        """Вычисляет высоту секции на основе количества элементов"""
        if item_count == 0:
            return view.minimumSizeHint().height()

        grid_size = view.gridSize()
        if not grid_size.isValid():
            pad = self._calc_pad()
            grid_size = QSize(
                self._icon_size.width() + pad, self._icon_size.height() + 20 + pad
            )

        # Получаем доступную ширину от контейнера
        viewport_width = self._container.width() - 8  # Учитываем отступы
        if viewport_width <= 0:
            viewport_width = self._scroll.viewport().width() - 8
        if viewport_width <= 0:
            viewport_width = 400

        item_width = grid_size.width()
        item_height = grid_size.height()

        # Вычисляем количество колонок
        columns = max(1, viewport_width // item_width)

        # Вычисляем количество строк
        rows = (item_count + columns - 1) // columns

        # Общая высота = строки * высота_элемента + отступы
        total_height = rows * item_height + self._spacing * 2

        return max(total_height, item_height + self._spacing * 2)

    def _count_online(self, model: QStandardItemModel) -> int:
        """Количество онлайн-устройств в модели категории."""
        count = 0
        for i in range(model.rowCount()):
            item = model.item(i)
            if (
                item
                and hasattr(item, "device")
                and item.device
                and item.device.is_online
            ):
                count += 1
        return count

    def _visible_count(self, name: str) -> int:
        """Количество видимых элементов категории (с учётом фильтра «только онлайн»)."""
        info = self._categories.get(name)
        if not info:
            return 0
        if self._online_only:
            return info["proxy"].rowCount()
        return info["model"].rowCount()

    def _update_header_count(self, name: str):
        """Обновить pill-счётчик «онлайн/всего» в заголовке категории."""
        info = self._categories.get(name)
        if not info:
            return
        model = info["model"]
        total = model.rowCount()
        online = self._count_online(model)
        info["count_label"].setText(f"{online}/{total}")

    def _update_all_header_counts(self):
        """Обновить счётчики всех категорий."""
        for name in self._category_order:
            self._update_header_count(name)

    def _collapse(self, name: str, auto: bool = False):
        """Свернуть категорию (скрыть тело). auto=True — скрытие фильтром «только онлайн»."""
        if name not in self._categories or name in self._collapsed:
            return
        self._collapsed.add(name)
        if auto:
            self._auto_collapsed.add(name)
        self._categories[name]["view"].hide()
        self._categories[name]["chevron"].setText("▸")

    def _expand(self, name: str, auto: bool = False):
        """Развернуть категорию. auto=True — развертывание авто-свёрнутой фильтром."""
        if name not in self._categories or name not in self._collapsed:
            return
        self._collapsed.discard(name)
        if auto:
            self._auto_collapsed.discard(name)
        self._categories[name]["view"].show()
        self._categories[name]["chevron"].setText("▾")
        self._update_section_height(name)

    def _toggle_collapse(self, name: str):
        """Переключить свёрнутость категории по клику на заголовке (ручное действие)."""
        if name not in self._categories:
            return
        if name in self._collapsed:
            # Ручное развертывание переопределяет авто-свёрнутость фильтра
            self._expand(name, auto=True)
        else:
            self._collapse(name, auto=False)

    def _sync_auto_collapse(self, name: str):
        """Свернуть/развернуть категорию автоматически по фильтру «только онлайн».

        Если включён «только онлайн» и видимых элементов нет — авто-свернуть.
        Если появились видимые элементы и категория была авто-свёрнута — развернуть.
        """
        if not self._online_only:
            return
        if name not in self._categories:
            return
        visible = self._visible_count(name)
        if visible == 0 and name not in self._collapsed:
            self._collapse(name, auto=True)
        elif visible > 0 and name in self._auto_collapsed:
            self._expand(name, auto=True)

    def _update_section_height(self, category_name: str):
        """Обновляет высоту конкретной секции"""
        if category_name not in self._categories:
            return

        info = self._categories[category_name]
        view = info["view"]
        model = info["model"]
        proxy = info["proxy"]

        # Свёрнутая секция не занимает высоту (view скрыт)
        if category_name in self._collapsed:
            view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            view.setFixedHeight(0)
            return

        # При фильтре «только онлайн» считаем видимые элементы, а не все
        if self._online_only:
            item_count = proxy.rowCount()
        else:
            item_count = model.rowCount()
        new_height = self._calculate_section_height(view, item_count)

        # Все секции имеют фиксированную высоту;
        # свободное место поглощается финальным stretch в _vbox.
        view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        view.setFixedHeight(new_height)

    def _update_all_sections_height(self):
        """Обновляет высоту всех секций"""
        for name in self._category_order:
            self._update_section_height(name)

    # --------- API ----------
    def add_category(self, name: str):
        """Добавляет категорию с заголовком `name`."""
        if name in self._categories:
            return  # already exists

        # Добавляем в порядок категорий
        if name not in self._category_order:
            self._category_order.append(name)

        # chevron — уголок-индикатор свёрнутости (▾ развёрнуто / ▸ свёрнуто)
        chevron = QLabel("▾")
        chevron.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        chevron.setAlignment(Qt.AlignCenter)
        chevron.setContentsMargins(2, 2, 2, 2)
        chevron.setFixedWidth(14)

        # header label — название группы
        header = QLabel(name)
        header.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        header.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setContentsMargins(2, 2, 2, 2)

        # count label — pill-бейдж «онлайн/всего»
        count_label = QLabel("0/0")
        count_label.setAlignment(Qt.AlignCenter)
        count_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        count_label.setContentsMargins(0, 0, 0, 0)
        count_label.setStyleSheet(
            "QLabel {"
            "  color: palette(window-text);"
            "  background: palette(window);"
            "  border: 1px solid palette(mid);"
            "  border-radius: 9px;"
            "  padding: 1px 8px;"
            "  font-size: 11px;"
            "  font-weight: 500;"
            "}"
        )

        # separating line
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setLineWidth(1)
        line.setMidLineWidth(0)

        # clickable header frame
        header_frame = ClickableHeader()
        header_frame.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        header_layout.addWidget(chevron)
        header_layout.addWidget(header)
        header_layout.addWidget(count_label)
        header_layout.addWidget(line)
        header_frame.clicked.connect(lambda n=name: self._toggle_collapse(n))

        # list view с динамической высотой
        view = DynamicHeightListView(icon_size=self._icon_size, spacing=self._spacing)
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setFlow(QListView.LeftToRight)
        view.setWrapping(True)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Snap)
        view.setIconSize(self._icon_size)
        # Ctrl/Shift-клик и рамка: групповое ПКМ («Управление», команды).
        view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        view.selectionPressed.connect(self._on_view_selection_pressed)

        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)

        view.setUniformItemSizes(True)
        view.setWordWrap(True)

        # model
        model = QStandardItemModel()
        proxy = OnlineFilterProxyModel(self._online_only)
        proxy.setSourceModel(model)
        view.setModel(proxy)

        # forward signals for compatibility
        view.clicked.connect(self._make_forward_clicked(model, proxy))
        view.doubleClicked.connect(self._make_forward_doubleclicked(model, proxy))
        view.customContextMenuRequested.connect(
            lambda pos, v=view: self.show_list_context_menu(v, pos)
        )

        # container widget for this category
        cat_widget = QWidget()
        cat_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        cat_layout = QVBoxLayout(cat_widget)
        cat_layout.setContentsMargins(0, 0, 0, 0)
        cat_layout.setSpacing(2)
        cat_layout.addWidget(header_frame)
        cat_layout.addWidget(view)

        # store
        self._categories[name] = {
            "widget": cat_widget,
            "label": header,
            "chevron": chevron,
            "count_label": count_label,
            "view": view,
            "model": model,
            "proxy": proxy,
        }

        # вставляем перед финальным stretch (последний элемент в _vbox),
        # чтобы секции шли сверху вниз, а свободное место ушло в низ.
        self._vbox.insertWidget(self._vbox.count() - 1, cat_widget)

        # Устанавливаем gridSize для view
        self._update_view_grid(view)

        # Устанавливаем начальную высоту
        self._update_section_height(name)

        # If this is the default category, expose compatibility attributes
        if name == self._default_category:
            self._model = model
            self._view = view

    def _on_view_selection_pressed(self, source_view, keep: bool) -> None:
        """Обычный клик в одной категории снимает выделение в остальных."""
        if keep:
            return
        for info in self._categories.values():
            view = info["view"]
            if view is not source_view:
                view.clearSelection()

    def _make_forward_clicked(self, model, proxy):
        def _slot(index: QModelIndex):
            # forward the raw QModelIndex coming from the specific view
            self.clicked.emit(index)

        return _slot

    def _make_forward_doubleclicked(self, model, proxy):
        def _slot(index: QModelIndex):
            source_index = proxy.mapToSource(index)
            item = model.itemFromIndex(source_index)
            if item and hasattr(item, "device"):
                self.itemDoubleClicked.emit(item)

        return _slot

    def remove_category(self, name: str):
        """Удаляет категорию и её виджеты."""
        info = self._categories.pop(name, None)
        if not info:
            return

        # Удаляем из порядка категорий
        if name in self._category_order:
            self._category_order.remove(name)

        widget = info["widget"]
        self._vbox.removeWidget(widget)
        widget.deleteLater()

    def add_device(self, device: DeviceModel, category: str | None = None):
        """Add device to appropriate category (or default).
        If `category` provided, use it. If category is None, fallback to device.group/device.folder or default.
        """
        # Determine category name
        if category is None:
            category = (
                getattr(device, "group", None)
                or getattr(device, "folder", None)
                or self._default_category
            )
        else:
            # treat empty/None-like parent as explicit "Нет категории"
            if category == "" or category is None:
                category = "Нет категории"

        if category not in self._categories:
            self.add_category(category)

        model = self._categories[category]["model"]

        # Проверяем, есть ли уже такое устройство в этой категории (по iid, fallback по host)
        device_iid = getattr(device, "iid", None)
        device_host = getattr(device, "host", None)
        for i in range(model.rowCount()):
            existing_item = model.item(i)
            if (
                existing_item
                and hasattr(existing_item, "device")
                and existing_item.device
            ):
                existing_iid = getattr(existing_item.device, "iid", None)
                existing_host = getattr(existing_item.device, "host", None)
                # Связь по iid (первичный), fallback по host
                is_duplicate = False
                if device_iid and existing_iid:
                    is_duplicate = device_identity(device) == device_identity(existing_item.device)
                elif device_host and existing_host:
                    is_duplicate = device_host == existing_host and device_identity(device)[0] == device_identity(existing_item.device)[0]
                if is_duplicate:
                    logger.debug(
                        f"Device {device.name} ({device_host}) already exists in category {category}, skipping"
                    )
                    return existing_item

        item = CustomListItem(device.name, device)
        item.setEditable(False)
        # Вставка по natural-ключу имени (как сортировка дерева): первый
        # существующий элемент с "большим" ключом определяет позицию.
        new_key = natural_key(device.name or "")
        insert_row = model.rowCount()
        for i in range(model.rowCount()):
            existing = model.item(i)
            if existing and hasattr(existing, "device"):
                if natural_key(existing.device.name or "") > new_key:
                    insert_row = i
                    break
        model.insertRow(insert_row, item)

        # Хосты ноды: статус — per-host таймер у ноды-владельца (зеркало
        # локального пинга), миниатюры — подписка через NodeThumbnailManager
        # (стартует только после онлайн-подтверждения, гейт в менеджере).
        # Хосты локальной ноды — обычные локальные устройства (пинг/Veyon).
        niid = getattr(device, "node_iid", None)
        if niid and niid != LOCAL_NODE_IID:
            try:
                from src.workers.node.status import get_node_status_manager

                get_node_status_manager().start_ping(device)
            except Exception as e:
                logger.debug(f"DeviceListView: node status start failed: {e}")
            if self._thumbnail_mode:
                item._thumbnail_active = True
                try:
                    from src.workers.node.thumbnails import (
                        get_node_thumbnail_manager,
                    )

                    get_node_thumbnail_manager().start_thumbnail(device, item)
                except Exception:
                    pass
            self._update_section_height(category)
            self._update_header_count(category)
            if self._online_only:
                self._sync_auto_collapse(category)
            self._categories[category]["view"].viewport().update()
            total = sum(
                self._categories[c]["model"].rowCount() for c in self._categories
            )
            logger.debug(f"Total items in DeviceListView model: {total}")
            return item

        get_host_ping_timer_manager().start_ping(device)

        # Подписываемся на миниатюры Veyon, если режим включен
        if self._thumbnail_mode:
            item._thumbnail_active = True
            get_thumbnail_timer_manager().start_thumbnail(device, item)

        # Обновляем высоту секции и счётчик после добавления
        self._update_section_height(category)
        self._update_header_count(category)
        # Если включён «только онлайн» и добавлен онлайн-хост в авто-свёрнутую группу — развернуть
        if self._online_only:
            self._sync_auto_collapse(category)

        # layout updates
        self._categories[category]["view"].viewport().update()

        # return total across all categories
        total = sum(self._categories[c]["model"].rowCount() for c in self._categories)
        logger.debug(f"Total items in DeviceListView model: {total}")
        # return total
        return item

    def remove_device(self, device_name: str):
        """Remove device by name across all categories"""
        removed = False
        # iterate over a snapshot to allow modifying _categories inside loop
        for name, info in list(self._categories.items()):
            model = info["model"]
            # iterate backwards to safely remove rows
            for i in range(model.rowCount() - 1, -1, -1):
                item: CustomListItem = model.item(i)
                if item and item.text() == device_name:
                    if hasattr(item, "cleanup"):
                        item.cleanup()
                    model.removeRow(i)
                    removed = True
            # Обновляем высоту секции и счётчик после удаления
            if removed:
                self._update_section_height(name)
                self._update_header_count(name)
            # if category became empty, remove it (but keep default category)
            if model.rowCount() == 0 and name != self._default_category:
                self.remove_category(name)
        return removed

    def remove_device_by_host(self, device_host: str):
        """Remove device by host across all categories (DEPRECATED: use remove_device_by_iid)"""
        removed = False
        # iterate over a snapshot to allow modifying _categories inside loop
        for name, info in list(self._categories.items()):
            model = info["model"]
            # iterate backwards to safely remove rows
            for i in range(model.rowCount() - 1, -1, -1):
                item: CustomListItem = model.item(i)
                if item and hasattr(item, "device") and item.device:
                    item_host = getattr(item.device, "host", None)
                    if item_host == device_host:
                        if hasattr(item, "cleanup"):
                            item.cleanup()
                        model.removeRow(i)
                        removed = True
            # Обновляем высоту секции и счётчик после удаления
            if removed:
                self._update_section_height(name)
                self._update_header_count(name)
            # if category became empty, remove it (but keep default category)
            if model.rowCount() == 0 and name != self._default_category:
                self.remove_category(name)
        return removed

    def remove_device_by_iid(self, device_iid: str, node_iid: str | None = None):
        """Remove device by iid across all categories (первичный метод)."""
        if not device_iid:
            return False
        removed = False
        for name, info in list(self._categories.items()):
            model = info["model"]
            for i in range(model.rowCount() - 1, -1, -1):
                item: CustomListItem = model.item(i)
                if item and hasattr(item, "device") and item.device:
                    if device_identity(item.device) == device_key(device_iid, node_iid):
                        if hasattr(item, "cleanup"):
                            item.cleanup()
                        model.removeRow(i)
                        removed = True
            if removed:
                self._update_section_height(name)
                self._update_header_count(name)
            if model.rowCount() == 0 and name != self._default_category:
                self.remove_category(name)
        return removed

    def update_or_move_device_by_iid(
        self, device: DeviceModel, new_category: str = None, *, tree_item=None
    ):
        """Обновить отображение устройства в списке по iid (при переименовании/редактировании/перемещении).

        Ищет CustomListItem с совпадающим iid:
        - Если категория та же → обновляет текст/tooltip/иконку на месте.
        - Если категория изменилась (new_category передан и отличается) → удаляет элемент
          из старой категории и добавляет в новую, сохраняя tree_item back-reference.
        """
        if not device or not getattr(device, "iid", None):
            return False
        target_iid = device_identity(device)

        # Шаг 1: найти существующий элемент и его текущую категорию
        found_category = None
        found_item = None
        found_index = -1
        for name, info in list(self._categories.items()):
            model = info["model"]
            for i in range(model.rowCount()):
                candidate: CustomListItem = model.item(i)
                if candidate and hasattr(candidate, "device") and candidate.device:
                    if device_identity(candidate.device) == target_iid:
                        found_category = name
                        found_item = candidate
                        found_index = i
                        break
            if found_item is not None:
                break

        if found_item is None:
            return False
        if tree_item is not None:
            found_item.tree_item = tree_item

        # Определить целевую категорию
        if new_category is None or new_category == "":
            new_category = "Нет категории"

        # Шаг 2a: категория не изменилась — обновить на месте
        if found_category == new_category:
            model = self._categories[found_category]["model"]
            view = self._categories[found_category].get("view")
            try:
                changed = (
                    found_item._thumbnail_model_signature
                    != found_item._model_signature(device)
                )
                found_item.device = device
                found_item._original_icon = device.icon
                found_item._thumbnail_model_signature = found_item._model_signature(
                    device
                )
                if changed:
                    found_item.clear_thumbnail()
                if self._thumbnail_mode:
                    niid = getattr(device, "node_iid", None)
                    if niid and niid != LOCAL_NODE_IID:
                        from src.workers.node.thumbnails import (
                            get_node_thumbnail_manager,
                        )

                        get_node_thumbnail_manager().update_device(device)
                    else:
                        get_thumbnail_timer_manager().update_device(device)
                found_item.setText(device.name)
                # Тултип собирается динамически при наведении (HostInfoService).
                if not getattr(found_item, "_thumbnail_pixmap", None):
                    found_item.setIcon(qicon_from_path(device.icon))
                idx = found_item.index()
                if idx.isValid() and model is not None:
                    model.dataChanged.emit(
                        idx,
                        idx,
                        [
                            Qt.ItemDataRole.DisplayRole,
                            Qt.ItemDataRole.DecorationRole,
                            Qt.ItemDataRole.ToolTipRole,
                        ],
                    )
                if view is not None:
                    view.viewport().update()
            except (RuntimeError, AttributeError):
                pass
            return True

        # Шаг 2b: категория изменилась — переместить элемент
        # Сохранить back-reference до удаления
        saved_tree_item = getattr(found_item, "tree_item", None)

        # Удалить из старой категории
        old_model = self._categories[found_category]["model"]
        if hasattr(found_item, "cleanup"):
            try:
                found_item.cleanup()
            except (RuntimeError, AttributeError):
                pass
        old_model.removeRow(found_index)
        self._update_section_height(found_category)
        self._update_header_count(found_category)
        # Удалить пустую категорию (кроме дефолтной)
        if old_model.rowCount() == 0 and found_category != self._default_category:
            self.remove_category(found_category)

        # Добавить в новую категорию
        new_item = self.add_device(device, category=new_category)
        if new_item is not None and saved_tree_item is not None:
            new_item.tree_item = saved_tree_item
        # Миниатюра будет перезапрошена менеджером автоматически при добавлении
        return True

    def clear_devices(self):
        """Clear all devices from list and cleanup resources"""
        # Iterate over snapshot because remove_category may modify _categories
        for name, info in list(self._categories.items()):
            model = info["model"]
            # cleanup each item
            for i in range(model.rowCount() - 1, -1, -1):
                item: CustomListItem = model.item(i)
                if item and hasattr(item, "cleanup"):
                    item.cleanup()
                model.removeRow(i)
            # Обновляем высоту секции и счётчик
            self._update_section_height(name)
            self._update_header_count(name)
            # remove non-default empty categories
            if model.rowCount() == 0 and name != self._default_category:
                self.remove_category(name)
        # Сбрасываем состояния свёрнутости — категории пересоздаются чистыми
        self._collapsed.clear()
        self._auto_collapsed.clear()
        # ensure default view updated
        default_info = self._categories.get(self._default_category)
        if default_info:
            default_info["view"].viewport().update()

    def get_all_devices(self):
        """Get all devices from list"""
        devices: list[DeviceModel] = []
        for info in self._categories.values():
            model = info["model"]
            for i in range(model.rowCount()):
                item: CustomListItem = model.item(i)
                if item and hasattr(item, "device"):
                    devices.append(item.device)
        return devices

    def get_current_device(self):
        """Get currently selected device from active focused view"""
        # Try to find focused view first
        fw = self.focusWidget()
        if isinstance(fw, QListView):
            index = fw.currentIndex()
            proxy = fw.model()
            if isinstance(proxy, QSortFilterProxyModel):
                source_index = proxy.mapToSource(index)
                item = info_model = None
                for info in self._categories.values():
                    if info["view"] is fw:
                        item = info["model"].itemFromIndex(source_index)
                        break
                if item and hasattr(item, "device"):
                    return item.device
            else:
                item = (
                    proxy.itemFromIndex(index)
                    if hasattr(proxy, "itemFromIndex")
                    else None
                )
                if item and hasattr(item, "device"):
                    return item.device
        # Fallback: first selected item across categories
        for info in self._categories.values():
            view = info["view"]
            sel = view.selectedIndexes()
            if sel:
                proxy = info["proxy"]
                source_index = proxy.mapToSource(sel[0])
                item = info["model"].itemFromIndex(source_index)
                if item and hasattr(item, "device"):
                    return item.device
        return None

    def get_selected_devices(self):
        """Собрать выделенные устройства всех категорий.

        Повторы убираются по паре (нода, iid): адрес хоста ноды может
        совпадать с адресом локального хоста.
        """
        devices: list[DeviceModel] = []
        seen = set()
        for info in self._categories.values():
            view = info["view"]
            proxy = info["proxy"]
            model = info["model"]
            for idx in view.selectedIndexes():
                source_index = proxy.mapToSource(idx)
                item = model.itemFromIndex(source_index)
                if item and hasattr(item, "device") and item.device:
                    key = device_identity(item.device)
                    if key not in seen:
                        seen.add(key)
                        devices.append(item.device)
        return devices

    def _calc_pad(self) -> int:
        """Нелинейный зазор вокруг иконки в плитке (sqrt+clamp).

        На больших иконках зазор растёт медленнее иконки (sqrt), на маленьких —
        минимальный читаемый. Иконка масштабируется векторно (SVG-движок в
        src/ui/icons.py), pad это только отступ в gridSize.
        Для size>128 зазор уменьшен вдвое относительно базовой формулы.
        32px→4, 64px→6, 128px→8, 256px→6, 512px→8.
        """
        size = self._icon_size.width()
        base = math.sqrt(size / 64.0) * 6
        if size > 128:
            base /= 2.0
        return max(4, min(16, int(base)))

    def _update_view_grid(self, view: QListView):
        """Настраивает gridSize у QListView так, чтобы плитки имели адекватную ширину/высоту."""
        fm = view.fontMetrics()
        text_h = fm.height()
        pad = self._calc_pad()
        tile_w = self._icon_size.width() + pad
        tile_h = self._icon_size.height() + text_h + pad

        total_items = view.model().rowCount()
        view.setGridSize(QSize(tile_w, tile_h))

    def set_online_only(self, online_only: bool):
        """Включить/выключить фильтр отображения только онлайн-устройств.

        При включении: группы без онлайн-хостов авто-сворачиваются (виден заголовок + «0/N»).
        При выключении: все авто-свёрнутые группы разворачиваются обратно;
        ручные сворачивания пользователя не затрагиваются.
        """
        self._online_only = online_only
        for info in self._categories.values():
            info["proxy"].set_online_only(online_only)

        if online_only:
            # Авто-сворачиваем пустые группы
            for name in self._category_order:
                if self._visible_count(name) == 0 and name not in self._collapsed:
                    self._collapse(name, auto=True)
        else:
            # Разворачиваем все авто-свёрнутые группы
            for name in list(self._auto_collapsed):
                self._expand(name, auto=True)

        self._update_all_sections_height()
        self._update_all_header_counts()

    def set_icon_size(self, size: int):
        """Изменить размер иконок/миниатюр (квадрат size×size в обоих режимах).

        Миниатюра 16:9 вписывается в квадрат по ширине, центрируется по вертикали.
        """
        self._icon_size = QSize(size, size)
        for info in self._categories.values():
            view = info["view"]
            view.setIconSize(self._icon_size)
            self._update_view_grid(view)
            # Перерисовываем сохранённые миниатюры под новый размер
            model = info["model"]
            for i in range(model.rowCount()):
                item = model.item(i)
                if getattr(item, "_thumbnail_raw_pixmap", None) is not None:
                    item.re_render_thumbnail(self._icon_size)
        self._update_all_sections_height()
        # Обновляем ширину иконки в таймерах миниатюр
        get_thumbnail_timer_manager().update_icon_width(size)

    def refresh_thumbnails(self):
        """Перерисовать все сохранённые миниатюры под текущий размер и стиль отображения.

        Используется после смены настройки thumbnail_style (monitor/plain), чтобы сразу
        применить новый стиль без ожидания следующего сетевого обновления.
        """
        for info in self._categories.values():
            model = info["model"]
            for i in range(model.rowCount()):
                item = model.item(i)
                if getattr(item, "_thumbnail_raw_pixmap", None) is not None:
                    item.re_render_thumbnail(self._icon_size)
            info["view"].viewport().update()

    def apply_thumbnail_settings(self) -> None:
        """Apply canonical schedule/gating and invalidate edited capture parameters."""
        manager = get_thumbnail_timer_manager()
        manager.refresh_settings()
        for info in self._categories.values():
            model = info["model"]
            for row in range(model.rowCount()):
                item = model.item(row)
                niid = getattr(item.device, "node_iid", None)
                if niid and niid != LOCAL_NODE_IID:
                    continue
                previous = item._thumbnail_capture_revision
                if (
                    previous is not None
                    and previous != manager.get_subscription_revision(item.device.iid)
                ):
                    item.clear_thumbnail()
        self.set_thumbnail_interval(config.app.thumbnails.interval_sec)
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            node_manager = get_node_thumbnail_manager()
            node_manager.refresh_settings()
        except Exception as exc:
            logger.debug(f"DeviceListView: node thumbnail settings refresh: {exc}")
        self.refresh_thumbnails()

    def refresh_icons(self) -> None:
        """Перечитать иконки списка под текущую тему иконок.

        Пути устройств ремаппятся на тему (разделяемые с деревом объекты
        DeviceModel - ремап идемпотентен), затем иконка элемента
        обновляется, если нет активной миниатюры; veyon_down - свежая.
        Кэш рамки миниатюр сбрасывается, сохранённые миниатюры
        перерисовываются под новую рамку темы сразу.
        """
        from src.config import remap_icon_path

        reset_thumb_renderer()
        for info in self._categories.values():
            model = info["model"]
            # Массовое обновление иконок без шторма dataChanged: каждый
            # setIcon иначе инвалидирует прокси/перерисовывает строку.
            # Чекбоксов в списке нет, иные роли не меняются.
            blocked = False
            try:
                blocked = model.signalsBlocked()
                model.blockSignals(True)
            except Exception:
                blocked = False
            try:
                for i in range(model.rowCount()):
                    item = model.item(i)
                    device = getattr(item, "device", None)
                    if device is None:
                        continue
                    try:
                        device.icon = remap_icon_path(getattr(device, "icon", ""))
                        if hasattr(device, "_icon_online"):
                            device._icon_online = remap_icon_path(device._icon_online)
                        if hasattr(device, "_icon_offline"):
                            device._icon_offline = remap_icon_path(device._icon_offline)
                        item._original_icon = device.icon
                        if getattr(item, "_thumbnail_pixmap", None) is not None:
                            if getattr(item, "_thumbnail_raw_pixmap", None) is not None:
                                try:
                                    item.re_render_thumbnail(self._icon_size)
                                except Exception:
                                    pass
                            continue
                        if getattr(item, "_veyon_down", False):
                            item.setIcon(_icon_from_key("veyon_down"))
                        else:
                            item.setIcon(qicon_from_path(device.icon))
                    except Exception:
                        pass
            finally:
                try:
                    if not blocked:
                        model.blockSignals(False)
                except Exception:
                    pass
            try:
                info["view"].viewport().update()
            except Exception:
                pass

    def set_thumbnail_mode(self, enabled: bool):
        """Включить/выключить миниатюры экранов вместо стандартных иконок."""
        self._thumbnail_mode = enabled
        mgr = get_thumbnail_timer_manager()
        # Менеджер нод (федерация): node-хосты обслуживаются отдельно
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            node_mgr = get_node_thumbnail_manager()
        except Exception:
            node_mgr = None
        if enabled:
            mgr.set_mode_enabled(True)
            if node_mgr is not None:
                node_mgr.set_mode_enabled(True)
            # Подписываем все текущие устройства
            for info in self._categories.values():
                model = info["model"]
                for i in range(model.rowCount()):
                    item = model.item(i)
                    if hasattr(item, "device") and item.device:
                        item._thumbnail_active = True
                        niid = getattr(item.device, "node_iid", None)
                        if niid and niid != LOCAL_NODE_IID:
                            if node_mgr is not None:
                                node_mgr.start_thumbnail(item.device, item)
                        else:
                            mgr.start_thumbnail(item.device, item)
        else:
            mgr.set_mode_enabled(False)
            if node_mgr is not None:
                node_mgr.set_mode_enabled(False)
            # Возвращаем стандартные иконки
            for info in self._categories.values():
                model = info["model"]
                for i in range(model.rowCount()):
                    item = model.item(i)
                    if hasattr(item, "clear_thumbnail"):
                        item._thumbnail_active = False
                        item.clear_thumbnail()

    def set_thumbnail_interval(self, seconds: int):
        """Изменить интервал обновления миниатюр (сек)."""
        get_thumbnail_timer_manager().set_interval(max(1, seconds))
        # Менеджер нод (федерация): node-таймеры живут отдельно
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            get_node_thumbnail_manager().set_interval(max(1, seconds))
        except Exception:
            pass

    def deactivate_thumbnails(self):
        """Приостановить живые миниатюры при недоступности Veyon.

        Останавливает все таймеры захвата и очищает показанные миниатюры,
        возвращая стандартные иконки устройств (онлайн/офлайн). Режим
        миниатюр (_thumbnail_mode) сохраняется — при возобновлении Veyon
        миниатюры перезапустятся автоматически через reactivate_thumbnails().

        Нод-хостов не трогаем: их миниатюры идут через NodeThumbnailManager
        (нода опрашивает хосты сама) и продолжают приходить.
        """
        mgr = get_thumbnail_timer_manager()
        mgr.stop_all()
        for info in self._categories.values():
            model = info["model"]
            for i in range(model.rowCount()):
                item = model.item(i)
                dev = getattr(item, "device", None)
                niid = getattr(dev, "node_iid", None) if dev else None
                if niid and niid != LOCAL_NODE_IID:
                    continue  # нод-хост удалённой ноды — обслуживается нод-менеджером
                if hasattr(item, "clear_thumbnail"):
                    item.clear_thumbnail()
            info["view"].viewport().update()

    def reactivate_thumbnails(self):
        """Возобновить живые миниатюры после восстановления Veyon.

        Перезапускает таймеры захвата для всех текущих устройств, если
        режим миниатюр включён. Вызывается при переходе Veyon-статуса
        False → True. Нод-хосты маршрутизируются в NodeThumbnailManager
        (как в set_thumbnail_mode) — локальный Veyon-менеджер им не подходит.
        """
        if not self._thumbnail_mode:
            return
        mgr = get_thumbnail_timer_manager()
        mgr.set_mode_enabled(True)
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            node_mgr = get_node_thumbnail_manager()
            node_mgr.set_mode_enabled(True)
        except Exception:
            node_mgr = None
        for info in self._categories.values():
            model = info["model"]
            for i in range(model.rowCount()):
                item = model.item(i)
                if hasattr(item, "device") and item.device:
                    item._thumbnail_active = True
                    niid = getattr(item.device, "node_iid", None)
                    if niid and niid != LOCAL_NODE_IID:
                        if node_mgr is not None:
                            node_mgr.start_thumbnail(item.device, item)
                    else:
                        mgr.start_thumbnail(item.device, item)

    def _apply_thumbnail(self, item, image_data, backend_result=True):
        """Apply a current frame or neutral source diagnostic in the GUI thread.

        The third argument is ThumbnailResult, or a legacy node's readiness flag.
        Ordinary host reachability is never changed by a capture/authentication error.
        """
        if not self._thumbnail_mode or not getattr(item, "_thumbnail_active", False):
            return
        try:
            model = item.model()
            if model is None or not any(
                info["model"] is model for info in self._categories.values()
            ):
                return
            if (
                not item.index().isValid()
                or model.itemFromIndex(item.index()) is not item
            ):
                return
        except RuntimeError:
            return
        result = backend_result if isinstance(backend_result, ThumbnailResult) else None
        if result is not None:
            device = item.device
            niid = getattr(device, "node_iid", None)
            if not niid or niid == LOCAL_NODE_IID:
                if result.backend != (
                    device.thumbnail_backend or config.app.thumbnails.backend
                ):
                    return
                captured = item._thumbnail_capture_revision
                if (
                    captured is not None
                    and captured
                    != get_thumbnail_timer_manager().get_subscription_revision(
                        device.iid
                    )
                ):
                    return
            if result.status == ThumbnailStatus.CANCELLED:
                return
            if result.status != ThumbnailStatus.OK:
                image_data = None
        if image_data is None:
            try:
                device = getattr(item, "device", None)
                if (
                    device is not None
                    and (result is None or result.status != ThumbnailStatus.OFFLINE)
                ):
                    if hasattr(item, "set_thumbnail_error"):
                        item.set_thumbnail_error(
                            result.backend if result else "veyon",
                            result.status if result else "unavailable",
                        )
                        for info in self._categories.values():
                            info["view"].viewport().update()
                    return
                # Drop an unusable old frame; retain ordinary online/offline semantics.
                if hasattr(item, "clear_thumbnail"):
                    item.clear_thumbnail()
                    for info in self._categories.values():
                        info["view"].viewport().update()
            except (RuntimeError, AttributeError):
                pass
            return
        try:
            pixmap = QPixmap()
            ok = pixmap.loadFromData(QByteArray(image_data))
            if not ok or pixmap.isNull():
                if hasattr(item, "set_thumbnail_error"):
                    item.set_thumbnail_error(
                        result.backend if result else "veyon", "protocol_error"
                    )
                return
            target_w = self._icon_size.width()
            target_h = self._icon_size.height()
            result = _compose_thumbnail(pixmap, target_w, target_h)
            item.set_thumbnail(result, raw_pixmap=pixmap)
            for info in self._categories.values():
                info["view"].viewport().update()
        except (RuntimeError, AttributeError):
            pass

    def resizeEvent(self, event):
        """Обработка изменения размера виджета"""
        super().resizeEvent(event)
        # Пересчитываем высоты всех секций при изменении размера
        # Используем QTimer для отложенного вызова после завершения resize
        QTimer.singleShot(0, self._update_all_sections_height)

    # Compatibility helper so callers that expect a QListView-like .viewport() work
    def viewport(self):
        """Return a QWidget representing the viewport (compatibility with old QListView usage)."""
        if getattr(self, "_view", None) is not None:
            return self._view.viewport()
        # Fallback: return scroll area's viewport
        return self._scroll.viewport()

    # Menu / commands handling: adapted to per-view calls
    def create_commands_menu(
        self, parent_item: QMenu, data_structure, devices: list | None = None
    ) -> QMenu:
        """Дерево задач в меню.

        ``devices`` — хосты, на которых выполняется задача: один хост идёт
        прежним путём (``commandsExecuted``), несколько —
        ``commandsExecutedForDevices``. Без списка — текущий хост списка.
        """
        if not isinstance(data_structure, list):
            return parent_item
        if devices is None:
            current = self.get_current_device()
            devices = [current] if current is not None else []
        devices = list(devices)
        for item_data in data_structure:
            if not isinstance(item_data, dict):
                continue
            if "name" in item_data and "commands" in item_data:
                command_name = item_data.get("name", "Без имени")
                command_action = QAction(
                    _icon_from_key("command"), command_name, parent_item
                )
                parent_item.addAction(command_action)
                commands = item_data["commands"]
                if commands and isinstance(commands, list):
                    command_data = {
                        "iid": item_data.get("iid"),
                        "name": command_name,
                        "commands": commands,
                        "description": item_data.get("description", ""),
                        "params": item_data.get("params", []),
                        "timeout": item_data.get(
                            "timeout", config.app.ssh.command_timeout
                        ),
                    }
                    command_action.triggered.connect(
                        lambda _=False, command_data=command_data: (
                            self._execute_command_on_devices(devices, command_data)
                        )
                    )
            elif len(item_data) == 1:
                category_name = list(item_data.keys())[0]
                child_data_list = item_data[category_name]
                if isinstance(child_data_list, list):
                    category_item = parent_item.addMenu(category_name)
                    category_item.setIcon(_icon_from_key("folder"))
                    self.create_commands_menu(category_item, child_data_list, devices)
        return parent_item

    def _build_control_menu(self, on_feature, parent=None) -> QMenu:
        """Подменю «Управление»: ``on_feature(token)`` вызывается по пункту.

        Только функции через SSH/UDP/DBus (Veyon WebAPI не нужен), поэтому
        подменю видно всегда.
        """
        control_submenu = QMenu("Управление", parent if parent is not None else self)
        control_submenu.setIcon(_icon_from_key("menu_veyon_control"))
        for gi, group in enumerate(CONTROL_FEATURE_GROUPS):
            if gi > 0:
                control_submenu.addSeparator()
            for token, icon_key, label in group:
                act = control_submenu.addAction(_icon_from_key(icon_key), label)
                act.triggered.connect(lambda _=False, t=token: on_feature(t))
        return control_submenu

    def _build_multi_host_menu(self, devices: list) -> QMenu:
        """Групповое меню для нескольких выделенных хостов (без показа).

        Только действия, которые имеют смысл на группе: задачи и
        «Управление». Хосты нод допускаются: команды и функции
        маршрутизируются в MainWindow по ``node_iid``.
        """
        devices = list(devices)
        menu = QMenu(self)
        header = menu.addAction(f"Выбрано хостов: {len(devices)}")
        header.setEnabled(False)
        menu.addSeparator()

        commands_submenu = QMenu("Выполнить команду", menu)
        commands_submenu.setIcon(_icon_from_key("command"))
        custom_action = commands_submenu.addAction(
            _icon_from_key("command"), "Своя команда…"
        )
        custom_action.triggered.connect(
            lambda _=False: self.customCommandRequestedForDevices.emit(list(devices))
        )
        commands_submenu.addSeparator()
        self.create_commands_menu(
            commands_submenu, datastore.get_commands_data(), devices
        )
        menu.addMenu(commands_submenu)

        menu.addSeparator()
        menu.addMenu(
            self._build_control_menu(
                lambda t: self.veyonFeatureRequestedForDevices.emit(list(devices), t),
                parent=menu,
            )
        )
        return menu

    def show_list_context_menu(self, view: QListView, position: QPoint):
        """Show context menu for item in given view at position (local to that view)"""
        index = view.indexAt(position)
        if index.isValid():
            proxy = view.model()
            # Map proxy index to source model to get the CustomListItem
            if isinstance(proxy, QSortFilterProxyModel):
                source_index = proxy.mapToSource(index)
                # Find the source model for this view
                item = None
                for info in self._categories.values():
                    if info["view"] is view:
                        item = info["model"].itemFromIndex(source_index)
                        break
            else:
                item = (
                    proxy.itemFromIndex(index)
                    if hasattr(proxy, "itemFromIndex")
                    else None
                )
            if isinstance(item, CustomListItem) and item.device:
                # ПКМ по одному из нескольких выделенных хостов — групповое
                # меню: только «Выполнить команду» и «Управление».
                selected = self.get_selected_devices()
                clicked_key = device_identity(item.device)
                if len(selected) > 1 and any(
                    device_identity(d) == clicked_key for d in selected
                ):
                    menu = self._build_multi_host_menu(selected)
                    menu.exec_(view.mapToGlobal(position))
                    menu.deleteLater()
                    return

                menu = QMenu()

                # Node-хост удалённой ноды: подсеть ноды недоступна оператору
                # напрямую — инструменты с прямой SSH-сессией (терминал, SFTP,
                # KDE, удалённая запись, редактор политик) скрыты. Live-данные —
                # через тултип (запрос к ноде) и «Информация о устройстве»
                # (client.host_info). Команды/фичи/Veyon — маршрутизируются
                # на ноду-владельца (см. MainWindow.execute_command /
                # _show_veyon_feature_node).
                is_remote_node = (
                    bool(getattr(item.device, "node_iid", None))
                    and getattr(item.device, "node_iid", None) != LOCAL_NODE_IID
                )

                commands_submenu = QMenu("Выполнить команду", self)
                commands_submenu.setIcon(_icon_from_key("command"))
                menu.addMenu(commands_submenu)
                custom_action = commands_submenu.addAction(
                    _icon_from_key("command"), "Своя команда…"
                )
                custom_action.triggered.connect(
                    lambda _=False, d=item.device: self.customCommandRequested.emit(d)
                )
                commands_submenu.addSeparator()
                commands_submenu = self.create_commands_menu(
                    commands_submenu, datastore.get_commands_data(), [item.device]
                )

                menu.addSeparator()

                if not is_remote_node:
                    terminal_action = menu.addAction(
                        _icon_from_key("menu_terminal"), "Открыть в терминале"
                    )
                    terminal_action.triggered.connect(item.open_terminal)

                    sftp_action = menu.addAction(
                        _icon_from_key("menu_sftp"), "Обмен файлами"
                    )
                    sftp_action.triggered.connect(item.open_sftp)

                    kde_action = menu.addAction(
                        _icon_from_key("menu_kde"), "Настройки KDE"
                    )
                    kde_action.setStatusTip(
                        "Управление настройками KDE на удалённой машине"
                    )
                    kde_action.triggered.connect(
                        lambda: self._open_kde_config_dialog(item.device)
                    )

                info_action = menu.addAction(
                    _icon_from_key("menu_info"), "Информация об устройстве"
                )
                info_action.triggered.connect(
                    lambda _, device=item.device: self.deviceInfoRequested.emit(device)
                )

                if not is_remote_node:
                    recording_action = menu.addAction(
                        _icon_from_key("menu_recording"), "Удалённая запись"
                    )
                    recording_action.triggered.connect(
                        lambda: self.remoteRecordingRequested.emit(item.device)
                    )

                    polkit_action = menu.addAction(
                        _icon_from_key("menu_polkit"), "Редактор политик"
                    )
                    polkit_action.triggered.connect(
                        lambda: self.polkitEditorRequested.emit(item.device)
                    )

                menu.addSeparator()

                veyon_available = getattr(self, "_veyon_features_available", True)
                # Node-хост удалённой ноды: локальный Veyon не важен — фичи
                # выполняются на ноде-владельце (её Veyon), маршрутизация в
                # MainWindow._show_veyon_feature_node. Показываем подменю, если
                # нода доступна (reachable) — иначе фича всё равно упадёт с
                # понятной ошибкой «нода недоступна».
                if is_remote_node:
                    try:
                        from src.di import get_container
                        from src.services import NodeService

                        _svc = get_container().resolve_optional(NodeService)
                        veyon_available = _svc is not None and _svc.is_reachable(
                            item.device.node_iid
                        )
                    except Exception:
                        veyon_available = False

                # Подменю «Управление» — всегда видимо.
                # Содержит только фичи, работающие через SSH/UDP/DBus (не требуют
                # Veyon WebAPI): power_on (WoL/UDP), reboot/powerdown/logoff
                # (systemctl/loginctl через SSH), login_ssh (DBus org.mos.auth через SSH).
                # logoff реализован через SSH (KDE DBus / loginctl),
                # login_ssh — через DBus (требует mos-auth-dbus-conf на хосте);
                # оба отнесены сюда, а не в Veyon-подменю.
                control_submenu = self._build_control_menu(
                    lambda t, d=item.device: self.veyonFeatureRequested.emit(d, t)
                )
                menu.addMenu(control_submenu)

                # Подменю «VNC (бекенд)» — динамическое: имя бекенда берётся
                # из настроек (veyon.vnc_server; per-host override), а не
                # захардкодено. Требует SSH-операций (пароль/рестарт) — для
                # нод-хостов не показывается (нет прямого SSH).
                vnc_server = (
                    getattr(item.device, "vnc_server", None)
                    or getattr(config.app.veyon, "vnc_server", DEFAULT_VNC_SERVER)
                    or DEFAULT_VNC_SERVER
                )
                backend_label = {"krfb": "KRFB", "kmsvnc": "kmsvnc"}.get(
                    vnc_server, "x11vnc"
                )
                if not is_remote_node:
                    vnc_submenu = QMenu(f"VNC ({backend_label})", self)
                    vnc_submenu.setIcon(_icon_from_key("menu_krfb"))

                    import sys as _sys

                    if _sys.platform == "win32":
                        # Windows: krdc недоступен - только браузер.
                        vnc_view_action = vnc_submenu.addAction(
                            _icon_from_key("menu_veyon_view"),
                            "Просмотр экрана (браузер)",
                        )
                        vnc_view_action.triggered.connect(
                            lambda: self.veyonAccessRequested.emit(
                                item.device, "krfbbr_view"
                            )
                        )
                        vnc_control_action = vnc_submenu.addAction(
                            _icon_from_key("menu_veyon_control"),
                            "Управление (браузер)",
                        )
                        vnc_control_action.triggered.connect(
                            lambda: self.veyonAccessRequested.emit(
                                item.device, "krfbbr_control"
                            )
                        )
                    else:
                        # Linux: клиент krdc (если установлен) + браузер.
                        from src.services.remote.krfb_service import KRFBService

                        # krdc установлен - обязательное условие клиентских
                        # пунктов (иначе клик даёт ошибку вместо подключения).
                        krdc_ok = vnc_server == "krfb" and KRFBService.krdc_available()
                        if krdc_ok:
                            vnc_view_action = vnc_submenu.addAction(
                                _icon_from_key("menu_veyon_view"),
                                "Просмотр экрана (клиент)",
                            )
                            vnc_view_action.triggered.connect(
                                lambda: self.veyonAccessRequested.emit(
                                    item.device, "krfbkr_view"
                                )
                            )
                            vnc_control_action = vnc_submenu.addAction(
                                _icon_from_key("menu_veyon_control"),
                                "Управление (клиент)",
                            )
                            vnc_control_action.triggered.connect(
                                lambda: self.veyonAccessRequested.emit(
                                    item.device, "krfbkr_control"
                                )
                            )
                            vnc_submenu.addSeparator()
                        vnc_browser_view = vnc_submenu.addAction(
                            _icon_from_key("menu_veyon_view"),
                            "Просмотр экрана (браузер)",
                        )
                        vnc_browser_view.triggered.connect(
                            lambda: self.veyonAccessRequested.emit(
                                item.device, "krfbbr_view"
                            )
                        )
                        vnc_browser_control = vnc_submenu.addAction(
                            _icon_from_key("menu_veyon_control"),
                            "Управление (браузер)",
                        )
                        vnc_browser_control.triggered.connect(
                            lambda: self.veyonAccessRequested.emit(
                                item.device, "krfbbr_control"
                            )
                        )

                    vnc_submenu.addSeparator()

                    vnc_password_action = vnc_submenu.addAction(
                        _icon_from_key("menu_credentials"),
                        f"Сменить пароль {backend_label}",
                    )
                    vnc_password_action.triggered.connect(
                        lambda: self.veyonFeatureRequested.emit(
                            item.device, "krfb_password"
                        )
                    )

                    vnc_restart_action = vnc_submenu.addAction(
                        _icon_from_key("veyon_restart_service"),
                        f"Перезапустить {backend_label}",
                    )
                    vnc_restart_action.triggered.connect(
                        lambda: self.veyonFeatureRequested.emit(
                            item.device, "krfb_restart"
                        )
                    )

                    menu.addMenu(vnc_submenu)

                # Подменю «Veyon» — только при доступности Veyon.
                # Содержит фичи, зависящие от Veyon WebAPI или RFB-моста:
                # просмотр/управление экраном, lock/unlock, login (WebAPI),
                # logoff_webapi (UserLogoffFeature через WebAPI — штатный выход
                # из сессии Veyon'ом; устойчивый SSH-вариант `logoff` через
                # KDE DBus / loginctl живёт в подменю «Управление»),
                # website/start_app/message, restart_service (перезапуск
                # сервиса Veyon на хосте — семантически относится к Veyon).
                if veyon_available:
                    veyon_submenu = QMenu("Удалённый экран", self)
                    veyon_submenu.setIcon(_icon_from_key("menu_veyon"))

                    veyon_view_action = veyon_submenu.addAction(
                        _icon_from_key("menu_veyon_view"), "Просмотр экрана"
                    )
                    veyon_view_action.triggered.connect(
                        lambda: self.veyonAccessRequested.emit(item.device, "view")
                    )

                    veyon_control_action = veyon_submenu.addAction(
                        _icon_from_key("menu_veyon_control"), "Управление"
                    )
                    veyon_control_action.triggered.connect(
                        lambda: self.veyonAccessRequested.emit(item.device, "control")
                    )

                    veyon_submenu.addSeparator()

                    veyon_groups = [
                        [
                            ("lock", "veyon_lock", "Заблокировать экран"),
                            ("unlock", "veyon_unlock", "Разблокировать экран"),
                            ("lock_input", "veyon_lock_input", "Заблокировать ввод"),
                            (
                                "unlock_input",
                                "veyon_unlock_input",
                                "Разблокировать ввод",
                            ),
                        ],
                        [
                            ("login", "veyon_login", "Вход пользователя"),
                            (
                                "logoff_webapi",
                                "veyon_logoff",
                                "Выход пользователя",
                            ),
                        ],
                        [
                            ("website", "veyon_website", "Открыть сайт"),
                            ("start_app", "veyon_start_app", "Запустить программу"),
                            ("message", "veyon_message", "Отправить сообщение"),
                        ],
                        [
                            (
                                "restart_service",
                                "veyon_restart_service",
                                "Перезагрузить сервис Veyon",
                            ),
                        ],
                    ]
                    for gi, group in enumerate(veyon_groups):
                        if gi > 0:
                            veyon_submenu.addSeparator()
                        for token, icon_key, label in group:
                            act = veyon_submenu.addAction(
                                _icon_from_key(icon_key), label
                            )
                            act.triggered.connect(
                                lambda _, t=token: self.veyonFeatureRequested.emit(
                                    item.device, t
                                )
                            )

                    menu.addMenu(veyon_submenu)

                global_pos = view.mapToGlobal(position)
                menu.exec_(global_pos)

    def _emit_item_doubleclicked_from_view(self, view: QListView, index: QModelIndex):
        proxy = view.model()
        if isinstance(proxy, QSortFilterProxyModel):
            source_index = proxy.mapToSource(index)
            item = None
            for info in self._categories.values():
                if info["view"] is view:
                    item = info["model"].itemFromIndex(source_index)
                    break
        else:
            item = (
                proxy.itemFromIndex(index) if hasattr(proxy, "itemFromIndex") else None
            )
        if item and hasattr(item, "device"):
            self.itemDoubleClicked.emit(item)

    def execute_command_on_device(self, device, command_data):
        if not device:
            device = self.get_current_device()

        if device and command_data:
            self.commandsExecuted.emit(device, command_data)

    def _execute_command_on_devices(self, devices: list, command_data: dict) -> None:
        """Задача из ПКМ: один хост — прежний сигнал, несколько — групповой."""
        if not command_data:
            return
        if len(devices) > 1:
            self.commandsExecutedForDevices.emit(list(devices), command_data)
        else:
            self.execute_command_on_device(devices[0] if devices else None, command_data)

    # ARCHITECTURE: Статический метод для получения Command с отложенным импортом
    @staticmethod
    def _get_command_class():
        """Ленивый импорт Command для избежания циклической зависимости"""
        from src.workers import Command

        return Command

    def _open_kde_config_dialog(self, device):
        """Открыть диалог управления настройками KDE"""
        try:
            from src.ui.dialogs.kde import KDEConfigDialog

            dialog = KDEConfigDialog(device, self)
            dialog.exec()

        except ImportError as e:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self, "Ошибка", f"Не удалось загрузить модуль управления KDE:\n{e}"
            )
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self, "Ошибка", f"Ошибка при открытии диалога KDE:\n{e}"
            )

    def set_veyon_features_available(self, available: bool):
        """Управление видимостью Veyon-подменю в контекстном меню.

        Когда Veyon недоступен — подменю «Veyon» (просмотр/управление +
        фичи lock/unlock/...) не добавляется в ПКМ. Проверка выполняется
        в момент показа меню через `getattr(self, '_veyon_features_available', True)`.
        """
        self._veyon_features_available = bool(available)
