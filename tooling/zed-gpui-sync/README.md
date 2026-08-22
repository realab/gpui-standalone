# Zed GPUI sync CLI

This directory contains the Python CLI that mirrors the GPUI dependency closure
from `zed-industries/zed` into the surrounding repository. It is a
self-contained `uv` project with a flat package layout, its own lockfile, and
its own tests.

## Run from the repository root

```sh
uv run --project tooling/zed-gpui-sync zed-gpui-sync
```

The CLI discovers the surrounding Git repository root. Its default source,
branch, root workspace packages, and mirrored paths are in this directory's
`zed-sync.toml`. A repository-level file with that name takes precedence, and
`--config` can select another file explicitly.

For every run, the CLI recursively follows path dependencies starting with the
configured workspace packages. It then generates the repository's root
`Cargo.toml` from Zed's root manifest, retaining only:

- workspace members in that dependency closure
- workspace package fields and lints inherited by those members
- workspace dependency entries used by those members
- relevant crates.io patches

Every discovered workspace member must be covered by a configured mirrored
path. This makes a newly introduced internal Zed dependency fail explicitly
until its source path is added to `zed-sync.toml`.

A real crate update replaces the configured directories, updates
`.zed-sync.json`, creates a scoped Git commit, and creates an annotated
`YYYY-MM-DD` tag. The tag date is the newest upstream commit date among all
configured paths; it is not the date when the command runs. Multiple real
updates whose newest path commits share a date move that tool-managed tag to
the latest sync commit.

If the mirrored Git trees and generated root manifest are unchanged, the
command creates no commit and no tag. This includes Zed commits that only
modify unrelated paths or root workspace fields removed by the projection.

## Daily automation

The repository's [sync workflow](../../.github/workflows/sync-gpui.yml) runs the
CLI every day at 02:17 UTC and can also be started manually. It pushes the sync
commit and generated date tag only when the configured crate trees changed.

## Check for updates

```sh
uv run --project tooling/zed-gpui-sync zed-gpui-sync --check
```

Exit statuses are:

- `0`: configured crate trees are unchanged
- `1`: at least one configured crate tree changed
- `2`: configuration, Git, network, or safety error

## Options

Local changes inside synchronized paths block a real update. Use `--force` only
when those changes may be replaced:

```sh
uv run --project tooling/zed-gpui-sync zed-gpui-sync --force
```

Temporary overrides are available for the source, ref, paths, and root
workspace packages:

```sh
uv run --project tooling/zed-gpui-sync zed-gpui-sync --ref <branch-or-tag>
uv run --project tooling/zed-gpui-sync zed-gpui-sync --source <git-url-or-path>
uv run --project tooling/zed-gpui-sync zed-gpui-sync \
  --path crates/gpui \
  --path crates/gpui_platform \
  --package gpui \
  --package gpui_platform
```

Run `uv run --project tooling/zed-gpui-sync zed-gpui-sync --help` for the full
command reference.

## Development

```sh
uv run --project tooling/zed-gpui-sync python -m unittest discover \
  -s tooling/zed-gpui-sync/tests -v

uv build --project tooling/zed-gpui-sync
```
