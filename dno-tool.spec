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
from pyi_common import COMMON_WORKER_DATAS, get_common_package_datas, get_common_hiddenimports
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
    ] + COMMON_WORKER_DATAS + _master_datas + _icon_datas() + _custom_pack_datas() + get_common_package_datas(),
    hiddenimports=get_common_hiddenimports(),
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
