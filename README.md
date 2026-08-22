# GPUI standalone

This repository is a date-tagged source mirror of selected GPUI crates from
Zed. The first mirrored crates are:

- `crates/gpui`
- `crates/gpui_platform`

## Add the crates to an application

Use the latest versions from the Git repository's default branch:

```toml
[dependencies]
gpui = { git = "https://github.com/realab/gpui-standalone.git", version = "*" }
gpui_platform = { git = "https://github.com/realab/gpui-standalone.git", version = "*" }
```

Cargo records the resolved Git revision in `Cargo.lock`. Run
`cargo update -p gpui -p gpui_platform` to refresh an existing project to the
latest synchronized revision.

To pin both crates to a specific synchronized snapshot, use the same dated tag
for each dependency:

```toml
[dependencies]
gpui = { git = "https://github.com/realab/gpui-standalone.git", tag = "2026-08-21" }
gpui_platform = { git = "https://github.com/realab/gpui-standalone.git", tag = "2026-08-21" }
```

Date tags use `YYYY-MM-DD` and represent the latest upstream commit date among
all synchronized crate paths.
