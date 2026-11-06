# kmsvnc для МОС: VNC-сервер, читающий кадр напрямую из DRM/KMS.
# Работает в X11, Wayland (KWin, GNOME) и на экране входа без портала.
# Сборка: packaging/kmsvnc/build.sh (окружение МОС 12, пакет для МОС 12 и 13).
# Пакет содержит только программу: службу, пароль и порт настраивает
# мастер dno-tool, так же как для x11vnc.

%global commit      190db24b727e556e234497c1ce38b88cd6668544
%global shortcommit 190db24
%global snapdate    20240625

Name:           kmsvnc
Version:        0.0.6
Release:        3.%{snapdate}git%{shortcommit}.dno%{?dist}
Summary:        VNC-сервер экрана через DRM/KMS (X11, Wayland, консоль)
Group:          Networking/Remote access
License:        GPLv3
URL:            https://github.com/isjerryxiao/kmsvnc
Source0:        %{url}/archive/%{commit}/%{name}-%{commit}.tar.gz
Patch0:         kmsvnc-abgr-formats.patch

BuildRequires:  cmake
BuildRequires:  gcc
BuildRequires:  make
BuildRequires:  pkgconfig(libdrm)
BuildRequires:  pkgconfig(libvncserver)
BuildRequires:  pkgconfig(xkbcommon)
BuildRequires:  pkgconfig(libva)
BuildRequires:  pkgconfig(libva-drm)

%description
kmsvnc читает изображение экрана напрямую из буфера кадра DRM/KMS и
передаёт его по протоколу VNC, ввод эмулирует через uinput. Не зависит
от графического окружения: работает в сеансах X11 и Wayland, на экране
входа и в текстовой консоли, без запроса разрешения у пользователя.

Сборка dno-tool: добавлена поддержка буферов ABGR/XBGR (KWin 6 на Intel).
Служба в пакет не входит, её создаёт мастер настройки dno-tool.

%prep
%autosetup -n %{name}-%{commit} -p1

%build
cmake -S . -B _build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=%{_prefix} \
    -DCMAKE_C_FLAGS="%{optflags}"
cmake --build _build %{?_smp_mflags}

%install
DESTDIR=%{buildroot} cmake --install _build

%files
%license LICENSE
%doc README.md
%{_bindir}/kmsvnc

%changelog
* Thu Nov 05 2026 xp9k <hellboar@mail.ru> - 0.0.6-3.20240625git190db24.dno
- Сборка в окружении МОС 12: один пакет для МОС 12 и МОС 13.
- Служба и настройки убраны из пакета, их создаёт мастер dno-tool.

* Thu Nov 05 2026 xp9k <hellboar@mail.ru> - 0.0.6-2.20240625git190db24.dno
- Служба: бессрочные повторы запуска, по умолчанию -w (экран будится при
  запуске, иначе при погашенном экране kmsvnc не стартует).

* Thu Nov 05 2026 xp9k <hellboar@mail.ru> - 0.0.6-1.20240625git190db24.dno
- Снимок 190db24, поддержка форматов ABGR/XBGR, служба systemd.
