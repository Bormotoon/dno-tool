#!/usr/bin/env bash
# Очистка выделенного WSL-стенда с сохранением действующих окружений обеих целей.
set -euo pipefail

ROOT=/srv/dno-build
exec 9>"${ROOT}/environment/build.lock"
exec 8>"${ROOT}/environment-mos13/build.lock"
flock -n 9
flock -n 8
if [ -n "$(podman ps -aq)" ]; then
    echo 'Очистка пропущена: на стенде остались контейнеры Podman.' >&2
    exit 1
fi
# Удаляем остатки Buildah до создания контейнеров, защищающих рабочие образы.
buildah rm --all

KEEPERS=()
release_keepers() {
    local status=$?
    trap - EXIT
    for name in "${KEEPERS[@]}"; do
        podman rm "${name}" || status=1
    done
    exit "${status}"
}
trap release_keepers EXIT
for target in mos12 mos13; do
    state="${ROOT}/environment"
    [ "${target}" = mos12 ] || state="${ROOT}/environment-mos13"
    [ -s "${state}/current-environment" ] || continue
    image="$(head -n 1 "${state}/current-environment")"
    podman image inspect "${image}" >/dev/null
    name="dno-build-cleanup-${target}-$$"
    podman create --name "${name}" "${image}" /bin/true
    KEEPERS+=("${name}")
done

# Контейнеры KEEPERS защищают текущие образы и их общие слои от prune.
podman image prune --all --force
for path in "${ROOT}/environment/cache" "${ROOT}/environment-mos13/cache" /var/tmp/buildah[0-9]*; do
    [ -e "${path}" ] || continue
    resolved="$(realpath -e -- "${path}")"
    case "${resolved}" in
        "${ROOT}/environment/cache"|"${ROOT}/environment-mos13/cache") ;;
        /var/tmp/buildah*)
            [[ "${resolved}" =~ ^/var/tmp/buildah[0-9]+$ ]] || exit 1
            ;;
        *) echo "Небезопасный путь очистки: ${resolved}" >&2; exit 1 ;;
    esac
    [ ! -L "${path}" ] || exit 1
    rm -rf -- "${resolved}"
done
for state in "${ROOT}/environment" "${ROOT}/environment-mos13"; do
    mkdir -p "${state}/cache"
    rm -f -- "${state}/previous-environment"
done
echo 'Очистка WSL завершена: сохранены действующие окружения MOS12/MOS13.'
