# GPUI standalone source mirror

This repository mirrors these directories from
[`zed-industries/zed`](https://github.com/zed-industries/zed):

- `crates/gpui`
- `crates/gpui_platform`

Run the sync with [`uv`](https://docs.astral.sh/uv/):

```sh
uv run sync_zed.py
```

The script fetches Zed's `main` branch with a sparse clone, replaces both local
crate directories, creates a scoped Git commit, and adds an annotated tag named
for the local sync date (`YYYY-MM-DD`). The tag annotation records the exact Zed
commit. If a second update is synced on the same day, the tool moves that day's
tool-managed tag to the newer sync commit.

Check without changing files:

```sh
uv run sync_zed.py --check
```

`--check` exits with status `0` when the recorded Zed commit is current and `1`
when an update is available. Network or configuration errors exit with status
`2`.

The two crate paths and `.zed-sync.json` are managed by the tool. A sync stops
if any of them contain local changes. Use `--force` only when those changes may
be replaced:

```sh
uv run sync_zed.py --force
```

Use a different branch, tag, or source repository when needed:

```sh
uv run sync_zed.py --ref <branch-or-tag>
uv run sync_zed.py --source <git-url-or-local-path> --ref <branch-or-tag>
```

The copied `Cargo.toml` files are intentionally kept byte-for-byte compatible
with the Zed monorepo. They inherit workspace metadata and depend on other Zed
workspace crates, so these first two crates alone are a source mirror rather
than a buildable standalone Cargo workspace. Add further crates to `SYNC_PATHS`
as the standalone workspace is expanded.
