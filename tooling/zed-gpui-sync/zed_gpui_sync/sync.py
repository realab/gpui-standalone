from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from .config import SyncConfig
from .errors import SyncError
from .models import SourceSnapshot, SyncTag
from .repository import (
    clone_snapshot,
    commit_sync,
    create_or_move_tag,
    destination_is_repository,
    ensure_date_tag_is_safe,
    ensure_destination_repository,
    latest_sync_tag,
    managed_status,
    resolve_remote_commit,
)


Output = Callable[[str], None]


def _same_upstream(previous: SyncTag | None, config: SyncConfig) -> bool:
    return previous is not None and previous.tracks(config.source, config.ref)


def _replace_managed_paths(config: SyncConfig, checkout: Path) -> None:
    root = config.root
    incoming_root = Path(tempfile.mkdtemp(prefix=".zed-sync-incoming-", dir=root))
    backup_root = Path(tempfile.mkdtemp(prefix=".zed-sync-backup-", dir=root))
    replacements: list[tuple[Path, Path | None]] = []
    try:
        for relative_path in config.paths:
            incoming = incoming_root / relative_path
            incoming.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(checkout / relative_path, incoming, symlinks=True)

        for relative_path in config.paths:
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if destination.exists() or destination.is_symlink():
                backup = backup_root / relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                destination.rename(backup)
            replacements.append((destination, backup))
            (incoming_root / relative_path).rename(destination)
    except BaseException:
        for destination, backup in reversed(replacements):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            elif destination.exists() or destination.is_symlink():
                destination.unlink()
            if backup is not None and backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.rename(destination)
        raise
    finally:
        shutil.rmtree(incoming_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def _write_metadata(config: SyncConfig, snapshot: SourceSnapshot, tag: str) -> None:
    payload = {
        "schema": 3,
        "source": config.source,
        "ref": config.ref,
        "source_commit": snapshot.commit,
        "source_date": snapshot.tag_date,
        "source_trees": {str(path): snapshot.trees[path] for path in config.paths},
        "source_path_commits": {
            str(path): snapshot.path_revisions[path].commit for path in config.paths
        },
        "source_path_dates": {
            str(path): snapshot.path_revisions[path].committed_at.isoformat()
            for path in config.paths
        },
        "sync_tag": tag,
        "synced_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "paths": [str(path) for path in config.paths],
    }
    destination = config.root / config.metadata_path
    temporary = destination.with_name(f"{destination.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def run_sync(
    config: SyncConfig,
    *,
    check_only: bool = False,
    force: bool = False,
    output: Output = print,
) -> int:
    """Run synchronization and return 0, or 1 for a check with updates."""

    is_repository = destination_is_repository(config.root)
    previous = latest_sync_tag(config.root, config.paths) if is_repository else None
    remote_commit = resolve_remote_commit(config.source, config.ref)

    if (
        _same_upstream(previous, config)
        and previous is not None
        and previous.source_commit == remote_commit
    ):
        output(
            f"No tracked crate updates: {config.ref} is still {remote_commit} "
            f"(tag {previous.name})."
        )
        return 0

    output(f"Inspecting tracked crate trees at {remote_commit} ...")
    with tempfile.TemporaryDirectory(prefix="zed-gpui-sync-") as temporary:
        checkout = Path(temporary) / "zed"
        snapshot = clone_snapshot(config, checkout)
        if snapshot.commit != remote_commit:
            output(
                "Upstream moved while checking; using the fetched commit "
                f"{snapshot.commit} instead of {remote_commit}."
            )

        if _same_upstream(previous, config) and previous is not None:
            changed_paths = previous.changed_paths(snapshot)
            if not changed_paths:
                output(
                    "No tracked crate updates; the Zed ref moved, but all configured "
                    "crate trees are unchanged. No commit or tag was created."
                )
                return 0
        else:
            changed_paths = config.paths

        output(f"Update available in: {', '.join(str(path) for path in changed_paths)}")
        if check_only:
            return 1

        if not is_repository:
            ensure_destination_repository(config.root)

        changes = managed_status(config)
        if changes and not force:
            raise SyncError(
                "local changes exist inside paths managed by the sync tool:\n"
                f"{changes}\n"
                "commit or move those changes, or rerun with --force to replace them"
            )

        tag_date = snapshot.tag_date
        replace_tag = ensure_date_tag_is_safe(config.root, tag_date)
        _replace_managed_paths(config, checkout)
        _write_metadata(config, snapshot, tag_date)

    local_commit = commit_sync(config, snapshot.commit, tag_date)
    if local_commit is None:
        output("No file changes were produced. No commit or tag was created.")
        return 0

    create_or_move_tag(
        config,
        snapshot=snapshot,
        commit=local_commit,
        tag=tag_date,
        replace=replace_tag,
    )
    output(f"Synced {', '.join(str(path) for path in config.paths)}")
    output(f"Zed commit: {snapshot.commit}")
    output(f"Local commit: {local_commit}")
    output(f"Latest synced-path commit date: {tag_date}")
    output(f"Tag: {tag_date}")
    return 0
