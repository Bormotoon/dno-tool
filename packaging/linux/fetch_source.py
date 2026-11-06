"""Скачать фиксированные исходники и сохранить SHA-256 в постоянном кэше."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path


def digest(path: Path) -> str:
    """Вычислить хеш потоково, включая большие архивы Qt."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fetch(name: str, recipe: Path, cache: Path) -> Path:
    """Проверить уже скачанный архив по сохранённому хешу до распаковки."""
    spec = json.loads(recipe.read_text(encoding="utf-8"))[name]
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / Path(spec["url"]).name
    record = cache / (target.name + ".json")
    expected = spec.get("sha256")
    origin = spec.get("sha256_origin") or (
        "recipe-or-publisher"
        if expected or spec.get("checksum_url")
        else "first-https-download"
    )
    download_url = spec["url"]
    if record.exists():
        old = json.loads(record.read_text(encoding="utf-8"))
        if old["url"] != spec["url"] or old["version"] != spec["version"]:
            raise ValueError("Источник не совпадает с сохранённым манифестом")
        if expected and expected != old["sha256"]:
            raise ValueError("Контрольные суммы рецепта и кэша не совпадают")
        expected = old["sha256"]
        origin = old.get("checksum_origin", origin)
        download_url = old.get("download_url", download_url)
    if not expected and spec.get("checksum_url"):
        with urllib.request.urlopen(spec["checksum_url"], timeout=120) as response:
            checksum = response.read(4096).decode("ascii")
        match = re.match(r"\s*([a-fA-F0-9]{64})(?:\s|$)", checksum)
        if not match:
            raise ValueError("Некорректная опубликованная контрольная сумма")
        expected = match[1].lower()
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            for url in (spec["url"], *spec.get("mirrors", [])):
                try:
                    with (
                        urllib.request.urlopen(url, timeout=120) as source,
                        temporary.open("wb") as dest,
                    ):
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            dest.write(block)
                except OSError as error:
                    print(f"Не удалось скачать {url}: {error}", file=sys.stderr)
                    last_error = error
                    continue
                actual = digest(temporary)
                if expected and actual != expected:
                    raise ValueError("SHA-256 скачанного архива не совпадает")
                temporary.replace(target)
                download_url = url
                break
            else:
                raise OSError(f"Ни один источник {name} недоступен") from last_error
        finally:
            temporary.unlink(missing_ok=True)
    actual = digest(target)
    if expected and actual != expected:
        raise ValueError(f"Повреждён кэш исходников: {target}")
    # Для источников без опубликованного хеша первая HTTPS-загрузка фиксирует его.
    data = {
        **spec,
        "sha256": actual,
        "checksum_origin": origin,
        "download_url": download_url,
    }
    temporary_record = record.with_suffix(".json.tmp")
    temporary_record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary_record.replace(record)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument(
        "--recipe", type=Path, default=Path(__file__).with_name("sources.json")
    )
    parser.add_argument("--cache", type=Path, default=Path("/cache/sources"))
    args = parser.parse_args()
    print(fetch(args.name, args.recipe, args.cache))
