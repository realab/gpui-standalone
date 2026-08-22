#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Mirror selected GPUI crates from the Zed repository.

The destination Git tag is the sync date (YYYY-MM-DD).  Its annotation stores
the exact upstream commit and is the source of truth for update checks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn, Sequence


DEFAULT_SOURCE = "https://github.com/zed-industries/zed.git"
DEFAULT_REF = "main"
SYNC_PATHS = (Path("crates/gpui"), Path("crates/gpui_platform"))
METADATA_PATH = Path(".zed-sync.json")
BOOTSTRAP_PATHS = (
    Path("sync_zed.py"),
    Path("README.md"),
    Path("tests/test_sync_zed.py"),
    Path(".gitignore"),
)
TAG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAG_SCHEMA = "1"
FALLBACK_GIT_NAME = "Zed crate sync"
FALLBACK_GIT_EMAIL = "zed-sync@localhost"


class SyncError(RuntimeError):
    """An expected, user-actionable sync failure."""


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        rendered = " ".join(str(part) for part in command)
        raise SyncError(f"command failed ({rendered}):\n{detail}")
    return result


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(("git", "-C", str(root), *arguments), check=check)


def fail(message: str) -> NoReturn:
    raise SyncError(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync gpui and gpui_platform from Zed and create a dated Git tag.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check for an upstream update (exit 1 when one is available)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace local modifications inside the managed crate paths",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"Zed Git remote or local repository (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"upstream branch or tag (default: {DEFAULT_REF})",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="destination repository root (default: directory containing this script)",
    )
    return parser.parse_args()


def ensure_destination_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    probe = git(root, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode == 0:
        actual_root = Path(probe.stdout.strip()).resolve()
        if actual_root != root:
            fail(
                f"destination {root} is inside a different Git repository ({actual_root}); "
                "use --root with that repository root or move the script"
            )
        return

    initialized = run(
        ("git", "init", "--initial-branch=main", str(root)),
        check=False,
    )
    if initialized.returncode != 0:
        # Git versions before 2.28 do not support --initial-branch.
        run(("git", "init", str(root)))
    print(f"Initialized Git repository in {root}")


def destination_is_repository(root: Path) -> bool:
    if not root.is_dir():
        return False
    probe = git(root, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode != 0:
        return False
    actual_root = Path(probe.stdout.strip()).resolve()
    if actual_root != root:
        fail(
            f"destination {root} is inside a different Git repository ({actual_root}); "
            "use --root with that repository root or move the script"
        )
    return True


def parse_tag_message(message: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in message.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.startswith("Zed-"):
            fields[key] = value.strip()
    return fields


def managed_tag_fields(root: Path, tag: str) -> dict[str, str] | None:
    result = git(
        root,
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag}",
    )
    fields = parse_tag_message(result.stdout)
    if fields.get("Zed-Sync-Schema") != TAG_SCHEMA:
        return None
    return fields


def date_tags(root: Path) -> list[str]:
    output = git(root, "tag", "--list").stdout
    return sorted(
        (tag for tag in output.splitlines() if TAG_PATTERN.fullmatch(tag)),
        reverse=True,
    )


def latest_managed_tag(root: Path) -> tuple[str, dict[str, str]] | None:
    for tag in date_tags(root):
        fields = managed_tag_fields(root, tag)
        if fields is not None:
            return tag, fields
    return None


def resolve_remote_commit(source: str, ref: str) -> str:
    if ref.startswith("refs/"):
        patterns = (ref, f"{ref}^{{}}")
    else:
        patterns = (
            f"refs/heads/{ref}",
            f"refs/tags/{ref}^{{}}",
            f"refs/tags/{ref}",
        )

    result = run(("git", "ls-remote", "--exit-code", source, *patterns), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ref {ref!r} was not found"
        fail(f"could not resolve {source} at {ref!r}: {detail}")

    refs = {}
    for line in result.stdout.splitlines():
        commit, _, remote_ref = line.partition("\t")
        if commit and remote_ref:
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
    fail(f"could not parse the remote commit for {source} at {ref!r}")


def is_up_to_date(
    previous: tuple[str, dict[str, str]] | None,
    *,
    source: str,
    ref: str,
    remote_commit: str,
) -> bool:
    if previous is None:
        return False
    _, fields = previous
    return (
        fields.get("Zed-Repository") == source
        and fields.get("Zed-Ref") == ref
        and fields.get("Zed-Commit") == remote_commit
    )


def ensure_today_tag_is_safe(root: Path, today: str) -> bool:
    if today not in date_tags(root):
        return False
    if managed_tag_fields(root, today) is None:
        fail(
            f"tag {today} already exists but was not created by this sync tool; "
            "refusing to replace it"
        )
    return True


def managed_status(root: Path) -> str:
    paths = tuple(str(path) for path in (*SYNC_PATHS, METADATA_PATH))
    return git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *paths,
    ).stdout.strip()


def clone_sparse(source: str, ref: str, destination: Path) -> str:
    print(f"Fetching {source} ({ref}) ...")
    clone_ref = ref
    for prefix in ("refs/heads/", "refs/tags/"):
        if clone_ref.startswith(prefix):
            clone_ref = clone_ref.removeprefix(prefix)
            break
    run(
        (
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "--sparse",
            "--single-branch",
            "--branch",
            clone_ref,
            source,
            str(destination),
        )
    )
    git(destination, "sparse-checkout", "set", *(str(path) for path in SYNC_PATHS))
    commit = git(destination, "rev-parse", "HEAD").stdout.strip()
    for relative_path in SYNC_PATHS:
        source_path = destination / relative_path
        if not source_path.is_dir():
            fail(f"upstream path is missing or not a directory: {relative_path}")
    return commit


def replace_managed_paths(root: Path, checkout: Path) -> None:
    incoming_root = Path(tempfile.mkdtemp(prefix=".zed-sync-incoming-", dir=root))
    backup_root = Path(tempfile.mkdtemp(prefix=".zed-sync-backup-", dir=root))
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for relative_path in SYNC_PATHS:
            incoming = incoming_root / relative_path
            incoming.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(checkout / relative_path, incoming, symlinks=True)

        for relative_path in SYNC_PATHS:
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if destination.exists() or destination.is_symlink():
                backup = backup_root / relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                destination.rename(backup)
            (incoming_root / relative_path).rename(destination)
            replaced.append((destination, backup))
    except BaseException:
        for destination, backup in reversed(replaced):
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


def upstream_tree_ids(checkout: Path) -> dict[str, str]:
    return {
        str(path): git(checkout, "rev-parse", f"HEAD:{path}").stdout.strip()
        for path in SYNC_PATHS
    }


def write_metadata(
    root: Path,
    *,
    source: str,
    ref: str,
    source_commit: str,
    tag: str,
    tree_ids: dict[str, str],
) -> None:
    payload = {
        "schema": 1,
        "source": source,
        "ref": ref,
        "source_commit": source_commit,
        "source_trees": tree_ids,
        "sync_tag": tag,
        "synced_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "paths": [str(path) for path in SYNC_PATHS],
    }
    destination = root / METADATA_PATH
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def git_with_identity(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
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


def commit_managed_paths(root: Path, source_commit: str, tag: str) -> str:
    has_head = git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    selected_paths = [*SYNC_PATHS, METADATA_PATH]
    if not has_head:
        # Keep a freshly created destination repository self-contained. These
        # files are included only in the initial commit and are never swept into
        # later sync commits.
        selected_paths.extend(path for path in BOOTSTRAP_PATHS if (root / path).is_file())
    paths = tuple(str(path) for path in selected_paths)
    git(root, "add", "-A", "--", *paths)
    staged = git(root, "diff", "--cached", "--quiet", "--", *paths, check=False)
    if staged.returncode not in (0, 1):
        fail(staged.stderr.strip() or "could not inspect staged sync changes")
    if staged.returncode == 1:
        git_with_identity(
            root,
            "commit",
            "--only",
            "-m",
            f"Sync GPUI crates from Zed ({tag})",
            "-m",
            f"Zed-Commit: {source_commit}",
            "--",
            *paths,
        )
    head = git(root, "rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        fail("the sync produced no commit, so there is no commit to tag")
    return head.stdout.strip()


def tag_message(*, source: str, ref: str, source_commit: str, tag: str) -> str:
    crates = ", ".join(str(path) for path in SYNC_PATHS)
    return "\n".join(
        (
            f"Zed GPUI sync {tag}",
            "",
            f"Zed-Sync-Schema: {TAG_SCHEMA}",
            f"Zed-Repository: {source}",
            f"Zed-Ref: {ref}",
            f"Zed-Commit: {source_commit}",
            f"Zed-Paths: {crates}",
        )
    )


def create_or_move_tag(
    root: Path,
    *,
    source: str,
    ref: str,
    source_commit: str,
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
            tag_message(
                source=source,
                ref=ref,
                source_commit=source_commit,
                tag=tag,
            ),
        )
    )
    git_with_identity(root, *arguments)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    is_repository = destination_is_repository(root)
    previous = latest_managed_tag(root) if is_repository else None
    remote_commit = resolve_remote_commit(args.source, args.ref)
    if is_up_to_date(
        previous,
        source=args.source,
        ref=args.ref,
        remote_commit=remote_commit,
    ):
        assert previous is not None
        print(f"Up to date: {args.ref} is still {remote_commit} (tag {previous[0]}).")
        return 0

    previous_description = previous[1].get("Zed-Commit", "unknown") if previous else "none"
    print(f"Update available: {previous_description} -> {remote_commit}")
    if args.check:
        return 1

    if not is_repository:
        ensure_destination_repository(root)

    changes = managed_status(root)
    if changes and not args.force:
        fail(
            "local changes exist inside paths managed by the sync tool:\n"
            f"{changes}\n"
            "commit or move those changes, or rerun with --force to replace them"
        )

    today = dt.datetime.now().astimezone().date().isoformat()
    replace_tag = ensure_today_tag_is_safe(root, today)

    with tempfile.TemporaryDirectory(prefix="zed-gpui-sync-") as temporary:
        checkout = Path(temporary) / "zed"
        actual_commit = clone_sparse(args.source, args.ref, checkout)
        if actual_commit != remote_commit:
            print(
                "Upstream moved while syncing; using the fetched commit "
                f"{actual_commit} instead of {remote_commit}."
            )
        trees = upstream_tree_ids(checkout)
        replace_managed_paths(root, checkout)

    write_metadata(
        root,
        source=args.source,
        ref=args.ref,
        source_commit=actual_commit,
        tag=today,
        tree_ids=trees,
    )
    local_commit = commit_managed_paths(root, actual_commit, today)
    create_or_move_tag(
        root,
        source=args.source,
        ref=args.ref,
        source_commit=actual_commit,
        commit=local_commit,
        tag=today,
        replace=replace_tag,
    )
    print(f"Synced {', '.join(str(path) for path in SYNC_PATHS)}")
    print(f"Zed commit: {actual_commit}")
    print(f"Local commit: {local_commit}")
    print(f"Tag: {today}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
