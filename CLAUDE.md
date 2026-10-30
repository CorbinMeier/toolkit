# toolkit

Folders are dependencies: `libs/` (toolchain only) < `apt/` (needs apt) and `cosmic/` (needs COSMIC) < nothing depends on `archive/`. A tool lives in the folder named for the most specific thing it relies on; dependencies point down only. MIT licensed. Full rules in README.md.

- **Upstream projects are prerequisites, never copies.** No vendored code, no submodules; list each in `PREREQUISITES.md` with its repo URL and always target the latest upstream. Pins exist only where listed under "Where this policy bends" there.
- Nothing from private tooling or client work belongs here. Use neutral examples ("Example App").
- Write home paths as `~`, never an absolute `/home/<user>`.
- Every tool README keeps an **Outside this repo** section: what it installs, what it shadows, hardcoded paths, how to uninstall.
- **This repo is PUBLIC. Never commit secrets**: API keys, tokens, passwords, bearer-token or `.env*` files, TLS/private keys (`*.key`, `ca.key`), or copies of real config from `~/.config`. Use placeholders in examples and read real values from untracked files in the home folder. Never read, print or paste such files either (see the global rules on `.env`, `.pem`, `*secret*`, `*key*`, `*password*`).
- Before any commit, check the staged diff for secrets and for private paths or names. If one is already committed, say so and stop: it must be revoked and purged from history, not just deleted in a new commit. The user pushes; agents never push.
