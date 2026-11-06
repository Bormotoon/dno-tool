#!/usr/bin/env bash
# build_rpm.sh — сборка RPM-пакета dno-tool для ROSA / МОС Linux
# Запуск из корня проекта: bash scripts/build_rpm.sh
# Со служебным мастер-ключом нод: DNO_MASTER_KEY=<файл .pub> bash scripts/build_rpm.sh
# (ключ кладётся в сборку как master-key.pub, пакет получает суффикс -master).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
source "${PROJECT_ROOT}/packaging/linux/build-output.sh"
dno_stage application
PACKAGING_DIR="${PROJECT_ROOT}/packaging"

INIT_FILE="${PROJECT_ROOT}/src/__init__.py"
if [ ! -f "${INIT_FILE}" ]; then
    echo "Ошибка: файл не найден: ${INIT_FILE}"
    echo "PROJECT_ROOT=${PROJECT_ROOT}"
    echo "SCRIPT_DIR=${SCRIPT_DIR}"
    ls -la "${PROJECT_ROOT}/" 2>/dev/null || echo "(каталог не найден)"
    exit 1
fi

VERSION=$(grep -m1 '__version__.*=.*"' "${INIT_FILE}" | cut -d'"' -f2)
if [ -z "${VERSION}" ]; then
    VERSION=$(grep -m1 "__version__.*=.*'" "${INIT_FILE}" | cut -d"'" -f2)
fi

if [ -z "${VERSION}" ]; then
    echo "Ошибка: не удалось определить версию из src/__init__.py"
    exit 1
fi

echo "=== Сборка RPM-пакета dno-tool-${VERSION} ==="
MASTER_SUFFIX=""
if [ -n "${DNO_MASTER_KEY:-}" ] && [ "${DNO_MASTER_KEY}" != 0 ]; then
    # Абсолютный путь: дальше скрипт меняет каталог.
    if [ ! -f "${DNO_MASTER_KEY}" ]; then
        echo "Ошибка: нет файла открытой части мастер-ключа: ${DNO_MASTER_KEY}" >&2
        exit 1
    fi
    DNO_MASTER_KEY="$(realpath -- "${DNO_MASTER_KEY}")"
    export DNO_MASTER_KEY
    MASTER_SUFFIX="-master"
    echo "Вариант: со служебным мастер-ключом (${DNO_MASTER_KEY})"
else
    export DNO_MASTER_KEY=""
    echo "Вариант: без служебного мастер-ключа"
fi

PACKAGE_TARGET="${DNO_PACKAGE_TARGET:-generic}"
REPORT_DIR="${PROJECT_ROOT}/.cache/build-reports/${PACKAGE_TARGET}"
mkdir -p "${REPORT_DIR}"
GLIBC_MAXIMUM="${DNO_GLIBC_MAXIMUM:-2.33}"
if [[ ! "${GLIBC_MAXIMUM}" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Ошибка: неверный предел glibc: ${GLIBC_MAXIMUM}" >&2
    exit 1
fi
case "${PACKAGE_TARGET}" in
    generic|mos12|mos13) ;;
    *) echo "Ошибка: неизвестная цель пакета: ${PACKAGE_TARGET}" >&2; exit 1 ;;
esac

# ── 0. Проверка и установка зависимостей ──────────────────────────────────
# Проверяем инструменты, а не RPM-базу: стенд может работать на Debian.
for tool in rpmbuild rpm python3 readelf tar gzip; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "Не найден ${tool}. Debian: apt-get install rpm binutils python3 python3-venv. МОС: dnf install rpm-build binutils python3."
        exit 1
    fi
done

# ── 0.5. Проверка наличия необходимых файлов проекта ─────────────────────
POLICYKIT_DIR="${PACKAGING_DIR}/policykit"
if [ ! -d "${POLICYKIT_DIR}" ]; then
    echo "Ошибка: каталог policykit не найден: ${POLICYKIT_DIR}"
    exit 1
fi

for f in com.dno-tool.policy com.dno-tool.desktop com.dno-tool-admin.desktop dno-tool-admin; do
    if [ ! -f "${POLICYKIT_DIR}/${f}" ]; then
        echo "Ошибка: файл не найден: ${POLICYKIT_DIR}/${f}"
        exit 1
    fi
