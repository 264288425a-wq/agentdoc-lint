"""Token counting with optional tiktoken support and zero-dependency fallback."""

from __future__ import annotations

import contextlib
import importlib
import math
import re
from typing import Any, Protocol

_CHUNK_RE = re.compile(
    r"[A-Za-z]+(?:_[A-Za-z]+)*|[0-9]+|[\u4e00-\u9fff]|[^\sA-Za-z0-9\u4e00-\u9fff]+"
)


class TokenCounter(Protocol):
    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """Zero-dependency BPE-style estimator for Markdown instruction files."""

    @property
    def name(self) -> str:
        return "heuristic-bpe"

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0.0
        for match in _CHUNK_RE.finditer(text):
            chunk = match.group(0)
            first = chunk[0]
            if "\u4e00" <= first <= "\u9fff":
                total += 1.4
            elif first.isdigit():
                total += max(1.0, math.ceil(len(chunk) / 3.0))
            elif first.isalpha():
                parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", chunk)
                if not parts:
                    total += max(1.0, math.ceil(len(chunk) / 4.5))
                else:
                    for part in parts:
                        total += 1.0 if len(part) <= 6 else math.ceil(len(part) / 4.5)
                    total += chunk.count("_") * 0.4
            else:
                total += max(0.65, len(chunk) * 0.65)
        total += text.count("\n") * 0.35
        return max(1, round(total))


class TiktokenCounter:
    """Exact OpenAI BPE token counter when `tiktoken` is installed."""

    def __init__(self, encoding_name: str = "o200k_base") -> None:
        tiktoken_mod: Any = importlib.import_module("tiktoken")
        self._encoding_name = encoding_name
        self._encoding: Any = tiktoken_mod.get_encoding(encoding_name)

    @property
    def name(self) -> str:
        return f"tiktoken:{self._encoding_name}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text, disallowed_special=()))


def get_token_counter(
    prefer_tiktoken: bool = True,
    encoding: str = "o200k_base",
) -> TokenCounter:
    if prefer_tiktoken:
        with contextlib.suppress(Exception):
            return TiktokenCounter(encoding_name=encoding)
    return HeuristicTokenCounter()
