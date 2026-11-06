#!/usr/bin/env bash
# Этот скрипт вызывается от root внутри выделенной Ubuntu WSL.
set -euo pipefail
source /etc/os-release
test "${ID}:${VERSION_ID}" = ubuntu:24.04
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y podman rsync ca-certificates
mkdir -p /srv/dno-build
podman info > /srv/dno-build/podman-info.txt
