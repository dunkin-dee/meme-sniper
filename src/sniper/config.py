"""Config loading with ${ENV_VAR} expansion.

All thresholds live in config.yaml. Code reads them through Config.get() with a
dotted path so a missing key fails loudly at the call site rather than silently
defaulting to something that quietly changes behaviour.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MISSING = object()


def _expand(value: Any) -> Any:
    """Recursively expand ${ENV_VAR} references in strings."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class ConfigError(RuntimeError):
    """Raised when config is missing or malformed."""


class Config:
    """Dotted-path accessor over the parsed config.yaml."""

    def __init__(self, data: dict[str, Any], source: Path | None = None) -> None:
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        if path is None:
            path = os.environ.get("SNIPER_CONFIG", "config.yaml")
        path = Path(path).resolve()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
        return cls(_expand(raw), source=path)

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        """Fetch a value by dotted path, e.g. 'rpc.max_rps'."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise ConfigError(
                        f"missing config key {dotted!r}"
                        + (f" in {self.source}" if self.source else "")
                    )
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted)
        if not isinstance(value, dict):
            raise ConfigError(f"config key {dotted!r} is not a mapping")
        return value

    def __contains__(self, dotted: str) -> bool:
        return self.get(dotted, None) is not None

    @property
    def data(self) -> dict[str, Any]:
        return self._data


def resolve_path(cfg: Config, dotted: str) -> Path:
    """Resolve a config path value relative to the config file's directory."""
    raw = Path(str(cfg.get(dotted)))
    if raw.is_absolute():
        return raw
    base = cfg.source.parent if cfg.source else Path.cwd()
    return (base / raw).resolve()
