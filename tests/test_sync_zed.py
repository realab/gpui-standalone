from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "sync_zed.py"


def run(command: list[str], cwd: Path, *, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
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


class SyncZedIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="test-zed-sync-")
        base = Path(self.temporary.name)
        self.upstream = base / "upstream"
        self.destination = base / "destination"
        self.upstream.mkdir()
        self.destination.mkdir()
        run(["git", "init", "--initial-branch=main"], self.upstream)
        run(["git", "config", "user.name", "Test"], self.upstream)
        run(["git", "config", "user.email", "test@example.com"], self.upstream)
        self.write_upstream("one", include_removed_file=True)
        self.commit_upstream("initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_upstream(self, value: str, *, include_removed_file: bool) -> None:
        for crate in ("gpui", "gpui_platform"):
            crate_root = self.upstream / "crates" / crate
            (crate_root / "src").mkdir(parents=True, exist_ok=True)
            (crate_root / "Cargo.toml").write_text(
                f'[package]\nname = "{crate}"\nversion = "0.0.0"\n',
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

    def commit_upstream(self, message: str) -> str:
        run(["git", "add", "-A"], self.upstream)
        run(["git", "commit", "-m", message], self.upstream)
        return run(["git", "rev-parse", "HEAD"], self.upstream).stdout.strip()

    def sync(self, *extra: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        return run(
            [
                sys.executable,
                str(SCRIPT),
                "--source",
                str(self.upstream),
                "--root",
                str(self.destination),
                *extra,
            ],
            PROJECT_ROOT,
            expected=expected,
        )

    def test_sync_check_update_and_local_change_protection(self) -> None:
        today = dt.datetime.now().astimezone().date().isoformat()

        # A check against a new destination is read-only.
        self.sync("--check", expected=1)
        self.assertFalse((self.destination / ".git").exists())

        first = self.sync()
        self.assertIn(f"Tag: {today}", first.stdout)
        self.assertEqual(
            (self.destination / "crates/gpui/src/lib.rs").read_text(encoding="utf-8"),
            'pub const VALUE: &str = "one";\n',
        )
        self.assertTrue((self.destination / "crates/gpui/remove_me.txt").exists())
        self.sync("--check")

        self.write_upstream("two", include_removed_file=False)
        second_upstream_commit = self.commit_upstream("update")
        self.sync("--check", expected=1)
        self.sync()

        self.assertFalse((self.destination / "crates/gpui/remove_me.txt").exists())
        metadata = json.loads(
            (self.destination / ".zed-sync.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["source_commit"], second_upstream_commit)
        tag_count = run(["git", "tag", "--list"], self.destination).stdout.splitlines()
        self.assertEqual(tag_count, [today])
        tag_text = run(["git", "tag", "-n99", today], self.destination).stdout
        self.assertIn(f"Zed-Commit: {second_upstream_commit}", tag_text)

        local_file = self.destination / "crates/gpui/src/lib.rs"
        local_file.write_text("local edit\n", encoding="utf-8")
        self.write_upstream("three", include_removed_file=False)
        self.commit_upstream("another update")
        blocked = self.sync(expected=2)
        self.assertIn("local changes exist", blocked.stderr)
        self.assertEqual(local_file.read_text(encoding="utf-8"), "local edit\n")


if __name__ == "__main__":
    unittest.main()
