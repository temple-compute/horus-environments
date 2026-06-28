# my-plugin

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A template for building plugins for [horus-runtime](https://github.com/temple-compute/horus-runtime).

---

## Overview

**horus-runtime** loads plugins through Python [entry points](https://packaging.python.org/en/latest/specifications/entry-points/). Any package that declares an entry point under one of the `horus.*` groups is automatically discovered and registered when `HorusContext.boot()` is called.

This repository is a batteries-included starting point. It ships with:

- A concrete `CustomTask` that extends `BaseTask`.
- A plugin-scoped i18n module (`my_plugin.i18n`) so your strings can be translated independently of the runtime.
- A `pytest` test skeleton with a shared `HorusContext` fixture.
- A `Makefile` with the same targets used by the runtime itself (lint, type-check, format, i18n, …).
- Pre-commit hooks for ruff, mypy, and Babel completeness checks.

---

## Repository structure

```
src/
└── my_plugin/
    ├── __init__.py
    ├── i18n.py                  # plugin-scoped gettext wrapper
    ├── locale/
    │   └── messages.pot         # translatable strings template
    └── task/
        ├── __init__.py
        └── custom_task.py       # example task
tests/
├── __init__.py
├── conftest.py                  # shared fixtures (registry, HorusContext)
└── unit/
    ├── __init__.py
    └── test_custom_task.py
babel.cfg
Makefile
pyproject.toml
```

---

## Extending horus-runtime

### Extension points

The runtime defines the following entry point groups. A plugin can contribute to one or more of them by declaring the group and pointing it at the module that defines the subclass.

| Group | Base class | `registry_key` field | Purpose |
|---|---|---|---|
| `horus.task` | `BaseTask` | `kind` | Unit of work executed by the runtime |
| `horus.artifact` | `BaseArtifact` | `kind` | Data produced or consumed by a task |
| `horus.executor` | `BaseExecutor` | `kind` | Runs a task in a specific environment (local, SLURM, …) |
| `horus.runtime` | `BaseRuntime` | `kind` | Prepares the command/script handed to an executor |
| `horus.target` | `BaseTarget` | `kind` | Describes *where* a task is dispatched |
| `horus.event` | `BaseEvent` | `event_type` | Structured event emitted on the event bus |

### How registration works

When `HorusContext.boot()` runs, it calls `AutoRegistry.init_registry()`, which:

1. Iterates every installed package's `horus.*` entry point groups.
2. Calls `.load()` on each entry point, importing the declared module.
3. The act of importing the module executes the class body, which triggers `AutoRegistry` and adds the class to the correct registry under its discriminator key.

Declaring an entry point is therefore sufficient, you do not need to call any registration function manually.

### Adding a task

Subclass `BaseTask`, set a globally unique `kind`, implement `_run()`, and declare the entry point in `pyproject.toml` under `[project.entry-points."horus.task"]`. Re-sync with `uv sync`.

### Adding an artifact

Subclass `BaseArtifact` and implement `exists()`, `hash()`, and `size()`. Declare the entry point under `[project.entry-points."horus.artifact"]`.

### Adding an executor

Subclass `BaseExecutor` and implement `execute()`. Optionally restrict accepted runtimes via the `runtimes` class variable. Declare the entry point under `[project.entry-points."horus.executor"]`.

### Adding a custom event

Subclass `BaseEvent`, set `event_type`, and emit via `ctx.bus.emit(...)`. Subscribe by subclassing `HorusEventSubscriber` and adding a matching `on_<event_type>` method.

> Full class signatures, field references, and worked examples are at [docs.templecompute.com](https://docs.templecompute.com/docs/sdk/overview).

---

## Internationalization (i18n)

Each plugin maintains its **own** gettext domain and locale directory, independent of the runtime's translations.

### How it works

`src/my_plugin/i18n.py` wraps Python's `gettext` module, looking for compiled `.mo` files in `src/my_plugin/locale/<lang>/LC_MESSAGES/my_plugin.mo`. If no catalog exists for the detected locale, it falls back to returning the original string unchanged.

Import the wrapper as `_` (required by Babel's extractor) in any module with user-visible strings. Use `make babel-extract` → edit `.po` → `make babel-check` to update translations. The pre-commit hook prevents committing incomplete catalogs.

> Full i18n workflow and plural-form reference: [docs.templecompute.com](https://docs.templecompute.com/docs/sdk/i18n).

---

## Development

### Requirements

- Python ≥ 3.13
- `horus-runtime` ≥ 0.1.1 (install from source or PyPI)
- [uv](https://docs.astral.sh/uv/) for managing the virtual environment and dependencies

### Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and
[hatchling](https://hatch.pypa.io/) + [`uv-dynamic-versioning`](https://github.com/ninoseki/uv-dynamic-versioning)
for packaging. The version is derived from git tags (`vX.Y.Z`), falling back to
`0.0.1` when no tag is present — never hard-code a version.

```bash
# Create the virtualenv and install the project + dev dependencies
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install
```

Run any command inside the environment with `uv run ...` (e.g. `uv run make test`).
Cut a release by pushing a tag: `git tag v0.1.0 && git push --tags`, then publish a
GitHub release — the `release` workflow builds and uploads to PyPI.

### Common commands

| Command | Description |
|---|---|
| `make test` | Run the full test suite with coverage |
| `make lint` | ruff + mypy |
| `make format` | Auto-fix with ruff |
| `make type-check` | mypy only |
| `make babel-extract` | Update `messages.pot` |
| `make babel-add LANG=es` | Add a new language |
| `make babel-check` | Verify all strings are translated |
| `make clean` | Remove build artefacts and caches |

---

## License

MIT (`license = "MIT"` in `pyproject.toml`). Add a `LICENSE` file with your
copyright holder if you intend to distribute.