done

# ── 1. Собираем бинарный файл через PyInstaller ──────────────────────────
echo "--- Шаг 1: Сборка бинарного файла ---"
cd "${PROJECT_ROOT}"

BUILD_VENV="${DNO_BUILD_VENV:-${PROJECT_ROOT}/.venv}"
if [ ! -d "${BUILD_VENV}" ]; then
    echo "Создание виртуального окружения..."
    python3 -m venv "${BUILD_VENV}"
fi

source "${BUILD_VENV}/bin/activate"
BUILD_GLIBC_VERSION=$(getconf GNU_LIBC_VERSION | awk '{print $2}')
if [[ ! "${BUILD_GLIBC_VERSION}" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Ошибка: не удалось определить версию glibc для зависимостей RPM"
    exit 1
fi
echo "Сборочная glibc: ${BUILD_GLIBC_VERSION}; точные требования проверяются после упаковки."
# Изолированный проверочный checkout может использовать готовое окружение
# сборочного стенда без обновления пакетов в общем виртуальном окружении.
if [ "${DNO_SKIP_DEPENDENCY_INSTALL:-0}" != "1" ]; then
    python -m pip install --upgrade pip -q
    python scripts/ensure_qt.py --update
    python -m pip install -r requirements.txt -q
    python -m pip install pyinstaller -q
else
    # Запрет установки сохраняется, но несовместимую Qt упаковывать нельзя.
    python scripts/ensure_qt.py
fi
python packaging/media/qt_codecs.py
# Нативные библиотеки находятся в lib/, темы и иконки — в assets/.
dno_stage pyinstaller
pyinstaller dno-tool-linux.spec
python packaging/master_key.py check dist/dno-tool
mkdir -p dist/dno-tool/assets
cp -a assets/themes dist/dno-tool/assets/
dno_stage media
python packaging/media/verify.py audit dist/dno-tool/dno-tool --report "${REPORT_DIR}/media-package-report-${PACKAGE_TARGET}.json"
python packaging/media/verify.py smoke dist/dno-tool/dno-tool --report "${REPORT_DIR}/media-runtime-report-${PACKAGE_TARGET}.json"
# Настоящий X11-сервер проверяет инициализацию клавиатуры, которую offscreen не покрывает.
xvfb-run -a python packaging/media/verify.py smoke dist/dno-tool/dno-tool --platform xcb --report "${REPORT_DIR}/x11-runtime-report-${PACKAGE_TARGET}.json"

# Проверяем загрузчик и все нативные библиотеки каталога lib/.
dno_stage elf
ELF_REPORT="${REPORT_DIR}/elf-report-${PACKAGE_TARGET}.json"
python packaging/linux/audit_elf.py dist/dno-tool/dno-tool --maximum "${GLIBC_MAXIMUM}" --report "${ELF_REPORT}"
BUILD_GLIBC_VERSION=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["maximum_required"])' "${ELF_REPORT}")
deactivate

BINARY="${PROJECT_ROOT}/dist/dno-tool/dno-tool"
if [ ! -f "${BINARY}" ]; then
    echo "Ошибка: бинарный файл не найден в dist/"
    exit 1
fi
echo "Бинарный файл: ${BINARY}"
dno_stage rpm

# ── 2. Подготавливаем иконки ─────────────────────────────────────────────
echo "--- Шаг 2: Подготовка иконок ---"
ICON_DIR="${PROJECT_ROOT}/rpmbuild/_icons"
mkdir -p "${ICON_DIR}"

cp "${PROJECT_ROOT}/assets/icons/icon.png" "${ICON_DIR}/dno-tool.png"
HAS_PNG=true

SVG_SRC="${PROJECT_ROOT}/assets/icons/icon.svg"
HAS_SVG=true
if [ ! -f "${SVG_SRC}" ]; then
    echo "Предупреждение: SVG иконка не найдена (${SVG_SRC})"
    HAS_SVG=false
fi

# ── 2.5. Проверка внешних zip иконок (в /opt, не в бинарнике) ──────────
echo "--- Шаг 2.5: Проверка внешних zip иконок ---"
ICON_ZIPS=(badge classic flat line midnight neon paper solid)
for z in "${ICON_ZIPS[@]}"; do
    if [ ! -f "${PROJECT_ROOT}/assets/icons/${z}.zip" ]; then
        echo "Ошибка: нет темы assets/icons/${z}.zip (соберите: python scripts/gen_icon_themes.py)"
        exit 1
    fi
done
for p in mono color; do
    if [ ! -f "${PROJECT_ROOT}/assets/custom/${p}.zip" ]; then
        echo "Ошибка: нет пака assets/custom/${p}.zip"
        exit 1
    fi
done
if [ ! -f "${PROJECT_ROOT}/assets/icons/themes.json" ]; then
    echo "Ошибка: нет assets/icons/themes.json"
    exit 1
fi

# ── 3. Формируем исходный архив для rpmbuild ────────────────────────────
echo "--- Шаг 3: Подготовка исходного архива ---"
RPMBUILD="${PROJECT_ROOT}/rpmbuild"
SOURCES="${RPMBUILD}/SOURCES/dno-tool-${VERSION}"

rm -rf "${SOURCES}"
mkdir -p "${SOURCES}/policykit" "${SOURCES}/icon"
mkdir -p "${SOURCES}/opt/dno-tool/assets/icons" "${SOURCES}/opt/dno-tool/assets/custom" "${SOURCES}/opt/dno-tool/assets/remote"

cp -a "${PROJECT_ROOT}/dist/dno-tool/." "${SOURCES}/opt/dno-tool/"
chmod +x "${SOURCES}/opt/dno-tool/dno-tool"

# Внешние zip иконок: темы + custom-паки (в бинарник не пакуются).
cp "${PROJECT_ROOT}"/assets/icons/*.zip "${SOURCES}/opt/dno-tool/assets/icons/"
cp "${PROJECT_ROOT}/assets/icons/themes.json" "${SOURCES}/opt/dno-tool/assets/icons/"
for f in favicon.ico toolbar_overflow.svg icon.png icon.svg; do
    if [ -f "${PROJECT_ROOT}/assets/icons/${f}" ]; then
        cp "${PROJECT_ROOT}/assets/icons/${f}" "${SOURCES}/opt/dno-tool/assets/icons/"
    fi
done
cp "${PROJECT_ROOT}/assets/custom/mono.zip" "${SOURCES}/opt/dno-tool/assets/custom/"
cp "${PROJECT_ROOT}/assets/custom/color.zip" "${SOURCES}/opt/dno-tool/assets/custom/"
# Пакеты для хостов, которые ставит мастер настройки (kmsvnc).
cp "${PROJECT_ROOT}"/assets/remote/*.rpm "${SOURCES}/opt/dno-tool/assets/remote/"

cp "${POLICYKIT_DIR}/com.dno-tool.policy" "${SOURCES}/policykit/"
cp "${POLICYKIT_DIR}/com.dno-tool.desktop" "${SOURCES}/policykit/"
cp "${POLICYKIT_DIR}/com.dno-tool-admin.desktop" "${SOURCES}/policykit/"
cp "${POLICYKIT_DIR}/dno-tool-admin" "${SOURCES}/policykit/"

if ${HAS_PNG}; then
    cp "${ICON_DIR}/dno-tool.png" "${SOURCES}/icon/"
fi
if ${HAS_SVG}; then
    cp "${SVG_SRC}" "${SOURCES}/icon/dno-tool.svg"
fi

# ── 4. Создаём tar.gz ────────────────────────────────────────────────────
echo "--- Шаг 4: Создание tar.gz архива ---"
cd "${RPMBUILD}/SOURCES"
rm -f "dno-tool-${VERSION}.tar.gz"
tar -czf "dno-tool-${VERSION}.tar.gz" "dno-tool-${VERSION}"

# ── 5. Копируем и обновляем spec-файл ────────────────────────────────────
echo "--- Шаг 5: Подготовка spec-файла ---"
mkdir -p "${RPMBUILD}/SPECS"
cp "${PACKAGING_DIR}/rpm/dno-tool.spec" "${RPMBUILD}/SPECS/dno-tool.spec"
sed -i "s/^Version:.*/Version:        ${VERSION}/" "${RPMBUILD}/SPECS/dno-tool.spec"
if [ "${PACKAGE_TARGET}" != generic ]; then
    sed -i "s/^Release:.*/Release:        1.${PACKAGE_TARGET}/" "${RPMBUILD}/SPECS/dno-tool.spec"
fi
# Дополняем требования RPM результатом аудита всего поставляемого lib/.
sed -i "/^Requires:.*polkit/a Requires:       glibc >= ${BUILD_GLIBC_VERSION}" "${RPMBUILD}/SPECS/dno-tool.spec"

# Убираем строки с иконками из spec, если файлов нет
if ! ${HAS_PNG}; then
    sed -i '/icon\/dno-tool.png/d' "${RPMBUILD}/SPECS/dno-tool.spec"
fi
if ! ${HAS_SVG}; then
    sed -i '/icon\/dno-tool.svg/d' "${RPMBUILD}/SPECS/dno-tool.spec"
fi

# ── 6. Собираем RPM ──────────────────────────────────────────────────────
echo "--- Шаг 6: Сборка RPM-пакета ---"
mkdir -p "${RPMBUILD}"/{BUILD,RPMS,SRPMS}
rm -f "${RPMBUILD}"/RPMS/x86_64/dno-tool-*.rpm

# Копируем rpmlintrc чтобы rpmlint не валил на предсобранный бинарник
if [ -f "${PACKAGING_DIR}/rpm/dno-tool.rpmlintrc" ]; then
    cp "${PACKAGING_DIR}/rpm/dno-tool.rpmlintrc" "${RPMBUILD}/SOURCES/dno-tool.rpmlintrc"
fi

rpmbuild -bb "${RPMBUILD}/SPECS/dno-tool.spec" \
    --define "_topdir ${RPMBUILD}" \
    --define "_binary_filedigest_algorithm 8" \
    --define "_unpackaged_files_terminate_build 0"

RPM_FILE=$(find "${RPMBUILD}/RPMS" -name "dno-tool-${VERSION}-*.rpm" -type f 2>/dev/null | head -1)
if [ -z "${RPM_FILE}" ]; then
    echo "Ошибка: RPM-пакет не найден после сборки!"
    ls -laR "${RPMBUILD}/RPMS/"
    exit 1
fi

mkdir -p "${PROJECT_ROOT}/dist"
RPM_BASENAME=$(basename "${RPM_FILE}")
case "${RPM_BASENAME}" in
    *.x86_64.rpm) ;;
    *) echo "Ошибка: для Linux-пакета ожидалась архитектура x86_64: ${RPM_BASENAME}" >&2; exit 1 ;;
