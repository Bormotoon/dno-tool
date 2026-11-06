#!/usr/bin/env bash
# Постоянное окружение Debian 11 и отдельный кандидат для явных обновлений.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "${PROJECT_ROOT}/packaging/linux/build-output.sh"
dno_stage environment
if [ "$(uname -m)" != "x86_64" ]; then
    echo "Ошибка: этот образ сборки рассчитан на x86_64."
    exit 1
fi
if [ -n "${CONTAINER_ENGINE:-}" ]; then
    ENGINE="${CONTAINER_ENGINE}"
elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
else
    echo "Ошибка: установите Podman или Docker."
    exit 1
fi

TARGET="${DNO_BUILD_TARGET:-mos12}"
case "${TARGET}" in
    mos12)
        CONTAINERFILE="Containerfile"
        GLIBC_MAXIMUM="2.33"
        ;;
    mos13)
        CONTAINERFILE="Containerfile.mos13"
        GLIBC_MAXIMUM="2.40"
        ;;
    *) echo "Неизвестная цель сборки: ${TARGET}" >&2; exit 1 ;;
esac

STATE="${DNO_BUILD_STATE:-${HOME}/.local/share/dno-build}"
BUILD_CACHE="${DNO_BUILD_CACHE:-${STATE}/cache}"
mkdir -p "${BUILD_CACHE}" "${PROJECT_ROOT}/.cache" "${PROJECT_ROOT}/dist"
command -v flock >/dev/null || { echo 'Нужен flock (util-linux).'; exit 1; }
# Один писатель защищает переключение образов и общий кэш от гонок.
exec 9>"${STATE}/build.lock"
flock -n 9 || { echo 'На этом стенде уже идёт сборка.'; exit 1; }
STAGING="$(mktemp -d "${PROJECT_ROOT}/.cache/rpm-container.XXXXXX")"
trap 'rm -rf -- "${STAGING}"' EXIT
mkdir -p "${STAGING}/context/packaging" "${STAGING}/context/scripts"
mkdir -p "${STAGING}/context/packaging/linux"
for SCRIPT in build-toolchain.sh fetch_source.py sources.json prepare-runtime.py verify_environment.py write_manifest.py; do
    cp "${PROJECT_ROOT}/packaging/linux/${SCRIPT}" "${STAGING}/context/packaging/linux/"
done
cp "${PROJECT_ROOT}/packaging/linux/${CONTAINERFILE}" \
    "${STAGING}/context/packaging/linux/Containerfile"
cp "${PROJECT_ROOT}/requirements.txt" "${STAGING}/context/"
cp "${PROJECT_ROOT}/scripts/ensure_qt.py" "${STAGING}/context/scripts/"
mkdir -p "${STAGING}/context/packaging/media"
for SCRIPT in linux.py bundle.py qt_codecs.py; do
    cp "${PROJECT_ROOT}/packaging/media/${SCRIPT}" "${STAGING}/context/packaging/media/"
done
# Отпечаток старого формата нужен только для безопасного перехода без пересборки.
LEGACY_RECIPE_HASH="$(cd "${STAGING}/context" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
# Отпечаток включает только файлы, меняющие системные или Python-зависимости.
# Сборочные скрипты проекта, spec, changelog и исходники приложения не влияют на него.
DEPENDENCY_FILES=(
    packaging/linux/build-toolchain.sh packaging/linux/fetch_source.py
    packaging/linux/sources.json packaging/linux/prepare-runtime.py
    packaging/linux/verify_environment.py packaging/linux/write_manifest.py
    packaging/media/linux.py packaging/media/bundle.py packaging/media/qt_codecs.py
    requirements.txt scripts/ensure_qt.py
)
if [ "${TARGET}" = mos13 ]; then
    DEPENDENCY_FILES+=(packaging/linux/Containerfile.mos13)
else
    DEPENDENCY_FILES+=(packaging/linux/Containerfile)
fi
HASH_INPUT="${STAGING}/context/dependency-input"
: > "${HASH_INPUT}"
for FILE in "${DEPENDENCY_FILES[@]}"; do
    if [ -f "${PROJECT_ROOT}/${FILE}" ]; then
        printf '%s\0' "${FILE}" >> "${HASH_INPUT}"
        cat "${PROJECT_ROOT}/${FILE}" >> "${HASH_INPUT}"
        printf '\0' >> "${HASH_INPUT}"
    fi
done
RECIPE_HASH="$(sha256sum "${HASH_INPUT}" | cut -d' ' -f1)"
UPDATE="${DNO_UPDATE_ENVIRONMENT:-0}"
PROMOTE=0
if [ -s "${STATE}/current-environment" ] && [ "${UPDATE}" != 1 ]; then
    IMAGE="$(sed -n '1p' "${STATE}/current-environment")"
    SAVED_HASH="$(sed -n '2p' "${STATE}/current-environment")"
    if [ "${SAVED_HASH}" != "${RECIPE_HASH}" ]; then
        if [ "${SAVED_HASH}" = "${LEGACY_RECIPE_HASH}" ]; then
            printf '%s\n%s\n' "${IMAGE}" "${RECIPE_HASH}" > "${STATE}/current-environment.new"
            mv "${STATE}/current-environment.new" "${STATE}/current-environment"
            echo 'Формат отпечатка стенда обновлён без пересборки зависимостей.'
        else
            echo 'Стенд устарел: изменились зависимости или рецепт окружения. Обновите только этот стенд командой .\\scripts\\update_wsl_environment.ps1 -Target mos12|mos13.' >&2
            exit 1
        fi
    fi
    "${ENGINE}" image inspect "${IMAGE}" >/dev/null
