# GPUI standalone

This repository is a date-tagged source mirror of selected GPUI crates from
Zed. The first mirrored crates are:

- `crates/gpui`
- `crates/gpui_platform`

## Use the synchronized crates

Patch Zed's GPUI dependencies to follow the latest synchronized `main` branch:

```toml
[patch."https://github.com/zed-industries/zed"]
gpui = { git = "https://github.com/realab/gpui-standalone.git", branch = "main" }
gpui_macros = { git = "https://github.com/realab/gpui-standalone.git", branch = "main" }
```

Cargo pins the selected Git commit in `Cargo.lock`. Run
`cargo update -p gpui -p gpui_macros` to update an existing project.

For a reproducible build, patch both crates to the same dated tag:

```toml
[patch."https://github.com/zed-industries/zed"]
gpui = { git = "https://github.com/realab/gpui-standalone.git", tag = "2026-08-21" }
gpui_macros = { git = "https://github.com/realab/gpui-standalone.git", tag = "2026-08-21" }
```

Date tags use `YYYY-MM-DD` and represent the latest upstream commit date among
all synchronized crate paths.
