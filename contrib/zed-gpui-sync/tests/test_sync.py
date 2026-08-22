from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from zed_gpui_sync.config import SyncConfig, load_config
from zed_gpui_sync.errors import SyncError
from zed_gpui_sync.sync import run_sync
from zed_gpui_sync.verify import verify_consumer_build


TOOL_ROOT = Path(__file__).resolve().parents[1]
INITIAL_COMMIT_TIME = "2024-01-02T09:00:00+00:00"
INITIAL_TAG = "2024-01-02"
UNRELATED_COMMIT_TIME = "2025-07-20T12:00:00+00:00"
GPUI_UPDATE_TIME = "2024-02-10T15:00:00+00:00"
PLATFORM_UPDATE_TIME = "2024-02-12T18:30:00+00:00"
UPDATE_TAG = "2024-02-12"
SAME_DATE_UPDATE_TIME = "2024-02-12T22:45:00+00:00"
LOCAL_TRACKED_UPDATE_TIME = "2025-08-01T10:00:00+00:00"


def run(
    command: list[str],
    cwd: Path,
    *,
    expected: int = 0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != expected:
        raise AssertionError(
            f"expected exit {expected}, got {result.returncode}\n"
            f"command: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class SyncIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="test-zed-sync-")
        base = Path(self.temporary.name)
        self.upstream = base / "upstream"
        self.source = self.upstream.as_uri()
        self.destination = base / "destination"
        self.config = base / "zed-sync.toml"
        self.upstream.mkdir()
        self.destination.mkdir()
        self.config.write_text(
            """\
paths = [
    "crates/gpui",
    "crates/gpui_platform",
    "crates/support",
]
workspace_packages = ["gpui", "gpui_platform"]
""",
            encoding="utf-8",
        )
        run(["git", "init", "--initial-branch=main"], self.upstream)
        run(["git", "config", "user.name", "Test"], self.upstream)
        run(["git", "config", "user.email", "test@example.com"], self.upstream)
        self.write_workspace()
        self.write_crates("one", include_removed_file=True)
        self.commit_upstream("initial", INITIAL_COMMIT_TIME)

        self.environment = os.environ.copy()
        existing_pythonpath = self.environment.get("PYTHONPATH")
        self.environment["PYTHONPATH"] = (
            f"{TOOL_ROOT}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(TOOL_ROOT)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_workspace(self) -> None:
        (self.upstream / "Cargo.toml").write_text(
            """\
[workspace]
resolver = "2"
members = [
    "crates/gpui",
    "crates/gpui_platform",
    "crates/support",
    "crates/unrelated",
]
default-members = ["crates/unrelated"]

[workspace.package]
edition = "2024"
publish = false
license = "GPL-3.0-or-later"

[workspace.dependencies]
gpui = { path = "crates/gpui" }
gpui_platform = { path = "crates/gpui_platform" }
support = { path = "crates/support" }
serde = "1"
unrelated = { path = "crates/unrelated" }

[workspace.lints.rust]
unsafe_code = "allow"

[workspace.metadata.unrelated]
note = "initial"

[profile.dev.package.unrelated]
opt-level = 1
""",
            encoding="utf-8",
        )

        for crate in ("support", "unrelated"):
            crate_root = self.upstream / "crates" / crate
            (crate_root / "src").mkdir(parents=True, exist_ok=True)
            (crate_root / "Cargo.toml").write_text(
                f"""\
[package]
name = "{crate}"
version = "0.0.0"
edition.workspace = true
publish.workspace = true
""",
                encoding="utf-8",
            )
            (crate_root / "src" / "lib.rs").write_text(
                f'pub const NAME: &str = "{crate}";\n',
                encoding="utf-8",
            )

    def write_crates(self, value: str, *, include_removed_file: bool) -> None:
        for crate in ("gpui", "gpui_platform"):
            crate_root = self.upstream / "crates" / crate
            (crate_root / "src").mkdir(parents=True, exist_ok=True)
            dependencies = (
                "[dependencies]\n"
                "support.workspace = true\n"
                "serde.workspace = true\n\n"
                "[dev-dependencies]\n"
                "gpui_platform.workspace = true\n"
                if crate == "gpui"
                else "[dependencies]\ngpui.workspace = true\n"
            )
            (crate_root / "Cargo.toml").write_text(
                f"""\
[package]
name = "{crate}"
version = "0.0.0"
edition.workspace = true
publish.workspace = true

[lints]
workspace = true

{dependencies}""",
                encoding="utf-8",
            )
            (crate_root / "src" / "lib.rs").write_text(
                f'pub const VALUE: &str = "{value}";\n',
                encoding="utf-8",
            )
        removed = self.upstream / "crates" / "gpui" / "remove_me.txt"
        if include_removed_file:
            removed.write_text("temporary\n", encoding="utf-8")
        elif removed.exists():
            removed.unlink()

    def commit_upstream(self, message: str, committed_at: str) -> str:
        run(["git", "add", "-A"], self.upstream)
        environment = os.environ.copy()
        environment["GIT_AUTHOR_DATE"] = committed_at
        environment["GIT_COMMITTER_DATE"] = committed_at
        run(
            ["git", "commit", "-m", message],
            self.upstream,
            env=environment,
        )
        return run(["git", "rev-parse", "HEAD"], self.upstream).stdout.strip()

    def sync(self, *extra: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        return run(
            [
                sys.executable,
                "-m",
                "zed_gpui_sync",
                "--source",
                self.source,
                "--root",
                str(self.destination),
                "--config",
                str(self.config),
                *extra,
            ],
            TOOL_ROOT,
            expected=expected,
            env=self.environment,
        )

    def sync_config(self) -> SyncConfig:
        return load_config(
            root=self.destination,
            config_path=self.config,
            source_override=self.source,
            ref_override=None,
            path_overrides=None,
            package_overrides=None,
        )

    def history(self) -> tuple[str, list[str], str]:
        head = run(["git", "rev-parse", "HEAD"], self.destination).stdout.strip()
        tags = run(["git", "tag", "--list"], self.destination).stdout.splitlines()
        metadata = (self.destination / "manifest.json").read_text(encoding="utf-8")
        return head, tags, metadata

    def test_only_tracked_tree_updates_create_commits_and_tags(self) -> None:
        # Checking a new destination is read-only.
        self.sync("--check", expected=1)
        self.assertFalse((self.destination / ".git").exists())

        first = self.sync()
        self.assertIn(f"Tag: {INITIAL_TAG}", first.stdout)
        self.assertTrue((self.destination / "crates/gpui/remove_me.txt").exists())
        generated = tomllib.loads(
            (self.destination / "Cargo.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            generated["workspace"]["members"],
            ["crates/gpui", "crates/gpui_platform", "crates/support"],
        )
        self.assertEqual(
            generated["workspace"]["default-members"],
            ["crates/gpui", "crates/gpui_platform"],
        )
        self.assertEqual(
            set(generated["workspace"]["dependencies"]),
            {"gpui", "gpui_platform", "support", "serde"},
        )
        self.assertEqual(
            generated["workspace"]["package"],
            {"edition": "2024", "publish": False},
        )
        self.assertIn("lints", generated["workspace"])
        self.assertNotIn("metadata", generated["workspace"])
        self.assertNotIn("profile", generated)

        first_history = self.history()

        # A daily run against the exact same ref is a complete no-op.
        no_change = self.sync()
        self.assertIn("No tracked crate updates", no_change.stdout)
        self.assertEqual(self.history(), first_history)

        # A root Cargo change that is removed by the projection is also a no-op.
        source_manifest = self.upstream / "Cargo.toml"
        source_manifest.write_text(
            source_manifest.read_text(encoding="utf-8").replace(
                'note = "initial"',
                'note = "changed"',
            ),
            encoding="utf-8",
        )
        self.commit_upstream("unrelated update", UNRELATED_COMMIT_TIME)
        unrelated_check = self.sync("--check")
        self.assertIn("all configured crate trees are unchanged", unrelated_check.stdout)
        self.assertEqual(self.history(), first_history)
        unrelated_run = self.sync()
        self.assertIn("No commit or tag was created", unrelated_run.stdout)
        self.assertEqual(self.history(), first_history)

        # Two paths changed on different dates use the newest path commit date,
        # not the newer unrelated commit date or the date this test runs.
        gpui_source = self.upstream / "crates/gpui/src/lib.rs"
        gpui_source.write_text('pub const VALUE: &str = "two";\n', encoding="utf-8")
        (self.upstream / "crates/gpui/remove_me.txt").unlink()
        gpui_commit = self.commit_upstream("gpui update", GPUI_UPDATE_TIME)

        platform_source = self.upstream / "crates/gpui_platform/src/lib.rs"
        platform_source.write_text(
            'pub const VALUE: &str = "two";\n',
            encoding="utf-8",
        )
        second_upstream_commit = self.commit_upstream(
            "platform update",
            PLATFORM_UPDATE_TIME,
        )
        self.sync("--check", expected=1)
        updated = self.sync()
        self.assertIn("Update available in", updated.stdout)
        self.assertIn(f"Latest synced-path commit date: {UPDATE_TAG}", updated.stdout)
        self.assertFalse((self.destination / "crates/gpui/remove_me.txt").exists())

        second_history = self.history()
        self.assertNotEqual(second_history[0], first_history[0])
        self.assertEqual(second_history[1], [INITIAL_TAG, UPDATE_TAG])
        metadata = json.loads(second_history[2])
        self.assertEqual(metadata["schema"], 4)
        self.assertEqual(metadata["source_commit"], second_upstream_commit)
        self.assertEqual(metadata["source_date"], UPDATE_TAG)
        self.assertEqual(
            metadata["source_path_commits"]["crates/gpui"],
            gpui_commit,
        )
        self.assertEqual(
            metadata["source_path_commits"]["crates/gpui_platform"],
            second_upstream_commit,
        )
        tag_text = run(["git", "tag", "-n99", UPDATE_TAG], self.destination).stdout
        self.assertIn(f"Zed-Commit: {second_upstream_commit}", tag_text)
        self.assertIn(f"Zed-Source-Date: {UPDATE_TAG}", tag_text)
        self.assertIn("Zed-Tree-crates/gpui:", tag_text)
        self.assertIn(f"Zed-Path-Commit-crates/gpui: {gpui_commit}", tag_text)

        # Another real update on the same source date moves that date's tag.
        gpui_source.write_text('pub const VALUE: &str = "three";\n', encoding="utf-8")
        third_upstream_commit = self.commit_upstream(
            "same-date gpui update",
            SAME_DATE_UPDATE_TIME,
        )
        same_date_update = self.sync()
        self.assertIn(f"Tag: {UPDATE_TAG}", same_date_update.stdout)
        third_history = self.history()
        self.assertNotEqual(third_history[0], second_history[0])
        self.assertEqual(third_history[1], [INITIAL_TAG, UPDATE_TAG])
        tagged_commit = run(
            ["git", "rev-parse", f"{UPDATE_TAG}^{{}}"],
            self.destination,
        ).stdout.strip()
        self.assertEqual(tagged_commit, third_history[0])
        third_metadata = json.loads(third_history[2])
        self.assertEqual(third_metadata["source_commit"], third_upstream_commit)

    def test_relevant_root_dependency_change_updates_generated_manifest(self) -> None:
        self.sync()
        first_head, first_tags, _ = self.history()

        source_manifest = self.upstream / "Cargo.toml"
        source_manifest.write_text(
            source_manifest.read_text(encoding="utf-8").replace(
                'serde = "1"',
                'serde = { version = "1", features = ["derive"] }',
            ),
            encoding="utf-8",
        )
        source_commit = self.commit_upstream(
            "update a retained dependency",
            UNRELATED_COMMIT_TIME,
        )

        self.sync("--check", expected=1)
        updated = self.sync()
        self.assertIn("Cargo.toml", updated.stdout)
        second_head, second_tags, metadata_text = self.history()
        self.assertNotEqual(second_head, first_head)
        self.assertEqual(first_tags, [INITIAL_TAG])
        self.assertEqual(second_tags, [INITIAL_TAG])
        generated = tomllib.loads(
            (self.destination / "Cargo.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            generated["workspace"]["dependencies"]["serde"]["features"],
            ["derive"],
        )
        metadata = json.loads(metadata_text)
        self.assertEqual(metadata["source_commit"], source_commit)
        self.assertEqual(metadata["source_date"], INITIAL_TAG)

    def test_pre_commit_check_runs_before_commit_and_tag(self) -> None:
        observations: list[str] = []

        def check_before_commit(config: SyncConfig, output: object) -> None:
            self.assertTrue((config.root / "Cargo.toml").is_file())
            self.assertFalse((config.root / "manifest.json").exists())
            head = subprocess.run(
                ["git", "-C", config.root, "rev-parse", "--verify", "HEAD"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(head.returncode, 0)
            self.assertEqual(
                run(["git", "tag", "--list"], config.root).stdout,
                "",
            )
            observations.append("checked")

        result = run_sync(
            self.sync_config(),
            before_commit=check_before_commit,
            output=lambda _message: None,
        )

        self.assertEqual(result, 0)
        self.assertEqual(observations, ["checked"])
        head, tags, _metadata = self.history()
        self.assertTrue(head)
        self.assertEqual(tags, [INITIAL_TAG])

    def test_failed_pre_commit_check_creates_no_commit_or_tag(self) -> None:
        self.sync()
        before_history = self.history()
        mirrored_source = self.destination / "crates/gpui/src/lib.rs"
        before_source = mirrored_source.read_text(encoding="utf-8")

        upstream_source = self.upstream / "crates/gpui/src/lib.rs"
        upstream_source.write_text(
            'pub const VALUE: &str = "unverified";\n',
            encoding="utf-8",
        )
        self.commit_upstream("unverified update", LOCAL_TRACKED_UPDATE_TIME)

        def reject_update(config: SyncConfig, output: object) -> None:
            self.assertTrue((config.root / "Cargo.toml").is_file())
            self.assertIn(
                "unverified",
                mirrored_source.read_text(encoding="utf-8"),
            )
            raise SyncError("consumer build failed")

        with self.assertRaisesRegex(SyncError, "consumer build failed"):
            run_sync(
                self.sync_config(),
                before_commit=reject_update,
                output=lambda _message: None,
            )

        self.assertEqual(self.history(), before_history)
        self.assertEqual(mirrored_source.read_text(encoding="utf-8"), before_source)

    def test_consumer_verifier_uses_both_configured_dependencies(self) -> None:
        self.sync()
        observed: dict[str, object] = {}

        def inspect_build(command: object, *, cwd: Path | None = None) -> None:
            arguments = [str(argument) for argument in command]  # type: ignore[union-attr]
            manifest_path = Path(arguments[arguments.index("--manifest-path") + 1])
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            observed["command"] = arguments
            observed["cwd"] = cwd
            observed["manifest"] = manifest
            observed["source"] = (manifest_path.parent / "src/main.rs").read_text(
                encoding="utf-8"
            )
            repository_url = manifest["dependencies"]["gpui"]["git"]
            repository = Path(unquote(urlparse(repository_url).path))
            observed["tracked_files"] = run(
                ["git", "ls-files"], repository
            ).stdout.splitlines()

        with patch(
            "zed_gpui_sync.verify.run_live_command",
            side_effect=inspect_build,
        ):
            verify_consumer_build(self.sync_config(), output=lambda _message: None)

        manifest = observed["manifest"]
        self.assertIsInstance(manifest, dict)
        dependencies = manifest["dependencies"]  # type: ignore[index]
        self.assertEqual(set(dependencies), {"gpui", "gpui_platform"})
        for package in ("gpui", "gpui_platform"):
            self.assertEqual(dependencies[package]["version"], "*")
            repository_url = dependencies[package]["git"]
            self.assertTrue(repository_url.startswith("file://"))
        self.assertEqual(
            dependencies["gpui"]["git"],
            dependencies["gpui_platform"]["git"],
        )
        tracked_files = observed["tracked_files"]
        self.assertIn("Cargo.toml", tracked_files)
        self.assertIn("crates/gpui/Cargo.toml", tracked_files)
        self.assertIn("crates/gpui_platform/Cargo.toml", tracked_files)
        self.assertEqual(observed["source"], "fn main() {}\n")
        self.assertEqual(observed["command"][0:2], ["cargo", "build"])  # type: ignore[index]

    def test_local_changes_are_blocked_only_when_upstream_crates_changed(self) -> None:
        self.sync()
        local_file = self.destination / "crates/gpui/src/lib.rs"
        local_file.write_text("local edit\n", encoding="utf-8")

        # Unrelated upstream activity does not touch or reject the local edit.
        (self.upstream / "README.md").write_text("unrelated\n", encoding="utf-8")
        self.commit_upstream("unrelated update", UNRELATED_COMMIT_TIME)
        self.sync()
        self.assertEqual(local_file.read_text(encoding="utf-8"), "local edit\n")

        self.write_crates("three", include_removed_file=True)
        self.commit_upstream("tracked update", LOCAL_TRACKED_UPDATE_TIME)
        blocked = self.sync(expected=2)
        self.assertIn("local changes exist", blocked.stderr)
        self.assertEqual(local_file.read_text(encoding="utf-8"), "local edit\n")


if __name__ == "__main__":
    unittest.main()
