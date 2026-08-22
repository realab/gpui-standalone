from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Sequence

from .config import SyncConfig
from .errors import SyncError
from .models import PathRevision, SourceSnapshot, SyncTag
from .process import run_command
from .workspace import generate_workspace_manifest


TAG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAG_SCHEMA = "4"
INITIAL_HISTORY_DEPTH = 64
MAX_DEEPEN_ATTEMPTS = 10
FALLBACK_GIT_NAME = "Zed crate sync"
FALLBACK_GIT_EMAIL = "zed-sync@localhost"


def git(
    root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_command(("git", "-C", root, *arguments), check=check)


def discover_repository_root(start: Path) -> Path:
    """Return the containing Git root, or ``start`` outside a repository."""

    start = start.expanduser().resolve()
    probe = git(start, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode == 0:
        return Path(probe.stdout.strip()).resolve()
    return start


def destination_is_repository(root: Path) -> bool:
    if not root.is_dir():
        return False
    probe = git(root, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode != 0:
        return False
    actual_root = Path(probe.stdout.strip()).resolve()
    if actual_root != root:
        raise SyncError(
            f"destination {root} is inside a different Git repository ({actual_root}); "
            "use --root with that repository root"
        )
    return True


def ensure_destination_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if destination_is_repository(root):
        return
    run_command(("git", "init", "--initial-branch=main", root))
    print(f"Initialized Git repository in {root}")


def resolve_remote_commit(source: str, ref: str) -> str:
    if ref.startswith("refs/"):
        patterns = (ref, f"{ref}^{{}}")
    else:
        patterns = (
            f"refs/heads/{ref}",
            f"refs/tags/{ref}^{{}}",
            f"refs/tags/{ref}",
        )
    result = run_command(
        ("git", "ls-remote", "--exit-code", source, *patterns),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ref {ref!r} was not found"
        raise SyncError(f"could not resolve {source} at {ref!r}: {detail}")

    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        commit, separator, remote_ref = line.partition("\t")
        if commit and separator and remote_ref:
            refs[remote_ref] = commit

    preferred_refs = (
        f"refs/heads/{ref}",
        f"refs/tags/{ref}^{{}}",
        f"refs/tags/{ref}",
        f"{ref}^{{}}",
        ref,
    )
    for candidate in preferred_refs:
        if candidate in refs:
            return refs[candidate]
    raise SyncError(f"could not parse the remote commit for {source} at {ref!r}")


def _shallow_boundaries(checkout: Path) -> set[str]:
    shallow = git(checkout, "rev-parse", "--is-shallow-repository").stdout.strip()
    if shallow != "true":
        return set()
    shallow_path = Path(
        git(checkout, "rev-parse", "--git-path", "shallow").stdout.strip()
    )
    if not shallow_path.is_absolute():
        shallow_path = checkout / shallow_path
    return set(shallow_path.read_text(encoding="utf-8").splitlines())


def _read_path_revisions(
    checkout: Path,
    paths: Sequence[Path],
) -> dict[Path, PathRevision]:
    revisions: dict[Path, PathRevision] = {}
    for path in paths:
        result = git(
            checkout,
            "log",
            "-1",
            "--format=%H%x00%cI",
            "HEAD",
            "--",
            str(path),
        ).stdout.strip()
        commit, separator, committed_at = result.partition("\x00")
        if not commit or not separator or not committed_at:
            raise SyncError(f"could not find the latest upstream commit for {path}")
        try:
            timestamp = dt.datetime.fromisoformat(committed_at)
        except ValueError as error:
            raise SyncError(
                f"invalid commit timestamp for {path}: {committed_at}"
            ) from error
        revisions[path] = PathRevision(commit=commit, committed_at=timestamp)
    return revisions


def _latest_path_revisions(
    checkout: Path,
    ref: str,
    paths: Sequence[Path],
) -> dict[Path, PathRevision]:
    """Resolve path history, deepening a shallow clone only when necessary."""

    deepen_by = INITIAL_HISTORY_DEPTH
    for attempt in range(MAX_DEEPEN_ATTEMPTS + 1):
        revisions = _read_path_revisions(checkout, paths)
        boundaries = _shallow_boundaries(checkout)
        if not boundaries or all(
            revision.commit not in boundaries for revision in revisions.values()
        ):
            return revisions

        if attempt == MAX_DEEPEN_ATTEMPTS:
            git(
                checkout,
                "fetch",
                "--quiet",
                "--unshallow",
                "--filter=blob:none",
                "origin",
                ref,
            )
            return _read_path_revisions(checkout, paths)
        else:
            git(
                checkout,
                "fetch",
                "--quiet",
                f"--deepen={deepen_by}",
                "--filter=blob:none",
                "origin",
                ref,
            )
            deepen_by *= 2

    raise SyncError("could not resolve complete history for synchronized paths")


def clone_snapshot(config: SyncConfig, destination: Path) -> SourceSnapshot:
    clone_ref = config.ref
    for prefix in ("refs/heads/", "refs/tags/"):
        if clone_ref.startswith(prefix):
            clone_ref = clone_ref.removeprefix(prefix)
            break

    run_command(
        (
            "git",
            "clone",
            "--quiet",
            f"--depth={INITIAL_HISTORY_DEPTH}",
            "--filter=blob:none",
            "--sparse",
            "--single-branch",
            "--branch",
            clone_ref,
            config.source,
            destination,
        )
    )
    git(destination, "sparse-checkout", "set", *(str(path) for path in config.paths))
    commit = git(destination, "rev-parse", "HEAD").stdout.strip()
    trees: dict[Path, str] = {}
    for path in config.paths:
        if not (destination / path).is_dir():
            raise SyncError(f"upstream path is missing or not a directory: {path}")
        trees[path] = git(destination, "rev-parse", f"HEAD:{path}").stdout.strip()
    generated_workspace = generate_workspace_manifest(destination, config)
    generated_content = generated_workspace.content
    trees[config.workspace_manifest_path] = (
        "sha256:" + hashlib.sha256(generated_content.encode("utf-8")).hexdigest()
    )
    path_revisions = _latest_path_revisions(destination, clone_ref, config.paths)
    return SourceSnapshot(
        commit=commit,
        trees=trees,
        path_revisions=path_revisions,
        generated_files={config.workspace_manifest_path: generated_content},
    )


def _parse_tag_message(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in message.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.startswith("Zed-"):
            fields[key] = value.strip()
    return fields


def _date_tags(root: Path) -> list[str]:
    output = git(root, "tag", "--list").stdout
    return sorted(
        (tag for tag in output.splitlines() if TAG_PATTERN.fullmatch(tag)),
        reverse=True,
    )


def _tag_fields(root: Path, tag: str) -> dict[str, str] | None:
    message = git(
        root,
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag}",
    ).stdout
    fields = _parse_tag_message(message)
    if fields.get("Zed-Sync-Schema") != TAG_SCHEMA:
        return None
    return fields


def latest_sync_tag(root: Path, paths: Sequence[Path]) -> SyncTag | None:
    for tag in _date_tags(root):
        fields = _tag_fields(root, tag)
        if fields is None:
            continue
        source = fields.get("Zed-Repository")
        ref = fields.get("Zed-Ref")
        commit = fields.get("Zed-Commit")
        if not source or not ref or not commit:
            raise SyncError(f"sync tag {tag} is missing required Zed metadata")

        recorded_trees = {
            path: fields.get(f"Zed-Tree-{path}", "") for path in paths
        }
        missing_paths = [path for path, tree_id in recorded_trees.items() if not tree_id]
        if missing_paths:
            missing = ", ".join(str(path) for path in missing_paths)
            raise SyncError(f"sync tag {tag} is missing tree metadata for: {missing}")
        return SyncTag(
            name=tag,
            source=source,
            ref=ref,
            source_commit=commit,
            trees=recorded_trees,
            config_hash=fields.get("Zed-Config-Hash"),
        )
    return None


def ensure_date_tag_is_safe(root: Path, tag: str) -> bool:
    if tag not in _date_tags(root):
        return False
    if _tag_fields(root, tag) is None:
        raise SyncError(
            f"tag {tag} already exists but was not created by this sync tool; "
            "refusing to replace it"
        )
    return True


def managed_status(config: SyncConfig) -> str:
    return git(
        config.root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *(str(path) for path in config.managed_paths),
    ).stdout.strip()


def _git_with_identity(
    root: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    name = git(root, "config", "user.name", check=False).stdout.strip()
    email = git(root, "config", "user.email", check=False).stdout.strip()
    identity: tuple[str, ...] = ()
    if not name or not email:
        identity = (
            "-c",
            f"user.name={FALLBACK_GIT_NAME}",
            "-c",
            f"user.email={FALLBACK_GIT_EMAIL}",
        )
    return git(root, *identity, *arguments)


def commit_sync(config: SyncConfig, source_commit: str, tag: str) -> str | None:
    paths = tuple(str(path) for path in config.managed_paths)
    git(config.root, "add", "-A", "--", *paths)
    staged = git(
        config.root,
        "diff",
        "--cached",
        "--quiet",
        "--",
        *paths,
        check=False,
    )
    if staged.returncode == 0:
        return None
    if staged.returncode != 1:
        raise SyncError(staged.stderr.strip() or "could not inspect staged sync changes")

    _git_with_identity(
        config.root,
        "commit",
        "--only",
        "-m",
        f"Sync GPUI crates from Zed ({tag})",
        "-m",
        f"Zed-Commit: {source_commit}",
        "--",
        *paths,
    )
    return git(config.root, "rev-parse", "HEAD").stdout.strip()


def _tag_message(config: SyncConfig, snapshot: SourceSnapshot, tag: str) -> str:
    lines = [
        f"Zed GPUI sync {tag}",
        "",
        f"Zed-Sync-Schema: {TAG_SCHEMA}",
        f"Zed-Repository: {config.source}",
        f"Zed-Ref: {config.ref}",
        f"Zed-Commit: {snapshot.commit}",
        f"Zed-Source-Date: {snapshot.tag_date}",
        f"Zed-Config-Hash: {config.signature}",
        f"Zed-Paths: {', '.join(str(path) for path in config.paths)}",
        f"Zed-Generated-Paths: {config.workspace_manifest_path}",
    ]
    lines.extend(
        f"Zed-Tree-{path}: {tree_id}" for path, tree_id in snapshot.trees.items()
    )
    lines.extend(
        f"Zed-Path-Commit-{path}: {revision.commit}"
        for path, revision in snapshot.path_revisions.items()
    )
    lines.extend(
        f"Zed-Path-Date-{path}: {revision.committed_at.isoformat()}"
        for path, revision in snapshot.path_revisions.items()
    )
    return "\n".join(lines)


def create_or_move_tag(
    config: SyncConfig,
    *,
    snapshot: SourceSnapshot,
    commit: str,
    tag: str,
    replace: bool,
) -> None:
    arguments = ["tag", "-a"]
    if replace:
        arguments.append("--force")
    arguments.extend(
        (
            tag,
            commit,
            "-m",
            _tag_message(config, snapshot, tag),
        )
    )
    _git_with_identity(config.root, *arguments)
