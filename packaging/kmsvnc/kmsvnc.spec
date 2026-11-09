# kmsvnc для МОС: VNC-сервер, читающий кадр напрямую из DRM/KMS.
# Работает в X11, Wayland (KWin, GNOME) и на экране входа без портала.
# Сборка: packaging/kmsvnc/build.sh (окружение МОС 12, пакет для МОС 12 и 13).
# Пакет содержит только программу: службу, пароль и порт настраивает
# мастер dno-tool, так же как для x11vnc.

%global commit      190db24b727e556e234497c1ce38b88cd6668544
%global shortcommit 190db24
%global snapdate    20240625
# libvncserver встраивается статически: системная в МОС 12 (0.9.13) и МОС 13
# (0.9.15) различается раскладкой структур под одним soname.
%global lvs_version 0.9.15

Name:           kmsvnc
Version:        0.0.6
Release:        10.%{snapdate}git%{shortcommit}.dno%{?dist}
Summary:        VNC-сервер экрана через DRM/KMS (X11, Wayland, консоль)
Group:          Networking/Remote access
License:        GPLv3 and GPLv2+
URL:            https://github.com/xp9k/kmsvnc
Source0:        %{name}-%{commit}.tar.gz
Source1:        libvncserver-%{lvs_version}.tar.gz
# Исправление libvncserver (не PatchN: autosetup применил бы его к kmsvnc).
Source2:        libvncserver-detach-client-thread.patch

BuildRequires:  cmake
BuildRequires:  gcc
BuildRequires:  make
BuildRequires:  patch
BuildRequires:  pkgconfig(libdrm)
BuildRequires:  pkgconfig(zlib)
BuildRequires:  pkgconfig(libjpeg)
BuildRequires:  pkgconfig(libpng)
BuildRequires:  pkgconfig(xkbcommon)
BuildRequires:  pkgconfig(libva)
BuildRequires:  pkgconfig(libva-drm)
BuildRequires:  pkgconfig(egl)
BuildRequires:  pkgconfig(glesv2)
BuildRequires:  pkgconfig(gbm)
BuildRequires:  pkgconfig(json-c)

%description
kmsvnc читает изображение экрана напрямую из буфера кадра DRM/KMS и
передаёт его по протоколу VNC, ввод эмулирует через uinput. Не зависит
от графического окружения: работает в сеансах X11 и Wayland, на экране
входа и в текстовой консоли, без запроса разрешения у пользователя.

Сборка dno-tool: добавлена поддержка буферов ABGR/XBGR (KWin 6 на Intel).
NVIDIA, Intel, AMD: импорт текущего буфера экрана через EGL DMA-BUF,
чтение через GLES (VAAPI — запасной путь для Intel и AMD).
Миниатюры dno-tool: кадр, уменьшенный по запросу клиента в целое число раз.
libvncserver %{lvs_version} встроена статически (одинаково на МОС 12 и 13).
Служба в пакет не входит, её создаёт мастер настройки dno-tool.

%prep
%autosetup -n %{name}-%{commit} -p1 -a 1
patch -d libvncserver-LibVNCServer-%{lvs_version} -p1 < %{SOURCE2}

%build
# Только серверная часть: без TLS/SASL/websockets и примеров. VNC Auth
# реализован внутри libvncserver, внешняя криптография не нужна.
cmake -S libvncserver-LibVNCServer-%{lvs_version} -B _lvs_build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_FLAGS="%{optflags}" \
    -DCMAKE_INSTALL_PREFIX="$PWD/_lvs" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DWITH_OPENSSL=OFF -DWITH_GNUTLS=OFF -DWITH_GCRYPT=OFF -DWITH_SASL=OFF \
    -DWITH_WEBSOCKETS=OFF -DWITH_SYSTEMD=OFF -DWITH_LZO=OFF \
    -DWITH_TIGHTVNC_FILETRANSFER=OFF -DWITH_SDL=OFF -DWITH_GTK=OFF -DWITH_QT=OFF \
    -DWITH_LIBSSHTUNNEL=OFF -DWITH_FFMPEG=OFF -DWITH_XCB=OFF \
    -DWITH_EXAMPLES=OFF -DWITH_TESTS=OFF