else
    IMAGE="localhost/dno-tool-build:candidate-$(date -u +%Y%m%d%H%M%S)-$$"
    dno_stage environment_update
    # Кэш и исходники живут в Linux-ФС. Предыдущий образ не изменяется.
    "${ENGINE}" build --layers --volume "${BUILD_CACHE}:/cache:Z" \
        --build-arg "BUILD_JOBS=${DNO_BUILD_JOBS:-4}" \
        --build-arg "ENVIRONMENT_REVISION=$(date -u +%Y%m%d%H%M%S)" \
        -t "${IMAGE}" -f "${STAGING}/context/packaging/linux/Containerfile" "${STAGING}/context"
    PROMOTE=1
fi

# WSL уже передал отдельный снимок: второй раз копировать его не нужно.
BUILD_ROOT="${PROJECT_ROOT}"
if [ "${DNO_BUILD_PROJECT_ISOLATED:-0}" != 1 ]; then
    BUILD_ROOT="${STAGING}/project"
    mkdir -p "${BUILD_ROOT}"
    # Файлы мастер-ключа из корня проекта не копируются; ключ из DNO_MASTER_KEY
    # кладётся в снимок ниже под именем master-key.pub.
    MASTER_EXCLUDE=(--exclude='./master-key.key' --exclude='./master-key.pub')
    tar -C "${PROJECT_ROOT}" --exclude='./.git' --exclude='./.venv' \
    --exclude='./.cache' --exclude='./dist' --exclude='./build' \
    --exclude='./rpmbuild' --exclude='./tests' --exclude='./.codex' \
    "${MASTER_EXCLUDE[@]}" \
    --exclude='./.agents' --exclude='./.aws' --exclude='./build*.log' --exclude='./build.exit*' \
    --exclude='__pycache__' --exclude='*.pyc' \
    -cf - . | tar -C "${BUILD_ROOT}" -xf -
fi
# Вариант со служебным мастер-ключом: файл в корне сборки, путь внутри контейнера.
CONTAINER_MASTER_KEY=""
if [ -n "${DNO_MASTER_KEY:-}" ] && [ "${DNO_MASTER_KEY}" != 0 ]; then
    MASTER_SOURCE="$(realpath -- "${DNO_MASTER_KEY}")"
    if [ ! -f "${MASTER_SOURCE}" ]; then
        echo "Ошибка: нет файла открытой части мастер-ключа: ${DNO_MASTER_KEY}" >&2
        exit 1
    fi
    if [ "${MASTER_SOURCE}" != "$(realpath -m -- "${BUILD_ROOT}/master-key.pub")" ]; then
        cp -- "${MASTER_SOURCE}" "${BUILD_ROOT}/master-key.pub"
    fi
    CONTAINER_MASTER_KEY="/work/master-key.pub"
fi
"${ENGINE}" run --pull=never --rm --network=none \
    -e "DNO_BUILD_PROGRESS=${DNO_BUILD_PROGRESS:-0}" \
    -e "DNO_PACKAGE_TARGET=${TARGET}" -e "DNO_GLIBC_MAXIMUM=${GLIBC_MAXIMUM}" \
    -e "DNO_MASTER_KEY=${CONTAINER_MASTER_KEY}" \
    -v "${BUILD_ROOT}:/work:Z" "${IMAGE}"
dno_stage environment_verify
"${ENGINE}" run --rm --network=none -e QT_QPA_PLATFORM=offscreen -e QT_MEDIA_BACKEND=ffmpeg \
    "${IMAGE}" /opt/dno/.venv/bin/python /opt/recipe/verify_environment.py
REPORT_DIR="${BUILD_ROOT}/.cache/build-reports/${TARGET}"
mkdir -p "${REPORT_DIR}"
"${ENGINE}" image inspect "${IMAGE}" > "${REPORT_DIR}/container-report-${TARGET}.json"
if [ "${BUILD_ROOT}" != "${PROJECT_ROOT}" ]; then
    for ARTIFACT in "${BUILD_ROOT}/dist/"*.rpm; do
        [ -f "${ARTIFACT}" ] && cp "${ARTIFACT}" "${PROJECT_ROOT}/dist/"
    done
    mkdir -p "${PROJECT_ROOT}/.cache/build-reports/${TARGET}"
    cp "${REPORT_DIR}/"*.json "${PROJECT_ROOT}/.cache/build-reports/${TARGET}/"
fi
if [ "${PROMOTE}" = 1 ]; then
    # Переключение выполняется после сборки приложения, ELF-аудита и RPM-проверок.
    if [ -s "${STATE}/current-environment" ]; then
        cp "${STATE}/current-environment" "${STATE}/previous-environment"
    fi
    printf '%s\n%s\n' "${IMAGE}" "${RECIPE_HASH}" > "${STATE}/current-environment.new"
    mv "${STATE}/current-environment.new" "${STATE}/current-environment"
fi
if [ "${DNO_PREPARE_ONLY:-0}" = 1 ]; then
    echo "Стенд ${TARGET} подготовлен (образ ${IMAGE}); пакетная сборка пропущена."
    exit 0
fi
echo "RPM: ${PROJECT_ROOT}/dist/; отчёты: ${PROJECT_ROOT}/.cache/build-reports/${TARGET}/"
