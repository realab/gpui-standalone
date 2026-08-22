from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PathRevision:
    """The latest upstream commit affecting one synchronized path."""

    commit: str
    committed_at: dt.datetime


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The upstream state and latest affecting commit for every selected path."""

    commit: str
    trees: Mapping[Path, str]
    path_revisions: Mapping[Path, PathRevision]
    generated_files: Mapping[Path, str]

    @property
    def latest_path_revision(self) -> PathRevision:
        return max(
            self.path_revisions.values(),
            key=lambda revision: revision.committed_at,
        )

    @property
    def tag_date(self) -> str:
        return self.latest_path_revision.committed_at.date().isoformat()


@dataclass(frozen=True, slots=True)
class SyncTag:
    """State recorded by an annotated date-formatted sync tag."""

    name: str
    source: str
    ref: str
    source_commit: str
    trees: Mapping[Path, str]
    config_hash: str | None = None

    def tracks(self, source: str, ref: str, config_hash: str) -> bool:
        return (
            self.source == source
            and self.ref == ref
            and self.config_hash == config_hash
            and all(self.trees.values())
        )

    def changed_paths(self, snapshot: SourceSnapshot) -> tuple[Path, ...]:
        return tuple(
            path
            for path, tree_id in snapshot.trees.items()
            if self.trees.get(path) != tree_id
        )
