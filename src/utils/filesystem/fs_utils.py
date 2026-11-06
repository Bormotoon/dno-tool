"""Утилиты для работы с файловой системой. Обеспечивают корректное владение файлами при запуске от sudo/pkexec."""
import os
from typing import Optional, Tuple

try:
    import pwd
except ImportError:
    pwd = None


_user_info: Optional[Tuple[int, int]] = None


def _get_user_info() -> Optional[Tuple[int, int]]:
    """Определить uid/gid реального пользователя из переменных окружения sudo/pkexec."""
    global _user_info
    if _user_info is not None:
        return _user_info

    if os.name == 'nt' or pwd is None:
        _user_info = None
        return None

    sudo_uid = os.environ.get('SUDO_UID')
    sudo_gid = os.environ.get('SUDO_GID')
    pkexec_uid = os.environ.get('PKEXEC_UID')

    if pkexec_uid is not None:
        uid = int(pkexec_uid)
        gid = pwd.getpwuid(uid).pw_gid
    elif sudo_uid is not None:
        uid = int(sudo_uid)
        gid = int(sudo_gid) if sudo_gid else pwd.getpwuid(uid).pw_gid
    elif os.getuid() == 0:
        home = os.environ.get('HOME', '/root')
        for pw in pwd.getpwall():
            if pw.pw_dir == home and pw.pw_uid != 0:
                uid = pw.pw_uid
                gid = pw.pw_gid
                break
        else:
            _user_info = None
            return None
    else:
        _user_info = None
        return None

    _user_info = (uid, gid)
    return _user_info


def chown_path(path: str) -> bool:
    """Сменить владельца файла или каталога на реального пользователя."""
    info = _get_user_info()
    if info is None:
        return False
    uid, gid = info
    try:
        os.chown(path, uid, gid)
        return True
    except OSError:
        return False


def ensure_file_user_owned(descriptor: int) -> None:
    """Назначить владельца открытому файлу без повторного разрешения пути."""
    if os.name == 'nt':
        return
    info = _get_user_info()
    if info is not None:
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != info:
            os.fchown(descriptor, *info)


def ensure_user_owned(path: str) -> None:
    """Рекурсивно установить владельца пути (и содержимого) на реального пользователя."""
    if os.name == 'nt':
        return

    info = _get_user_info()
    if info is None:
        return

    uid, gid = info

    path = os.path.abspath(path)

    if os.path.isfile(path):
        st = os.stat(path)
        if st.st_uid != uid or st.st_gid != gid:
            chown_path(path)
    elif os.path.isdir(path):
        st = os.stat(path)
        if st.st_uid != uid or st.st_gid != gid:
            chown_path(path)
        for dirpath, dirnames, filenames in os.walk(path):
            for d in dirnames:
                full = os.path.join(dirpath, d)
                dst = os.stat(full)
                if dst.st_uid != uid or dst.st_gid != gid:
                    chown_path(full)
            for f in filenames:
                full = os.path.join(dirpath, f)
                fst = os.stat(full)
                if fst.st_uid != uid or fst.st_gid != gid:
                    chown_path(full)