cmake --build _lvs_build %{?_smp_mflags}
cmake --install _lvs_build
cmake -S . -B _build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=%{_prefix} \
    -DCMAKE_C_FLAGS="%{optflags}" \
    -DKMSVNC_LIBVNCSERVER_PREFIX="$PWD/_lvs"
cmake --build _build %{?_smp_mflags}

%install
DESTDIR=%{buildroot} cmake --install _build
cp libvncserver-LibVNCServer-%{lvs_version}/COPYING COPYING.libvncserver

%files
%license LICENSE COPYING.libvncserver
%doc README.md
%{_bindir}/kmsvnc

%changelog
* Mon Nov 09 2026 dno-tool - 0.0.6-10.20240625git190db24.dno
- Intel и AMD: захват через EGL DMA-BUF с текущим буфером экрана на каждом
  кадре. Прежний захват VAAPI импортировал один буфер при запуске, и после
  page flip Xorg или входа пользователя отдавал застывшую картинку: ввод
  работал, но изменения экрана не приходили.
- Запасной захват VAAPI пересоздаёт поверхность при смене буфера экрана.
- EGL принимает буферы с несколькими плоскостями (Intel CCS), GBM открывается
  на render node с правом записи (iris на fd карты только для чтения падал),
  без конфигураций pbuffer берётся любая конфигурация GLES2 (Mesa GBM).
- Пробуждение экрана (-w) нажатием Shift: прежнее перемещение мыши в угол
  экрана открывало обзор окон KWin при каждом запуске службы.
- Причина отказа захвата EGL пишется в журнал.
- Сведения о захвате сразу попадают в журнал (построчный stdout).

* Sat Nov 07 2026 dno-tool - 0.0.6-9.20240625git190db24.dno
- libvncserver 0.9.15 встроена статически: системная библиотека МОС 12 и 13
  различается раскладкой структур под одним soname, 0.9.13 теряет буферы
  Zlib/Tight при каждом отключении клиента.
- Если подключены только клиенты миниатюр, экран снимается раз в секунду.
- Освобождение записи расширения миниатюр при отключении клиента.
- Патч libvncserver: поток клиента отсоединяется после отключения (стек и
  TLS каждого отключившегося клиента оставались до остановки сервера).

* Sat Nov 07 2026 dno-tool - 0.0.6-7.20240625git190db24.dno
- Расширение миниатюр dno-tool: клиент запрашивает размер, сервер
  отдаёт ему кадр, уменьшенный в целое число раз. Остальные клиенты
  получают полный кадр.

* Sat Nov 07 2026 dno-tool - 0.0.6-6.20240625git190db24.dno
- Определение схемы мониторов перенесено в C, внешний скрипт удалён.

* Fri Nov 06 2026 dno-tool - 0.0.6-5.20240625git190db24.dno
- Единый кадр всех мониторов NVIDIA с реальной геометрией KWin.
- Полный рабочий стол и стандартная схема мониторов ExtendedDesktopSize.

* Fri Nov 06 2026 dno-tool - 0.0.6-4.20240625git190db24.dno
- Абсолютная мышь без REL_X/REL_Y: Xorg/libinput принимает координаты.
- Пробуждение экрана через абсолютные события.
- Горизонтальная прокрутка, Shift для символов, повторы клавиш.
- Поиск клавиш во всех раскладках, отпускание ввода при отключении.
- Захват NVIDIA через EGL DMA-BUF с учётом актуального framebuffer.
- Полные исходники в src/, сборка без загрузки исходников из GitHub.

* Thu Nov 05 2026 dno-tool - 0.0.6-3.20240625git190db24.dno
- Сборка в окружении МОС 12: один пакет для МОС 12 и МОС 13.
- Служба и настройки убраны из пакета, их создаёт мастер dno-tool.

* Thu Nov 05 2026 dno-tool - 0.0.6-2.20240625git190db24.dno
- Служба: бессрочные повторы запуска, по умолчанию -w (экран будится при
  запуске, иначе при погашенном экране kmsvnc не стартует).

* Thu Nov 05 2026 dno-tool - 0.0.6-1.20240625git190db24.dno
- Снимок 190db24, поддержка форматов ABGR/XBGR, служба systemd.
