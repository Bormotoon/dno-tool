"""Автосбор диагностики при падении процесса (Windows, x64).

Ставится ТОЛЬКО в режиме отладки (DNO_TOOL_LOG_LEVEL=DEBUG, см.
src.logger.is_debug_mode): гейт - в __main__.main(). В релизных запусках
отладочные файлы (crash_pydump.log, crash-dumps/) не создаются. Прямой
вызов install_crash_report() (смок-тесты) гейт не обходит - он в точке
вызова, а не внутри функции.

Что делает install_crash_reporter() при старте приложения:

1. faulthandler.enable(all_threads=True) - при нативном сбое
   интерпретатор пишет Python-стек КАЖДОГО потока (пулы сканера/пинга/
   veyon, GUI) в crash_pydump.log. Работает и для c0000005, и для
   Qt fail-fast (0xc0000409), и для sys.exit-путей.

2. AddVectoredExceptionHandler(1, handler) - первый-chance VEH:
   на EXCEPTION_ACCESS_VIOLATION / FAIL_FAST / heap-corruption пишет
   полный minidump (MiniDumpWriteDump, dbghelp) в crash-dumps/,
   затем EXCEPTION_CONTINUE_SEARCH - исключение доходит до WER,
   штатная обработка не ломается.

Всё best-effort: любые ошибки установки - в stderr, приложение
работает как раньше. Хендлер минимальный (никаких аллокаций Python,
только WinAPI) - должен быть безопасен в повреждённом процессе.

Без прав администратора, ничего в системе не изменяет.
"""

import os
import sys
import threading

_CRASH_DIR_NAME = "crash-dumps"
_PY_DUMP_NAME = "crash_pydump.log"

# Исключения, по которым пишем minidump. Остальные (breakpoint, C++ EH
# из нормального кода) пропускаем - не ломаем работу.
# https://learn.microsoft.com/en-us/windows/win32/debug/getexceptioncode
# Исключения, по которым пишем minidump. Остальные (C++ EH из нормального
# кода, отладочные single-step) пропускаем - не ломаем работу.
# https://learn.microsoft.com/en-us/windows/win32/debug/getexceptioncode
_INTERESTING = frozenset(
    {
        0xC0000005,  # ACCESS_VIOLATION
        0xC0000409,  # STACK_BUFFER_OVERRUN / FAIL_FAST (Qt fatal)
        0xC0000374,  # HEAP_CORRUPTION
        0xC0000417,  # INVALID_CRT_PARAMETER
        0xC00000FD,  # STACK_OVERFLOW
    }
)

# BREAKPOINT (0x80000003) сознательно НЕ в списке: false positives от
# отладчиков/Int3-хуков; настоящие abort()-падения доходят как
# fail-fast (0xC0000409) или уже пойманы WER.

_state_lock = threading.Lock()
_state = {"installed": False, "dump_dir": "", "py_log": ""}


def _app_data_dir() -> str:
    """Каталог данных приложения (~/.dno-tool), как у logger.py.

    Тест-хук: DNO_CRASH_TEST_DIR перенаправляет всё в указанный каталог
    (смок-тест), чтобы не трогать реальные данные пользователя.
    """
    test_dir = os.environ.get("DNO_CRASH_TEST_DIR", "")
    if test_dir:
        return test_dir
    try:
        from src.logger import get_log_file_path

        return os.path.dirname(get_log_file_path())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".dno-tool")


def _minidump_path() -> str:
    return os.path.join(_state["dump_dir"], "dno-tool-crash.dmp")


# --- Minidump через dbghelp (ctypes, без зависимостей) ----------------------

_MINIDUMP_FLAGS = (
    0x00000001  # MiniDumpWithFullMemory
    | 0x00000002  # MiniDumpWithHandleData
    | 0x00000100  # MiniDumpWithThreadInfo
    | 0x00000200  # MiniDumpWithUnloadedModules
    | 0x00000800  # MiniDumpWithProcessThreadData
    | 0x00020000  # MiniDumpWithFullMemoryInfo
)

_VEH_CONTINUE_SEARCH = 0  # EXCEPTION_CONTINUE_SEARCH


