#!/usr/bin/env bash
# Все зависимости готовы; компилируются только привязки PySide6/Shiboken.
set -euo pipefail
JOBS="${BUILD_JOBS:-4}"
[[ "${JOBS}" =~ ^[1-8]$ ]] || { echo 'BUILD_JOBS: ожидается 1..8'; exit 1; }
export CMAKE_BUILD_PARALLEL_LEVEL="${JOBS}"
mkdir -p /cache/{sources,wheels,ccache,pip} /opt/dno/wheels /opt/manifest

case "${1:?Укажите этап}" in
pyside)
    ARCHIVE="$(python /opt/recipe/fetch_source.py pyside)"
    mkdir -p /tmp/dno-pyside
    tar -xf "${ARCHIVE}" --strip-components=1 -C /tmp/dno-pyside
    cp "${ARCHIVE}.json" /opt/manifest/pyside-source.json
    cd /tmp/dno-pyside
    # Для выбранного набора модулей необязательный плагин Designer может отсутствовать.
    python - <<'PY'
from pathlib import Path
packager = Path("build_scripts/platforms/linux.py")
text = packager.read_text(encoding="utf-8")
old = '''copydir("{install_dir}/plugins/designer",
                    plugins_target / "designer",
                    _filter=["*.so"],
                    recursive=False,
                    _vars=_vars)'''
assert text.count(old) == 1, "Изменился блок упаковки необязательного Designer"
packager.write_text(text.replace(old, old.replace("recursive=False,", "recursive=False, force=False,")), encoding="utf-8")
PY
    python -m venv /opt/pyside-build
    source /opt/pyside-build/bin/activate
    python -m pip install --only-binary=:all: --upgrade pip setuptools wheel packaging
    export LLVM_INSTALL_DIR=/opt/native CLANG_INSTALL_DIR=/opt/native
    export CMAKE_PREFIX_PATH=/opt/native
    export CPATH="/opt/native/include${CPATH:+:${CPATH}}"
    export CPLUS_INCLUDE_PATH="/opt/native/include${CPLUS_INCLUDE_PATH:+:${CPLUS_INCLUDE_PATH}}"
    export CMAKE_CXX_COMPILER_LAUNCHER=ccache CMAKE_C_COMPILER_LAUNCHER=ccache
    QT_PATHS="$(command -v qtpaths6 || command -v qtpaths || find /opt/native -type f -name qtpaths -print -quit)"
    test -n "${QT_PATHS}" || { echo 'В готовом Qt не найден qtpaths' >&2; exit 1; }
    "${QT_PATHS}" --qt-version
    python setup.py bdist_wheel --qtpaths="${QT_PATHS}" \
        --ignore-git --parallel="${JOBS}" --standalone --limited-api=yes \
        --module-subset=Core,Gui,Widgets,Network,DBus,OpenGL,OpenGLWidgets,Svg,SvgWidgets,Qml,Quick,QuickWidgets,Multimedia,MultimediaWidgets,Test,PrintSupport \
        --verbose-build
    cp dist/*.whl /opt/dno/wheels/
    cp dist/*.whl /cache/wheels/
    deactivate
    cd /
    rm -rf /tmp/dno-pyside /opt/pyside-build
    ;;
application)
    python -m venv /opt/dno/.venv
    source /opt/dno/.venv/bin/activate
    python -m pip install --only-binary=:all: --upgrade pip setuptools wheel
    python -m pip install --no-index --find-links=/opt/dno/wheels PySide6==6.12.0 shiboken6==6.12.0
    printf 'PySide6==6.12.0\nshiboken6==6.12.0\n' > /opt/dno/constraints.txt
    # Для нового Python PyYAML пока доступен без wheel: упаковываем чистый Python без C-ускорителя.
    PYYAML_FORCE_LIBYAML=0 python -m pip wheel --no-deps --no-build-isolation \
        --no-binary=PyYAML --wheel-dir=/opt/dno/wheels PyYAML
    # requirements.txt остаётся источником зависимостей приложения.
    python -m pip download --only-binary=:all: --dest=/opt/dno/wheels --find-links=/opt/dno/wheels \
        --constraint=/opt/dno/constraints.txt -r /opt/recipe/app/requirements.txt pyinstaller
    python -m pip install --only-binary=:all: --no-index --find-links=/opt/dno/wheels \
        --constraint=/opt/dno/constraints.txt -r /opt/recipe/app/requirements.txt pyinstaller
    # Собирается поддерживаемый PyAV против SDK того же готового FFmpeg.
    PYAV_ARCHIVE="$(python /opt/recipe/fetch_source.py av)"
    cp "${PYAV_ARCHIVE}.json" /opt/manifest/pyav-source.json
    PKG_CONFIG_PATH=/opt/native/lib/pkgconfig python -m pip install --force-reinstall --no-deps "${PYAV_ARCHIVE}"
    python /opt/recipe/media/linux.py --prefix /opt/native --runtime /opt/native \
        --pyside /opt/dno/.venv/lib/python3.15/site-packages/PySide6
    python -m pip check
    python /opt/recipe/app/ensure_qt.py --requirements /opt/recipe/app/requirements.txt
    QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg python /opt/recipe/verify_environment.py
    python -m pip freeze --all > /opt/manifest/requirements.lock
    cp /opt/dno/wheels/*.whl /cache/wheels/
    python /opt/recipe/write_manifest.py
    deactivate
    ;;
*) echo "Неизвестный этап: $1" >&2; exit 1 ;;
esac
