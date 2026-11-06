"""Включить только QML-модули, используемые выводом камер."""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [
    entry for entry in binaries
    if "qmltooling" not in Path(entry[0]).parts
]
qml_binaries, qml_datas = pyside6_library_info.collect_qtqml_files()


def _needed(entry):
    source, _ = entry
    parts = Path(source).parts
    try:
        relative = parts[parts.index("qml") + 1 :]
    except ValueError:
        return False
    if len(relative) < 2:
        return False
    if relative[0] in {"QtQuick", "QtMultimedia"}:
        return len(relative) == 2
    if relative[0] == "QtQml":
        return len(relative) == 2 or relative[1] in {"Models", "WorkerScript"}
    return False


binaries += [entry for entry in qml_binaries if _needed(entry)]
datas += [entry for entry in qml_datas if _needed(entry)]