esac
if [ "${PACKAGE_TARGET}" = generic ]; then
    RPM_DIST_BASENAME="${RPM_BASENAME%.x86_64.rpm}-linux-x86_64${MASTER_SUFFIX}.rpm"
else
    RPM_DIST_BASENAME="dno-tool-${VERSION}-1-${PACKAGE_TARGET}-x86_64${MASTER_SUFFIX}.rpm"
fi
RPM_DIST_FILE="${PROJECT_ROOT}/dist/${RPM_DIST_BASENAME}"
cp "${RPM_FILE}" "${RPM_DIST_FILE}"

if [ -f /opt/manifest/environment-manifest.json ]; then
    cp /opt/manifest/environment-manifest.json "${REPORT_DIR}/environment-manifest-${PACKAGE_TARGET}.json"
fi
RPM_REPORT="${REPORT_DIR}/rpm-report-${PACKAGE_TARGET}.json"
dno_stage rpm_verify
"${BUILD_VENV}/bin/python" "${PROJECT_ROOT}/packaging/linux/verify_rpm.py" \
    "${RPM_DIST_FILE}" --binary "${BINARY}" \
    --glibc "${BUILD_GLIBC_VERSION}" --report "${RPM_REPORT}"

echo ""
echo "============================================"
echo "  RPM-пакет собран успешно!"
echo "============================================"
echo "  Исходный RPM: ${RPM_FILE}"
echo "  Пакет Linux:  ${RPM_DIST_FILE}"
echo ""
echo "  Установка:"
echo "    sudo dnf install ${RPM_DIST_FILE}"
echo ""
echo "  Проверка содержимого:"
echo "    rpm -qlp ${RPM_DIST_FILE}"
echo ""
