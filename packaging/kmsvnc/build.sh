#!/bin/bash
# Сборка RPM kmsvnc для МОС 12 и МОС 13 в контейнере ROSA 2021.1 (база МОС 12).
#
#   bash packaging/kmsvnc/build.sh [каталог_вывода]
#
# Из Windows — scripts/build_kmsvnc.ps1 (WSL-стенд dno-build). Нужен podman
# или docker. Пакет собирается с glibc 2.33 и библиотеками МОС 12: он
# ставится и на МОС 12, и на МОС 13 (sonames библиотек совпадают). Пакеты
# для сборки берутся из зеркала МОС (os.mos.ru), исходники — из закреплённого
# коммита форка или локального клона src/.
set -euo pipefail

# KMSVNC_PACKAGING — каталог packaging/kmsvnc, если скрипт запущен не из него.
HERE=${KMSVNC_PACKAGING:-$(cd "$(dirname "$0")" && pwd)}
OUT=$(realpath -m -- "${1:-"$HERE/dist"}")
IMAGE=docker.io/rosalab/rosa2021.1@sha256:2be149fd2f4e00bb4da5c3ea7ce600bc29e47d5a6f21e28b54ffd7896dbf899e
MIRROR=${KMSVNC_MIRROR:-http://os.mos.ru/repo/r/rosa2021.1/base/release/repository/x86_64}
COMMIT=$(sed -n 's/^%global commit[[:space:]]*//p' "$HERE/kmsvnc.spec")
TARBALL="kmsvnc-$COMMIT.tar.gz"
SOURCE_COMMIT=9a18e250e7d4063da65f51b39aebe4b50a401088
SOURCE_SHA256=7456af5f4c00c013bc3cf57cc00a706059a1b123d9a985d24f3cb15fe70f80c8
SOURCE_URL="https://codeload.github.com/xp9k/kmsvnc/tar.gz/$SOURCE_COMMIT"

if command -v podman >/dev/null; then ENGINE=podman
elif command -v docker >/dev/null; then ENGINE=docker
else echo "Нужен podman или docker" >&2; exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/SOURCES" "$OUT"
SOURCE_DIR="$HERE/src"
if [ ! -f "$SOURCE_DIR/CMakeLists.txt" ]; then
    curl -fsSL --retry 2 -o "$WORK/source.tar.gz" "$SOURCE_URL"
    echo "$SOURCE_SHA256  $WORK/source.tar.gz" | sha256sum -c --quiet -
    mkdir "$WORK/source"
    tar -xzf "$WORK/source.tar.gz" --no-same-owner -C "$WORK/source"
    SOURCE_DIR="$WORK/source/kmsvnc-$SOURCE_COMMIT"
fi
test -f "$SOURCE_DIR/CMakeLists.txt"
tar -C "$SOURCE_DIR" --exclude=.git --transform "s,^,kmsvnc-$COMMIT/," \
    -czf "$WORK/SOURCES/$TARBALL" .
# Windows-checkout может дать CRLF, rpmbuild его не принимает.
sed 's/\r$//' "$HERE/kmsvnc.spec" > "$WORK/kmsvnc.spec"

"$ENGINE" run --rm -i -v "$WORK:/work:Z" -v "$OUT:/out:Z" -e MIRROR="$MIRROR" "$IMAGE" bash -s <<'INNER'
set -euo pipefail
rm -f /etc/yum.repos.d/*.repo
for repo in main contrib; do
    cat > /etc/yum.repos.d/mos12-$repo.repo <<REPO
[mos12-$repo]
name=MOS 12 (ROSA 2021.1) $repo
baseurl=$MIRROR/$repo/release/
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-ROSA
REPO
done
# Образ новее репозитория МОС 12: выравниваем версии, иначе, например,
# libmagic не читает базу file и rpmbuild падает.
dnf -y -q --allowerasing distro-sync
dnf -y -q --setopt=install_weak_deps=False install rpm-build gcc make cmake \
    'pkgconfig(libdrm)' 'pkgconfig(libvncserver)' 'pkgconfig(xkbcommon)' \
    'pkgconfig(libva)' 'pkgconfig(libva-drm)' \
    'pkgconfig(egl)' 'pkgconfig(glesv2)' 'pkgconfig(gbm)' 'pkgconfig(json-c)'
echo "Окружение: $(getconf GNU_LIBC_VERSION), $(rpm -q lib64vncserver-devel lib64va-devel)"
rpmbuild -ba --define '_topdir /work' --define '_sourcedir /work/SOURCES' /work/kmsvnc.spec
find /work/RPMS /work/SRPMS -name '*.rpm' -exec cp {} /out/ \;
INNER
ls -1 "$OUT"/kmsvnc-*.rpm
