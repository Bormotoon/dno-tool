# -*- mode: python ; coding: utf-8 -*-

import os
import sys
sys.path.insert(0, os.path.join(SPECPATH, "packaging", "media"))
from bundle import media_files, unify_media_binaries, clean_media_aliases
_media_binaries, _media_datas = media_files()

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Вариант со служебным мастер-ключом нод: DNO_MASTER_KEY=<файл> (-MasterKey <файл>
# у сборочных скриптов) кладёт ключ в корень сборки как master-key.pub.
sys.path.insert(0, os.path.join(SPECPATH, "packaging"))
from master_key import datas as master_key_datas
_master_datas = master_key_datas(workpath)


def _icon_datas():
    """Файлы assets/icons без папок тем: темы едут zip-архивами.

    Папки assets/icons/<id>/ (~1000 SVG) в бандл не кладутся - рантайм
    читает темы из <id>.zip на лету (src/config/icon_theme_loader.py)
    с fallback на папки в dev-режиме. Корневые файлы (themes.json,
    favicon, toolbar_overflow) нужны всегда.
    """
    out = []
    d = os.path.join("assets", "icons")
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            out.append((p, os.path.join("assets", "icons")))
    return out


def _custom_pack_datas():
    """Custom-паки кастомных панелей: только zip (frozen без папок).

    Рантайм читает assets/custom/mono.zip и assets/custom/color.zip
    на лету (icon_theme_loader.get_pack_bytes) с fallback на папки
    assets/custom/mono|color в dev-режиме. Папки (~2300 SVG) в бандл
    не кладутся. Если zip еще не собран - fallback на папку, чтобы
    локальная сборка не падала.
    """
    out = []
    for rel in ("custom/mono.zip", "custom/color.zip"):
        zp = os.path.join("assets", *rel.split("/"))
        if os.path.isfile(zp):
            out.append((zp, os.path.join("assets", os.path.dirname(rel))))
            continue
        dp = os.path.join("assets", os.path.dirname(rel),
                           os.path.splitext(os.path.basename(rel))[0])
        if os.path.isdir(dp):
            out.append((dp, os.path.join("assets", os.path.dirname(rel),
                                          os.path.splitext(os.path.basename(rel))[0])))
    return out


a = Analysis(
    ['__main__.py'],
    pathex=[],
    binaries=_media_binaries,
    datas=_media_datas + [
        ('assets/themes', 'assets/themes'),
        # Пакеты, которые мастер настройки ставит на хосты (kmsvnc).
        ('assets/remote', 'assets/remote'),
        ('src/workers/gst_rtsp_server.py', 'src/workers'),
        ('src/workers/remote_recording_runtime.py', 'src/workers'),
        ('src/workers/remote_session_lock.py', 'src/workers'),
        ('src/web/static', 'src/web/static'),
    ] + _master_datas + _icon_datas() + _custom_pack_datas() + collect_data_files('fastapi') + collect_data_files('uvicorn') \
      + collect_data_files('pydantic') + collect_data_files('starlette') \
      + collect_data_files('certifi') \
      + collect_data_files('PySide6', includes=['**/qtbase_ru.qm']),
    hiddenimports=[
        'av',
        'av.codec.hwaccel',
        'PySide6.QtQuickWidgets',
        'PySide6.QtMultimedia',
        'src.services.media',
        'src.services.media.graph',
        'src.services.media.render',
        'src.services.media.camera',
        'requests',
        'yaml',
        # Лексеры редактора и ленивые таблицы встроенных функций PHP.
        'pygments.lexers._php_builtins',
        # ── Web-сервер (FastAPI + uvicorn) ──
        'python_multipart',
        'multipart',
        'h11',
        'websockets',
        'httptools',
        'uvloop',
        # ── Veyon RFB bridge (RSA signing) ──
        'cryptography',
        'cryptography.hazmat.primitives.asymmetric.padding',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.primitives.serialization',
        # ── Credential bundles (AES-128-CBC password encryption) ──
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.ciphers.modes',
        'cryptography.hazmat.decrepit.ciphers.algorithms',
        'cryptography.hazmat.primitives.padding',
        # ── WoL: directed broadcast per-interface (ленивый import) ──
        'psutil',
        'psutil._psutil_windows',
        'psutil._pswindows',
        'psutil._common',
        # ── Внутренние модули dno-tool (ленивые импорты) ──
        'src.web',
        'src.web.server',
        'src.web.app',
        'src.web.desktop_api',
        'src.web.scanner_api',
        'src.web.camera_api',
        'src.web.asset_access',
        'src.web.device_info_api',
        'src.web.terminal_api',
        'src.web.files_api',
        'src.utils.security.web_tls',
        'src.services.hosts.device_info.collector',
        'src.services.remote.camera_web_bridge',
        'src.workers.task_presentation',
        'src.utils.text.time_format',
        'src.utils.text.command_highlight',
        'pygments.lexers.shell',
        'src.utils.scanner.engine',
        'src.web.auth',
        'src.web.executor',
        'src.services.configuration.credential_service',
        'src.services.hosts.host_info_service',
        'src.services.hosts.kde_service',
        'src.domain.kde',
        'src.config.kde_schema',
        'src.config.kde_help',
        'src.config.kde_labels',
        'src.config.kde_catalog_audit',
        'src.ui.dialogs.kde.kde_config_dialog',
        'src.ui.dialogs.kde.controller',
        'src.ui.dialogs.kde.delegates',
        'src.ui.dialogs.kde.setting_row',
        'src.ui.dialogs.kde.fields',
        'src.ui.dialogs.kde.settings_model',
        'src.workers.kde.operation',
        'src.workers.kde.remote_script',
        'src.services.thumbnails.thumbnail_service',
        'src.services.thumbnails.types',
        'src.services.thumbnails.veyon',
        'src.services.thumbnails.vnc',
        'src.services.thumbnails.rfb',
        'src.ui.dialogs.common.thumbnail_settings',
        'src.ui.widgets.bundle_selector',
        'src.ui.dialogs.tools.credentials',
        # ── Система нод (федерация, Ed25519 + NodeServer + клиент) ──
        'cryptography.hazmat.primitives.asymmetric.ed25519',
        'src.utils.security.node_crypto',
        'src.services.remote.node_service',
        'src.web.node_server',
        'src.web.node_api_models',
        'src.utils.security.node_transport',
        'src.domain.device_identity',
        'src.workers.node',
        'src.workers.node.client',
        'src.workers.node.status',
        'src.workers.node.thumbnails',
        'src.workers.node.command_worker',
        'src.workers.setup.installers.ssh',
        'src.workers.setup.installers.packages',
        'src.workers.setup.installers.veyon',
        'src.workers.setup.installers.vnc',
        'src.workers.setup.installers.x11vnc',
        'src.workers.setup.installers.krfb',
        'src.workers.setup.installers.vnc_common',
        'src.workers.setup.installers.system_vnc',
        'src.workers.setup.installers.kmsvnc',
        'src.workers.setup.phases',
        'src.workers.setup.packages',
        'src.ui.dialogs.wizard.setups.service',
        'src.ui.dialogs.tools.node_settings',
    ] + collect_submodules('fastapi') + collect_submodules('uvicorn') \
      + collect_submodules('pydantic') + collect_submodules('starlette') \
      + collect_submodules('multipart') + collect_submodules('python_multipart') \
      + collect_submodules('psutil') + collect_submodules('icmplib') \
      + collect_submodules('ldap3'),
    hookspath=["packaging/media/hooks"],
    hooksconfig={},
    runtime_hooks=["packaging/media/runtime_hook.py"],
    excludes=[],
    noarchive=False,
    optimize=0,
)
a.binaries = unify_media_binaries(a.binaries, include_media=True)
a.datas = clean_media_aliases(a.datas, keep_resources=True)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='dno-tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icons\\favicon.ico'],
)
