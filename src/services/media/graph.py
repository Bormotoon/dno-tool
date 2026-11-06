"""Явная сборка графов libavfilter через поддерживаемый API PyAV."""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from src.utils.system.media_runtime import load_pyav

av = load_pyav()


def split_graph(text: str, delimiters: str) -> list[str]:
    """Разделить граф, сохраняя первый уровень кавычек и экранирования."""
    result: list[str] = []
    start = 0
    quote = False
    escape = False
    for index, char in enumerate(text):
        if escape:
            escape = False
        elif char == "\\":
            escape = True
        elif char == "'":
            quote = not quote
        elif not quote and char in delimiters:
            result.append(text[start:index].strip())
            start = index + 1
    if quote or escape:
        raise ValueError("Незакрытая кавычка или экранирование в графе фильтров.")
    result.append(text[start:].strip())
    return result


def unescape_graph(text: str) -> str:
    """Снять уровень лексера графа перед передачей аргументов в AVOptions."""
    result: list[str] = []
    quote = False
    escape = False
    for char in text:
        if escape:
            result.append(char)
            escape = False
        elif char == "\\" and not quote:
            escape = True
        elif char == "'":
            quote = not quote
        else:
            result.append(char)
    return "".join(result)


@dataclass(frozen=True)
class FilterSpec:
    inputs: tuple[str, ...]
    name: str
    args: str
    outputs: tuple[str, ...]


def parse_graph(description: str) -> list[list[FilterSpec]]:
    """Разобрать только синтаксис графа; фильтры и опции проверяет FFmpeg."""
    chains: list[list[FilterSpec]] = []
    for chain in split_graph(description, ";"):
        nodes = []
        for expression in split_graph(chain, ","):
            inputs: list[str] = []
            outputs: list[str] = []
            while expression.startswith("["):
                match = re.match(r"\[([^\]]+)\]", expression)
                if match is None:
                    raise ValueError("Некорректная входная метка графа.")
                inputs.append(match[1])
                expression = expression[match.end() :]
            while expression.endswith("]"):
                match = re.search(r"\[([^\[\]]+)\]$", expression)
                if match is None:
                    break
                outputs.insert(0, match[1])
                expression = expression[: match.start()]
            name, separator, args = unescape_graph(expression).partition("=")
            if not re.fullmatch(r"[a-zA-Z0-9_]+(?:@[a-zA-Z0-9_]+)?", name):
                raise ValueError("Некорректное имя фильтра.")
            nodes.append(
                FilterSpec(
                    tuple(inputs),
                    name.split("@")[0],
                    args if separator else "",
                    tuple(outputs),
                )
            )
        chains.append(nodes)
    return chains


class FilterGraph:
    """Граф и его буферы принадлежат исключительно вызывающему потоку."""

    def __init__(
        self,
        description: str,
        inputs: dict[str, Any],
        outputs: dict[str, str],
        *,
        threads: int = 2,
        hw_device: Any = None,
        video_properties: dict[str, tuple[Fraction, Fraction]] | None = None,
    ) -> None:
        self.graph = av.filter.Graph(hw_device=hw_device)
        self.graph.threads = threads
        self.sources: dict[str, Any] = {}
        self.sinks: dict[str, Any] = {}
        labels: dict[str, tuple[Any, int]] = {}
        links: list[tuple[str, Any, int]] = []
        for label, frame in inputs.items():
            rate, sar = (video_properties or {}).get(label, (Fraction(25), Fraction(1)))
            node = (
                self.graph.add(
                    "buffer",
                    video_size=f"{frame.width}x{frame.height}",
                    pix_fmt=frame.format.name,
                    time_base=str(frame.time_base),
                    pixel_aspect=str(sar),
                    frame_rate=str(rate),
                )
                if isinstance(frame, av.VideoFrame)
                else self.graph.add_abuffer(
                    sample_rate=frame.sample_rate,
                    format=frame.format.name,
                    layout=frame.layout.name,
                    time_base=frame.time_base,
                )
            )
            self.sources[label] = node
            labels[label] = node, 0
        for chain in parse_graph(description):
            previous = None
            for spec in chain:
                if spec.name not in av.filter.filters_available:
                    raise ValueError(
                        f"В комплекте FFmpeg отсутствует фильтр {spec.name}."
                    )
                node = self.graph.add(spec.name, spec.args or None)
                if spec.inputs:
                    links.extend(
                        (label, node, pad) for pad, label in enumerate(spec.inputs)
                    )
                elif previous is not None:
                    previous.link_to(node)
                for pad, label in enumerate(spec.outputs):
                    if label in labels:
                        raise ValueError(f"Повторная выходная метка графа: {label}.")
                    labels[label] = node, pad
                previous = node
        for label, kind in outputs.items():
            sink = self.graph.add("buffersink" if kind == "video" else "abuffersink")
            self.sinks[label] = sink
            links.append((label, sink, 0))
        for label, target, pad in links:
            if label not in labels:
                raise ValueError(f"Вход графа не найден: {label}.")
            source, output_pad = labels[label]
            source.link_to(target, output_pad, pad)
        self.graph.configure()
