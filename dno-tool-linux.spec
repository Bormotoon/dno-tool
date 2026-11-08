# -*- mode: python ; coding: utf-8 -*-
# dno-tool-linux.spec — Linux-сборка в каталог: приложение, lib/ и assets/.
#
# Отличия от dno-tool.spec (Windows, везет все zip внутри):
# - темы assets/icons/*.zip НЕ пакуются (только themes.json + корень без zip);
# - custom-паки assets/custom/*.zip НЕ пакуются;
# - zip лежат рядом с бинарником в /opt/dno-tool/assets/ (см. scripts/build_rpm.sh),
#   рантайм находит их через icon_theme_loader._external_assets_base().
#
# Остальное (hiddenimports, themes, web/static) - один в один с dno-tool.spec.
# При добавлении hiddenimports править ОБА spec.

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


a = Analysis(
    ['__main__.py'],
    pathex=[],
    binaries=_media_binaries,
    datas=_media_datas + COMMON_WORKER_DATAS + _master_datas + get_common_package_datas(),
    hiddenimports=get_common_hiddenimports(),
    hookspath=["packaging/media/hooks"],
    hooksconfig={},
    runtime_hooks=["packaging/media/runtime_hook.py"],
    excludes=[],
    noarchive=False,
    optimize=0,
)
a.binaries = unify_media_binaries(a.binaries, include_media=True)
a.datas = [entry for entry in clean_media_aliases(a.datas, keep_resources=True)
           if not entry[0].replace('\\', '/').startswith('assets/')]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    contents_directory='lib',
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
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name='dno-tool')
