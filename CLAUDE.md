# Public

Two folders: `cosmic/` (COSMIC desktop tools) and `local-ai/` (offline AI kit). MIT licensed.

- **Upstream projects are prerequisites, never copies.** No vendored code, no submodules; list each in `PREREQUISITES.md` with its repo URL and always target the latest upstream. Pins exist only where listed under "Where this policy bends" there.
- Nothing from private tooling or client work belongs here. Use neutral examples ("Example App").
- Write home paths as `~`, never an absolute `/home/<user>`.
- Every tool README keeps an **Outside this repo** section: what it installs, what it shadows, hardcoded paths, how to uninstall.
