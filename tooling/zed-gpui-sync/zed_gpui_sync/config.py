from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Sequence

from .errors import SyncError


DEFAULT_SOURCE = "https://github.com/zed-industries/zed.git"
DEFAULT_REF = "main"
DEFAULT_PATHS = (Path("crates/gpui"), Path("crates/gpui_platform"))
DEFAULT_CONFIG_NAME = "zed-sync.toml"
TOOL_CONFIG_PATH = Path(__file__).resolve().parents[1] / DEFAULT_CONFIG_NAME
METADATA_PATH = Path(".zed-sync.json")


@dataclass(frozen=True, slots=True)
class SyncConfig:
    root: Path
    source: str
    ref: str
    paths: tuple[Path, ...]
    metadata_path: Path = METADATA_PATH

    @property
    def managed_paths(self) -> tuple[Path, ...]:
        return (*self.paths, self.metadata_path)


def _load_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise SyncError(f"configuration file does not exist: {path}")
        return {}
    if not path.is_file():
        raise SyncError(f"configuration path is not a file: {path}")
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise SyncError(f"invalid TOML in {path}: {error}") from error

    allowed = {"source", "ref", "paths"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SyncError(f"unknown configuration key(s) in {path}: {', '.join(unknown)}")
    return data


def _validate_paths(values: Sequence[object]) -> tuple[Path, ...]:
    if not values:
        raise SyncError("at least one source path must be configured")

    result: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SyncError("every configured path must be a non-empty string")
        path = Path(value)
        if path.is_absolute() or path == Path(".") or ".." in path.parts:
            raise SyncError(f"configured paths must be safe repository-relative paths: {value!r}")
        if path in seen:
            raise SyncError(f"configured path is duplicated: {value}")
        result.append(path)
        seen.add(path)
    return tuple(result)


def load_config(
    *,
    root: Path,
    config_path: Path | None,
    source_override: str | None,
    ref_override: str | None,
    path_overrides: Sequence[str] | None,
) -> SyncConfig:
    """Load defaults, an optional TOML file, and CLI overrides in that order."""

    root = root.expanduser().resolve()
    if config_path is None:
        repository_config = root / DEFAULT_CONFIG_NAME
        default_config = repository_config if repository_config.is_file() else TOOL_CONFIG_PATH
        data = _load_toml(default_config, required=False)
    else:
        resolved_config = config_path.expanduser()
        if not resolved_config.is_absolute():
            resolved_config = root / resolved_config
        data = _load_toml(resolved_config.resolve(), required=True)

    source = source_override if source_override is not None else data.get("source", DEFAULT_SOURCE)
    ref = ref_override if ref_override is not None else data.get("ref", DEFAULT_REF)
    raw_paths: Sequence[object]
    if path_overrides:
        raw_paths = path_overrides
    else:
        raw_paths = data.get("paths", [str(path) for path in DEFAULT_PATHS])

    if not isinstance(source, str) or not source.strip():
        raise SyncError("source must be a non-empty Git remote or local repository path")
    if not isinstance(ref, str) or not ref.strip():
        raise SyncError("ref must be a non-empty branch or tag")
    if not isinstance(raw_paths, (list, tuple)):
        raise SyncError("paths must be a TOML array of repository-relative strings")

    return SyncConfig(
        root=root,
        source=source,
        ref=ref,
        paths=_validate_paths(raw_paths),
    )
