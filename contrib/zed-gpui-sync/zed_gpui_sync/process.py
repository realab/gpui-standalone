from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
from typing import Sequence

from .errors import SyncError


def run_command(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured text output and consistent errors."""

    rendered_command = [str(part) for part in command]
    result = subprocess.run(
        rendered_command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise SyncError(f"command failed ({shlex.join(rendered_command)}):\n{detail}")
    return result


def run_live_command(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
) -> None:
    """Run a command with output attached to the terminal."""

    rendered_command = [str(part) for part in command]
    result = subprocess.run(
        rendered_command,
        cwd=cwd,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SyncError(
            f"command failed with exit status {result.returncode} "
            f"({shlex.join(rendered_command)})"
        )
