Name:           dno-tool
Version:        0.5.9
Release:        1%{?dist}
Summary:        Инструмент управления сетевыми устройствами для МОС/АЛЬТ Linux
Group:          System/Configuration/Other

License:        Proprietary
URL:            https://github.com/xp9k/dno-tool
Source0:        %{name}-%{version}.tar.gz

BuildArch:      x86_64

# Установка ТОЛЬКО в /opt/dno-tool (не relocatable, --relocate не поддерживается).
# Ярлыки общие для всех пользователей: /usr/share/applications (2 шт, как раньше).

Requires:       polkit
Requires:       openssh-clients
Obsoletes:      dno-tool < %{version}
Conflicts:      dno-tool < %{version}

%define __os_install_post %{nil}
%define __strip /bin/true
%define __spec_install_post /bin/true
%global _build_id_links none

# Приватные библиотеки приложения не предоставляют системные SONAME.
%global __provides_exclude_from ^/opt/dno-tool/lib/.*$
%global __requires_exclude_from ^/opt/dno-tool/lib/.*$

%description
DNO Tool — графическое приложение для удалённого администрирования
компьютеров под управлением МОС Linux по протоколу SSH.

Возможности:
- Дерево и список хостов с группами, статусами доступности, миниатюрами
  экранов (Veyon или VNC) и всплывающими сведениями о системе
- Массовое выполнение SSH-, SFTP- и локальных команд на многих хостах
  одновременно с живым выводом; своя команда без сохранения в библиотеку
- Библиотека из 400+ готовых команд, редактор команд, импорт из Ansible
- Связки учётных данных, sudo с паролем из связки, управление SSH-ключами
  и списком known_hosts
- Мастер настройки: хосты, команды, SSH-ключи, пара ключей Veyon, установка
  SSH/Veyon/VNC (kmsvnc, x11vnc, KRFB) на удалённые хосты
- Управление сеансами без Veyon: питание, выход и вход пользователя,
  блокировка экрана и ввода, Wake-on-LAN; функции Veyon (сообщения, сайты,
  запуск программ, блокировка)
- Встроенный VNC-просмотрщик, SSH-терминал и двухпанельный SFTP-менеджер
  с архиватором
- Сведения об устройстве, редакторы политик Polkit и настроек KDE
- Сканер IP-диапазонов (ICMP/TCP, DNS/NetBIOS, SNMP), мониторинг пинга
  с графиком и карта топологии сети
- Камеры ЕЦХД: запись, панель наблюдения; запись и трансляция удалённого
  рабочего стола (FFmpeg/GStreamer на хосте)
- Видеоредактор: монтаж записей, наложения, титры, экспорт
- Веб-интерфейс управления по HTTPS: хосты, команды, VNC, терминал, файлы,
  камеры; вход по локальным учётным записям или через LDAP/Active Directory
- Связь экземпляров программы (ноды): общие хосты, статусы, миниатюры,
  команды и VNC через защищённый канал TLS
- Диагностика SSH/Veyon/VNC с отчётом, монитор ресурсов и пулов потоков

%prep
%setup -q -n %{name}-%{version}

%install
rm -rf %{buildroot}

# ── Программа в /opt ──
install -Dm755 opt/dno-tool/dno-tool                           %{buildroot}/opt/dno-tool/dno-tool
cp -r opt/dno-tool/assets                                       %{buildroot}/opt/dno-tool/assets
cp -a opt/dno-tool/lib                                          %{buildroot}/opt/dno-tool/lib
chmod -R a+rX %{buildroot}/opt/dno-tool/lib
chmod -R a+rX %{buildroot}/opt/dno-tool/assets
# Zip/json/ico/svg/png из рабочей копии могут приехать с +x (Windows-checkout) -
# снять exec-бит с файлов ассетов, иначе rpmlint W: spurious-executable-perm.
find %{buildroot}/opt/dno-tool/assets -type f -exec chmod 644 {} +

# ── Симлинк для запуска из терминала (как раньше: dno-tool) ──
# Относительный (../../opt/...), чтобы не ловить E от brp/rpmlint
# за абсолютный симлинк.
mkdir -p %{buildroot}%{_bindir}
ln -s ../../opt/dno-tool/dno-tool %{buildroot}%{_bindir}/dno-tool

install -Dm755 policykit/dno-tool-admin                            %{buildroot}%{_bindir}/dno-tool-admin
install -Dm644 policykit/com.dno-tool.policy                       %{buildroot}%{_datadir}/polkit-1/actions/com.dno-tool.policy
install -Dm644 policykit/com.dno-tool.desktop                      %{buildroot}%{_datadir}/applications/com.dno-tool.desktop
install -Dm644 policykit/com.dno-tool-admin.desktop                %{buildroot}%{_datadir}/applications/com.dno-tool-admin.desktop
install -Dm644 icon/dno-tool.png                                   %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/dno-tool.png
install -Dm644 icon/dno-tool.svg                                   %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/dno-tool.svg

%pre
# Место установки: /opt/dno-tool (пакет не relocatable). Создаем /opt,
# если его нет (минимальные образы), вместо ошибки установки.
if [ ! -d /opt ]; then
    mkdir -p /opt || {
        echo "Ошибка: не удалось создать /opt, установка dno-tool возможна только в /opt/dno-tool"
        exit 1
    }
fi
# Миграция со старой версии (бинарник лежал файлом в /usr/bin):
# при upgrade ($1 -eq 2) rpm сам заменит файл симлинком, здесь чистим
# только неучтенные ручные копии, чтобы не смешивать версии.
if [ -e /opt/dno-tool/dno-tool ] && ! rpm -qf /opt/dno-tool/dno-tool >/dev/null 2>&1; then
    echo "Удаление неучтенной ручной копии /opt/dno-tool ..."
    rm -rf /opt/dno-tool
fi
# Старый файл-бирь в /usr/bin от ручной установки (не из rpm) - убрать,
# иначе будет затенять симлинк пакета.
if [ -f %{_bindir}/dno-tool ] && [ ! -L %{_bindir}/dno-tool ] \
    && ! rpm -qf %{_bindir}/dno-tool >/dev/null 2>&1; then
    echo "Удаление ручной копии %{_bindir}/dno-tool (заменяется симлинком на /opt) ..."
    rm -f %{_bindir}/dno-tool
fi

%post
update-desktop-database %{_datadir}/applications 2>/dev/null || :
gtk-update-icon-cache -f %{_datadir}/icons/hicolor 2>/dev/null || :

%postun
update-desktop-database %{_datadir}/applications 2>/dev/null || :
if [ $1 -eq 0 ]; then
    gtk-update-icon-cache -f %{_datadir}/icons/hicolor 2>/dev/null || :
fi

%files
%defattr(-,root,root,-)
/opt/dno-tool/
%{_bindir}/dno-tool
%{_bindir}/dno-tool-admin
%{_datadir}/polkit-1/actions/com.dno-tool.policy
%{_datadir}/applications/com.dno-tool.desktop
%{_datadir}/applications/com.dno-tool-admin.desktop
%{_datadir}/icons/hicolor/256x256/apps/dno-tool.png
%{_datadir}/icons/hicolor/scalable/apps/dno-tool.svg
