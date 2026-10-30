# apt

Tools that need a **Linux machine with apt** (Debian, Ubuntu, Pop!_OS) and nothing desktop-specific. General Linux scripts that find their way into this repo go here.

| Tool | What it is |
|---|---|
| [`vitals`](vitals/README.md) | Terminal and web system dashboard. Linux-only (reads `/proc`); apt supplies the build toolchain. |
| [`local-ai`](local-ai/README.md) | Offline AI kit and Parakeet dictation. Needs apt packages such as `git`, `curl`, `docker`, `wtype` and `parecord` (see its README). |

## Moving to another distro
These tools name apt packages in their READMEs and install scripts. On pacman, dnf or similar, copy a tool into a sibling folder (`pacman/`, `dnf/`) and change the package names there. Keep the dependency rule from the root README: such a folder may depend on `libs/`, never on another distro folder.

## Rules
- May depend on `libs/`. Never on `cosmic/` or `archive/`.
