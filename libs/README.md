# libs

Code that needs **only a language toolchain**: no desktop, no distro, no package manager. These are the only things `apt/` and `cosmic/` tools may depend on.

| Library | Language | Used by |
|---|---|---|
| [`claude-usage-core`](claude-usage-core/README.md) | Rust | `cosmic/applet-claude-usage`, `apt/vitals` |

## Rules
- A library here depends on nothing else in this repo.
- Consumers reach it by relative `path` (for example `../../libs/claude-usage-core`), so moving a library means editing every consumer's `Cargo.toml`.
- Libraries install nothing; they have no `scripts/uninstall.sh`.