def _install_minidump_handler() -> bool:
    """VEH: полный minidump на интересных исключениях. True - установлен."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        dbghelp = ctypes.windll.dbghelp

        MiniDumpWriteDump = dbghelp.MiniDumpWriteDump
        MiniDumpWriteDump.restype = wintypes.BOOL
        MiniDumpWriteDump.argtypes = [
            wintypes.HANDLE,  # hProcess
            wintypes.DWORD,  # ProcessId
            wintypes.HANDLE,  # hFile
            wintypes.DWORD,  # DumpType
            ctypes.c_void_p,  # ExceptionParam (ptr, Optional)
            ctypes.c_void_p,  # UserStreamParam
            ctypes.c_void_p,  # CallbackParam
        ]
        MiniDumpWriteDump.use_last_error = True

        # EXCEPTION_POINTERS: { ExceptionRecord*, ContextRecord* }
        # EXCEPTION_RECORD: { Code, Flags, Record*, Addr, NumberParameters,
        #                     Information[15] } - x64.
        class EXCEPTION_RECORD64(ctypes.Structure):
            _fields_ = [
                ("ExceptionCode", wintypes.DWORD),
                ("ExceptionFlags", wintypes.DWORD),
                ("ExceptionRecord", ctypes.c_uint64),
                ("ExceptionAddress", ctypes.c_uint64),
                ("NumberParameters", wintypes.DWORD),
                ("__pad", wintypes.DWORD),
                ("ExceptionInformation", ctypes.c_uint64 * 15),
            ]

        class EXCEPTION_POINTERS(ctypes.Structure):
            _fields_ = [
                ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD64)),
                ("ContextRecord", ctypes.c_void_p),
            ]

        # MINIDUMP_EXCEPTION_INFORMATION:
        #   { DWORD ThreadId; PEXCEPTION_POINTERS ExceptionPointers;
        #     BOOL ClientPointers; }
        # https://learn.microsoft.com/en-us/windows/win32/api/minidumpapiset/
        class MINIDUMP_EXCEPTION_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("ThreadId", wintypes.DWORD),
                ("ExceptionPointers", ctypes.c_void_p),
                ("ClientPointers", wintypes.BOOL),
            ]

        VECPROC = ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.POINTER(EXCEPTION_POINTERS))

        dump_path = _minidump_path().encode("utf-8", errors="ignore")
        # pydump лежит рядом с crash-dumps/ (в базовом каталоге приложения).
        # Sidecar-путь готовим заранее как байты: VEH не может вызывать
        # Python I/O (open/write) - только WinAPI (см. veh_handler ниже).
        veh_sidecar_path = os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.abspath(_minidump_path())),
                "..",
                _PY_DUMP_NAME + ".veh",
            )
        ).encode("utf-8", errors="ignore")

        # Ротация: максимум 5 дампов, старые переименовываем .1 ... .5
        try:
            if os.path.exists(_minidump_path()):
                for i in range(4, 0, -1):
                    src = _minidump_path() if i == 1 else f"{_minidump_path()}.{i}"
                    dst = f"{_minidump_path()}.{i + 1}"
                    if os.path.exists(src):
                        try:
                            os.replace(src, dst)
                        except OSError:
                            pass
        except Exception:
            pass

        def _veh_append(path_bytes: bytes, text_bytes: bytes) -> None:
            """Append text_bytes (ASCII) к файлу path_bytes через WinAPI.
            Никаких Python-объектов/аллокаций внутри VEH - только ctypes
            вызовы c заранее подготовленными байтами."""
            try:
                GENERIC_WRITE = 0x40000000
                OPEN_ALWAYS = 4
                FILE_END = 2
                INVALID = 0xFFFFFFFFFFFFFFFF
                h_file = kernel32.CreateFileA(
                    path_bytes,
                    GENERIC_WRITE,
                    0,  # no share
                    None,
                    OPEN_ALWAYS,
                    0,
                    None,
                )
                if h_file == INVALID or h_file == -1:
                    return
                try:
                    kernel32.SetFilePointer(h_file, 0, None, FILE_END)
                    written = wintypes.DWORD(0)
                    kernel32.WriteFile(
                        h_file,
                        text_bytes,
                        len(text_bytes),
                        ctypes.byref(written),
                        None,
                    )
                finally:
                    kernel32.CloseHandle(h_file)
            except Exception:
                pass

        # Шаблоны строк готовим ЗАРАНЕЕ (вне VEH): форматирование строк и
        # любые Python-аллокации внутри VEH запрещены - ctypes вызывает
        # WinAPI с заранее подготовленными байтами.
        _ENTER_TPL = b"[VEH-enter] code=0x%08X\n"
        _HIT_TPL = b"[VEH] code=0x%08X addr=0x%X thread=%u\n"

        def veh_handler(exc_ptrs) -> int:
            # Только интересные коды; всё прочее - сразу дальше.
            # ВАЖНО: внутри VEH нельзя вызывать Python I/O (open/write) -
            # GIL/аллокаторы/локи CPython могут быть повреждены. Только
            # WinAPI через ctypes с заранее подготовленными байтами.
            try:
                if exc_ptrs is not None and exc_ptrs:
                    _c = exc_ptrs.contents.ExceptionRecord[0].ExceptionCode & 0xFFFFFFFF
                else:
                    _c = 0xFFFFFFFF
                # Диагностический маркер входа (все вызовы VEH) в sidecar.
                # %08X - ровно 8 hex-цифр, буфер фиксированного размера.
                enter_buf = _ENTER_TPL % (_c & 0xFFFFFFFF,)
                _veh_append(veh_sidecar_path, enter_buf)

                rec = exc_ptrs.contents.ExceptionRecord if exc_ptrs else None
                code = rec.contents.ExceptionCode if rec else 0
                if code not in _INTERESTING:
                    return 0  # EXCEPTION_CONTINUE_SEARCH

                addr = rec.contents.ExceptionAddress if rec else 0
                hit_buf = _HIT_TPL % (
                    code & 0xFFFFFFFF,
                    addr,
                    kernel32.GetCurrentThreadId(),
                )
                _veh_append(veh_sidecar_path, hit_buf)

                # dbghelp требует ВАЛИДНЫЙ ContextRecord внутри
                # ExceptionPointers (иначе молча фейлит, size=0).
                # В реальном AV он валиден; в синтетике (invoke_for_test)
                # ctx=None → передаём ExceptionParam=NULL (дамп без
                # exception-статуса, но полный по памяти).
                ctx_ptr = exc_ptrs.contents.ContextRecord if exc_ptrs else None
                if ctx_ptr:
                    mei = MINIDUMP_EXCEPTION_INFORMATION(
                        ThreadId=kernel32.GetCurrentThreadId(),
                        ExceptionPointers=ctypes.addressof(exc_ptrs.contents),
                        ClientPointers=False,
                    )
                    exc_param = ctypes.byref(mei)
                else:
                    exc_param = None

                h_file = kernel32.CreateFileA(
                    dump_path,
                    0x40000000,  # GENERIC_WRITE
                    0,  # no share
                    None,
                    2,  # CREATE_ALWAYS
                    0,
                    None,
                )
                if h_file in (0xFFFFFFFFFFFFFFFF, -1):
                    return 0
                try:
                    h_proc = kernel32.GetCurrentProcess()
                    pid = kernel32.GetCurrentProcessId()
                    # Двухступенчато: сначала дамп с exception-статусом
                    # (правильный кейс реального AV). dbghelp молча фейлит
                    # (size=0), если ContextRecord недоступен из VEH
                    # (например, AV пойман чужой __try-ловушкой) — тогда
                    # ретраим с ExceptionParam=NULL: дамп без
                    # exception-статуса, но полный по памяти/потокам.
                    ok = MiniDumpWriteDump(
                        h_proc,
                        pid,
                        h_file,
                        _MINIDUMP_FLAGS,
                        exc_param,
                        None,
                        None,
                    )
                    if not ok and exc_param is not None:
                        kernel32.SetFilePointer(h_file, 0, None, 0)
                        kernel32.SetEndOfFile(h_file)
                        MiniDumpWriteDump(
                            h_proc,
                            pid,
                            h_file,
                            _MINIDUMP_FLAGS,
                            None,
                            None,
                            None,
                        )
                finally:
                    kernel32.CloseHandle(h_file)
            except Exception:
                pass
            return 0  # EXCEPTION_CONTINUE_SEARCH - WER/fault handler работают

        callback = VECPROC(veh_handler)
        # Ссылку держим глобально, чтобы GC не собрал тринк.
        _state["veh_callback"] = callback
        handle = kernel32.AddVectoredExceptionHandler(1, callback)
        if not handle:
            return False

        # Тест-хук: синтетический вызов хендлера (типы из этого замыкания -
        # ctypes сверяет идентичность класса указателя, внешние копии
        # не проходят проверку типов).
        def invoke_for_test(code: int, fault_addr: int) -> bool:
            try:
                rec = EXCEPTION_RECORD64()
                rec.ExceptionCode = code
                rec.ExceptionFlags = 0
                rec.ExceptionAddress = fault_addr
                rec.NumberParameters = 0
                ptrs = EXCEPTION_POINTERS()
                ptrs.ExceptionRecord = ctypes.pointer(rec)
                ptrs.ContextRecord = None
                holder = ctypes.POINTER(EXCEPTION_POINTERS)(ptrs)
                result = callback(holder)
                return result == 0
            except Exception:
                return False

        _state["invoke_veh"] = invoke_for_test
        return True
    except Exception:
        return False


def install_crash_report(enable_native: bool = True) -> dict:
    """Установить crash-репортёр. Вызывается один раз до QApplication.

    Returns:
        {"py_log": путь, "dump_dir": путь, "native": bool}
    """
    with _state_lock:
        if _state["installed"]:
            return {
                "py_log": _state["py_log"],
                "dump_dir": _state["dump_dir"],
                "native": bool(_state.get("veh_callback")),
            }

        base = _app_data_dir()
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            base = os.getcwd()
        dump_dir = os.path.join(base, _CRASH_DIR_NAME)
        try:
            os.makedirs(dump_dir, exist_ok=True)
        except OSError:
            dump_dir = base

        py_log = os.path.join(base, _PY_DUMP_NAME)

        # Порядок важен: _minidump_path()/_note_pydump() в установщике
        # VEH читают _state["dump_dir"], поэтому ставим пути ДО установки.
        _state["dump_dir"] = dump_dir
        _state["py_log"] = py_log

        # 1) Python-стеки всех потоков при сбое.
        try:
            stream = open(py_log, "w", buffering=1, encoding="utf-8", errors="replace")
            import faulthandler

            faulthandler.enable(file=stream, all_threads=True)
        except Exception as e:
            stream = None
            py_log = ""
            print(f"[crash_report] faulthandler.enable failed: {e}", file=sys.stderr)

        # 2) Полный minidump на нативных сбоях.
        native = False
        if enable_native:
            native = _install_minidump_handler()

        _state["installed"] = True

        msg = (
            f"[crash_report] installed: py_log={py_log or 'FAILED'} "
            f"dump_dir={dump_dir} native={'yes' if native else 'no'}"
        )
        try:
            from src.logger import logger

            logger.info(msg)
        except Exception:
            pass
        print(msg, file=sys.stderr)

        return {
            "py_log": py_log,
            "dump_dir": dump_dir,
            "native": native,
        }


def status() -> dict:
    """Текущее состояние (для диагностики/диалога «Диагностика»)."""
    with _state_lock:
        return {
            "installed": _state["installed"],
            "py_log": _state["py_log"],
            "dump_dir": _state["dump_dir"],
            "native": bool(_state.get("veh_callback")),
        }


def invoke_veh_for_test(code: int, fault_addr: int = 0xDEADBEEF) -> bool:
    """Синтетический вызов VEH-хендлера (только для смок-теста).

    Реализация - ``_state["invoke_veh"]``, создана внутри
    ``_install_minidump_handler``: ctypes сверяет идентичность класса
    указателя, поэтому собрать EXCECTION_POINTERS снаружи замыкания
    нельзя (TypeError «expected LP_EXCEPTION_POINTERS»).

    Returns:
        True - хендлер отработал и вернул CONTINUE_SEARCH (0).
    """
    fn = _state.get("invoke_veh")
    if fn is None:
        return False
    return bool(fn(code, fault_addr))
