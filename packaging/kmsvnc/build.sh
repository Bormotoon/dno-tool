#!/bin/bash
# Сборка RPM kmsvnc для МОС 12 и МОС 13 в контейнере ROSA 2021.1 (база МОС 12).
#
#   bash packaging/kmsvnc/build.sh [каталог_вывода]
#
# Из Windows — scripts/build_kmsvnc.ps1 (WSL-стенд dno-build). Нужен podman
# или docker. Пакет собирается с glibc 2.33 и библиотеками МОС 12: он
# ставится и на МОС 12, и на МОС 13 (sonames библиотек совпадают). Пакеты
# для сборки берутся из зеркала МОС (os.mos.ru), архив исходников — рядом
# со скриптом или с GitHub, сверяется по SHA-256.
set -euo pipefail

# KMSVNC_PACKAGING — каталог packaging/kmsvnc, если скрипт запущен не из него.
HERE=${KMSVNC_PACKAGING:-$(cd "$(dirname "$0")" && pwd)}
OUT=$(realpath -m -- "${1:-"$HERE/dist"}")
IMAGE=docker.io/rosalab/rosa2021.1@sha256:2be149fd2f4e00bb4da5c3ea7ce600bc29e47d5a6f21e28b54ffd7896dbf899e
MIRROR=${KMSVNC_MIRROR:-http://os.mos.ru/repo/r/rosa2021.1/base/release/repository/x86_64}
COMMIT=$(sed -n 's/^%global commit[[:space:]]*//p' "$HERE/kmsvnc.spec")
SHA256=c1bed53c23daecb8cf0d37519180dbbb745d9ef32ef01955a28da9c7723356e3
TARBALL="kmsvnc-$COMMIT.tar.gz"

if command -v podman >/dev/null; then ENGINE=podman
elif command -v docker >/dev/null; then ENGINE=docker
else echo "Нужен podman или docker" >&2; exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/SOURCES" "$OUT"
if [ -f "$HERE/$TARBALL" ]; then
    cp "$HERE/$TARBALL" "$WORK/SOURCES/"
else
    curl -fsSL -o "$WORK/SOURCES/$TARBALL" \
        "https://github.com/isjerryxiao/kmsvnc/archive/$COMMIT/$TARBALL"
fi
echo "$SHA256  $WORK/SOURCES/$TARBALL" | sha256sum -c --quiet -
cp "$HERE/kmsvnc-abgr-formats.patch" "$WORK/SOURCES/"
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
    'pkgconfig(libva)' 'pkgconfig(libva-drm)'
echo "Окружение: $(getconf GNU_LIBC_VERSION), $(rpm -q lib64vncserver-devel lib64va-devel)"
rpmbuild -ba --define '_topdir /work' --define '_sourcedir /work/SOURCES' /work/kmsvnc.spec
find /work/RPMS /work/SRPMS -name '*.rpm' -exec cp {} /out/ \;
INNER
ls -1 "$OUT"/kmsvnc-*.rpm
