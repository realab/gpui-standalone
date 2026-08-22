from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from . import __version__
from .config import DEFAULT_CONFIG_NAME, DEFAULT_REF, DEFAULT_SOURCE, load_config
from .errors import SyncError
from .repository import discover_repository_root
from .sync import run_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zed-gpui-sync",
        description=(
            "Mirror configured GPUI crate paths from Zed and tag real updates "
            "with the latest commit date across those paths."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check for tracked crate updates (exit 1 when available)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace local modifications inside managed paths when an update exists",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=discover_repository_root(Path.cwd()),
        help="destination repository root (default: containing Git repository)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"configuration file, relative to --root (default: {DEFAULT_CONFIG_NAME})",
    )
    parser.add_argument(
        "--source",
        help=f"override the configured Zed Git source (fallback: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--ref",
        help=f"override the configured upstream branch or tag (fallback: {DEFAULT_REF})",
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        help="override configured paths; repeat once for each repository-relative path",
    )
    parser.add_argument(
        "--package",
        dest="packages",
        action="append",
        help=(
            "override root workspace packages; repeat once for each package whose "
            "dependency closure should be retained"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(
            root=args.root,
            config_path=args.config,
            source_override=args.source,
            ref_override=args.ref,
            path_overrides=args.paths,
            package_overrides=args.packages,
        )
        return run_sync(config, check_only=args.check, force=args.force)
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
