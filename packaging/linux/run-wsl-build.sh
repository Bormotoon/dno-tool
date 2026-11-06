#!/usr/bin/env bash
# Снимок исходников и контейнерная сборка находятся только в Linux-ФС.
set -euo pipefail
ARCHIVE="${1:?Нужен архив исходников}"
RUN="${2:?Нужен каталог сборки}"
DESTINATION="${3:?Нужен каталог результатов}"
UPDATE="${4:-0}"
JOBS="${5:-4}"
LOG="${6:?Нужен путь журнала на Windows}"
TARGET="${7:?Нужна цель сборки mos12 или mos13}"
REPORT_DESTINATION="${8:?Нужен каталог служебных отчётов на Windows}"
PREPARE_ONLY="${9:-0}"
MASTER_KEY="${10:-0}"

# tee пишет сразу в окончательный журнал, включая подготовку и копирование.
# Сохраняем исходные дескрипторы, чтобы закрыть pipe и дождаться записи до cleanup.
: > "${LOG}"
exec 3>&1 4>&2
exec > >(tee "${LOG}") 2>&1
LOGGER_PID=$!
SAFE_RUN=''
cleanup() {
    local status=$? logger_status=0 cleanup_status=0
    trap - EXIT
    if [ -n "${SAFE_RUN}" ]; then
        printf '\n@@DNO_BUILD_STAGE@@|cleanup\n'
        if [ -f "${SAFE_RUN}/packaging/linux/cleanup-wsl-build.sh" ]; then
            bash "${SAFE_RUN}/packaging/linux/cleanup-wsl-build.sh" || cleanup_status=$?
        fi
        rm -rf -- "${SAFE_RUN}" || cleanup_status=$?
        sync
        fstrim / || echo 'Не удалось выполнить TRIM диска WSL.' >&2
    fi
    exec 1>&3 2>&4
    wait "${LOGGER_PID}" || logger_status=$?
    if [ "${status}" -eq 0 ]; then status=${logger_status}; fi
    if [ "${status}" -eq 0 ]; then status=${cleanup_status}; fi
    exit "${status}"
}
trap cleanup EXIT
printf '@@DNO_BUILD_STAGE@@|sources\n'
source /etc/os-release
test "${ID}:${VERSION_ID}" = ubuntu:24.04
test "$(uname -m)" = x86_64
command -v podman >/dev/null
# Хранилище образов общее: сборка и очистка разных целей не должны пересекаться.
mkdir -p /srv/dno-build/environment /srv/dno-build/environment-mos13
exec 7>/srv/dno-build/maintenance.lock
flock -n 7 || { echo 'Сборочный WSL-стенд уже занят.' >&2; exit 1; }
case "${TARGET}" in
    mos12) ENVIRONMENT_STATE=/srv/dno-build/environment ;;
    mos13) ENVIRONMENT_STATE=/srv/dno-build/environment-mos13 ;;
    *) echo "Неизвестная цель сборки: ${TARGET}" >&2; exit 1 ;;
esac
RUN="$(realpath -m -- "${RUN}")"
if [ "$(dirname -- "${RUN}")" != /srv/dno-build/runs ]; then
    echo 'Недопустимый каталог сборки'; exit 1
fi
SAFE_RUN="${RUN}"
mkdir -p "${RUN}"
tar -C "${RUN}" -xf "${ARCHIVE}"
# Windows-checkout может содержать CRLF даже при корректной .gitattributes.
find "${RUN}/scripts" "${RUN}/packaging/linux" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
source "${RUN}/packaging/linux/build-output.sh"
export DNO_BUILD_PROGRESS=1
export DNO_UPDATE_ENVIRONMENT="${UPDATE}" DNO_BUILD_JOBS="${JOBS}"
export DNO_BUILD_STATE="${ENVIRONMENT_STATE}"
export DNO_BUILD_CACHE="${ENVIRONMENT_STATE}/cache"
export DNO_BUILD_PROJECT_ISOLATED=1
export DNO_BUILD_TARGET="${TARGET}"
export DNO_PREPARE_ONLY="${PREPARE_ONLY}"
# Ключ для варианта -master лежит в корне снимка под именем master-key.pub.
if [ "${MASTER_KEY}" = 1 ]; then
    export DNO_MASTER_KEY="${RUN}/master-key.pub"
else
    export DNO_MASTER_KEY=""
fi
bash "${RUN}/scripts/build_rpm_container.sh"
if [ "${PREPARE_ONLY}" = 1 ]; then
    echo "Стенд ${TARGET} обновлён; пакетная сборка пропущена."
    exit 0
fi
dno_stage copy
for ARTIFACT in "${RUN}/dist/"*.rpm; do
    if [ -f "${ARTIFACT}" ]; then
        cp "${ARTIFACT}" "${DESTINATION}/"
        printf '\n@@DNO_BUILD_ARTIFACT@@|%s\n' "$(basename -- "${ARTIFACT}")"
    fi
done
mkdir -p "${REPORT_DESTINATION}/${TARGET}"
cp "${RUN}/.cache/build-reports/${TARGET}/"*.json "${REPORT_DESTINATION}/${TARGET}/"
