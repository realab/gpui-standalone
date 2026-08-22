from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
import shutil
import tempfile
import tomllib
from typing import Any

import tomlkit

from .config import SyncConfig
from .errors import SyncError
from .process import run_command, run_live_command


Output = Callable[[str], None]


def _load_generated_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as error:
        raise SyncError(f"generated Cargo workspace is missing: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise SyncError(f"generated Cargo workspace is invalid: {error}") from error


def _workspace_package_paths(config: SyncConfig) -> dict[str, Path]:
    manifest = _load_generated_manifest(config.root / config.workspace_manifest_path)
    workspace = manifest.get("workspace")
    if not isinstance(workspace, Mapping):
        raise SyncError("generated Cargo.toml has no [workspace] table")
    workspace_dependencies = workspace.get("dependencies")
    if not isinstance(workspace_dependencies, Mapping):
        raise SyncError("generated Cargo.toml has no [workspace.dependencies] table")

    package_paths: dict[str, Path] = {}
    for package in config.workspace_packages:
        specification = workspace_dependencies.get(package)
        if not isinstance(specification, Mapping):
            raise SyncError(
                f"generated Cargo.toml does not define workspace dependency {package!r}"
            )
        relative_path = specification.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise SyncError(
                f"workspace dependency {package!r} has no local path to verify"
            )
        package_path = (config.root / relative_path).resolve()
        if not package_path.is_relative_to(config.root) or not package_path.is_dir():
            raise SyncError(
                f"workspace dependency {package!r} has an invalid path: {relative_path}"
            )
        package_paths[package] = Path(relative_path)
    return package_paths


def _copy_prepared_repository(config: SyncConfig, destination: Path) -> None:
    destination.mkdir()
    for relative_path in config.state_paths:
        source = config.root / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)

    license_path = config.root / "LICENSE-APACHE"
    if license_path.is_file():
        shutil.copy2(license_path, destination / license_path.name)

    run_command(("git", "init", "--initial-branch=main", destination))
    run_command(("git", "-C", destination, "add", "--all"))
    run_command(
        (
            "git",
            "-C",
            destination,
            "-c",
            "user.name=GPUI sync verifier",
            "-c",
            "user.email=gpui-sync-verifier@localhost",
            "commit",
            "--quiet",
            "-m",
            "Prepared GPUI sync",
        )
    )


def _consumer_dependencies(
    packages: Mapping[str, Path],
    repository: Path,
) -> dict[str, dict[str, str]]:
    repository_url = repository.as_uri()
    return {
        package: {
            "git": repository_url,
            "version": "*",
        }
        for package in packages
    }


def verify_consumer_build(config: SyncConfig, output: Output = print) -> None:
    """Build a temporary application against the prepared local crates."""

    packages = _workspace_package_paths(config)
    with tempfile.TemporaryDirectory(prefix="gpui-sync-consumer-") as temporary:
        temporary_root = Path(temporary)
        repository = temporary_root / "prepared-repository"
        _copy_prepared_repository(config, repository)
        dependencies = _consumer_dependencies(packages, repository)

        consumer = temporary_root / "consumer"
        source = consumer / "src"
        source.mkdir(parents=True)

        manifest = tomlkit.document()
        manifest["package"] = {
            "name": "gpui-sync-consumer",
            "version": "0.0.0",
            "edition": "2024",
            "publish": False,
        }
        manifest["dependencies"] = dependencies
        manifest_path = consumer / "Cargo.toml"
        manifest_path.write_text(tomlkit.dumps(manifest), encoding="utf-8")
        (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

        package_list = ", ".join(dependencies)
        output(f"Building temporary Git-dependency consumer with: {package_list}")
        run_live_command(
            (
                "cargo",
                "build",
                "--manifest-path",
                manifest_path,
                "--target-dir",
                config.root / "target",
            ),
            cwd=consumer,
        )
