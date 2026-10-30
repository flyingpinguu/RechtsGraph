"""Replaceable embedding interface, OpenRouter adapter and persistent cache.

No network calls at import time. The cache is derived data, never legal evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    """Read only this project's ignored .env; existing environment wins."""
    path = PROJECT_ROOT / ".env"
    if os.environ.get("RECHTSGRAPH_LOAD_ENV", "1") == "0" or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and (name == "OPENROUTER_API_KEY" or name.startswith("RECHTSGRAPH_")):
            os.environ.setdefault(name, value.strip().strip("\"'"))


@dataclass(frozen=True)
class EmbeddingSettings:
    model: str = "qwen/qwen3-embedding-8b"
    dimensions: int = 1024
    api_key: str = field(default="", repr=False, compare=False)
    query_instruction: str = (
        "Given a legal research question, retrieve relevant German or European legal passages."
    )
    cache_path: Path = PROJECT_ROOT / "output/embeddings/cache.sqlite"
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.model.strip() or not 1 <= self.dimensions <= 4096:
            raise ValueError("Set an embedding model and dimensions between 1 and 4096.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Embedding timeout must be finite and positive.")

    @classmethod
    def from_environment(cls) -> "EmbeddingSettings":
        load_local_env()
        model = os.environ.get("RECHTSGRAPH_EMBEDDING_MODEL", cls.model)
        instruction = os.environ.get("RECHTSGRAPH_EMBEDDING_QUERY_INSTRUCTION")
        # Qwen's documented query instruction is model-specific, not universal.
        if instruction is None:
            instruction = cls.query_instruction if model.startswith("qwen/") else ""
        return cls(
            model=model,
            dimensions=int(os.environ.get("RECHTSGRAPH_EMBEDDING_DIMENSIONS", "1024")),
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            query_instruction=instruction,
            cache_path=Path(os.environ.get("RECHTSGRAPH_EMBEDDING_CACHE", str(cls.cache_path))),
            timeout=float(os.environ.get("RECHTSGRAPH_EMBEDDING_TIMEOUT", "60")),
        )

    @property
    def profile(self) -> str:
        # Include text preparation and query prompt: equal dimensions alone are insufficient.
        descriptor = ["citation-title-text-v1", self.model, self.dimensions, self.query_instruction]
        return hashlib.sha256(json.dumps(descriptor).encode()).hexdigest()[:20]

    def prepare(self, text: str, purpose: str) -> str:
        if purpose not in {"query", "document"}:
            raise ValueError("Embedding purpose must be query or document.")
        if purpose == "query" and self.query_instruction:
            return "Instruct: {}\nQuery:{}".format(self.query_instruction, text)
        return text


class Embedder(Protocol):
    settings: EmbeddingSettings

    def encode(self, texts: list[str], purpose: str = "document") -> list[list[float]]: ...


def validate_vector(value: object, dimensions: int) -> list[float]:
    if not isinstance(value, list) or len(value) != dimensions:
        raise ValueError("Embedding response has an incompatible vector dimension.")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value):
        raise ValueError("Embedding vector must contain numbers.")
    vector = [float(x) for x in value]
    if not all(math.isfinite(x) for x in vector) or not any(vector):
        raise ValueError("Embedding vector must be finite and nonzero.")
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


class OpenRouterEmbedder:
    """OpenAI-compatible embeddings endpoint; credentials/errors are never logged."""

    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings
        self.usage = {"requests": 0, "prompt_tokens": 0, "cost_usd": 0.0}

    def encode(self, texts: list[str], purpose: str = "document") -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is missing; set it in the environment or project .env.")
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Embedding inputs must be nonempty text.")
        body = json.dumps({
            "model": self.settings.model,
            "input": [self.settings.prepare(t, purpose) for t in texts],
            "dimensions": self.settings.dimensions,
            "encoding_format": "float",
            "provider": {"max_price": {"prompt": 0.04}},
        }).encode()
        request = Request("https://openrouter.ai/api/v1/embeddings", data=body, headers={
            "Authorization": "Bearer " + self.settings.api_key,
            "Content-Type": "application/json", "X-Title": "RechtsGraph",
        })
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.settings.timeout) as response:
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("OpenRouter embeddings HTTP {}; request failed.".format(exc.code)) from None
            except (URLError, OSError):
                raise RuntimeError("OpenRouter embeddings connection failed; cached work is retained.") from None
        if not isinstance(payload, dict):
            raise ValueError("Invalid embedding response.")
        rows = payload.get("data")
        if (not isinstance(rows, list) or len(rows) != len(texts)
                or any(not isinstance(row, dict) for row in rows)
                or any(type(row.get("index")) is not int for row in rows)
                or sorted(row.get("index", -1) for row in rows) != list(range(len(texts)))):
            raise ValueError("Embedding response must contain exactly one indexed vector per input.")
        vectors = [validate_vector(row.get("embedding"), self.settings.dimensions)
                   for row in sorted(rows, key=lambda row: row["index"])]
        usage = payload.get("usage") or {}
        self.usage["requests"] += 1
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["cost_usd"] += float(usage.get("cost") or 0)
        return vectors


class CachedEmbedder:
    """Cache by model/profile, purpose and exact input; useful for repeated tests."""

    def __init__(self, inner: Embedder):
        self.inner = inner
        self.settings = inner.settings
        self.settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.settings.cache_path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")

    def close(self) -> None:
        self.connection.close()

    def encode(self, texts: list[str], purpose: str = "document") -> list[list[float]]:
        # Validate purpose even when all entries are already cached.
        self.settings.prepare("", purpose)
        keys = [hashlib.sha256((self.settings.profile + "\0" + purpose + "\0" + text).encode()).hexdigest()
                for text in texts]
        vectors: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts):
            row = self.connection.execute("SELECT vector FROM embeddings WHERE key=?", (key,)).fetchone()
            if row:
                vector = array("f")
                vector.frombytes(row[0])
                vectors[key] = validate_vector(vector.tolist(), self.settings.dimensions)
            else:
                missing[key] = text
        if missing:
            result = self.inner.encode(list(missing.values()), purpose)
            if len(result) != len(missing):
                raise ValueError("Embedder returned an incorrect vector count.")
            for key, vector in zip(missing, result):
                vector = validate_vector(vector, self.settings.dimensions)
                self.connection.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?)", (key, array("f", vector).tobytes()))
                vectors[key] = vector
            self.connection.commit()
        return [vectors[key] for key in keys]
