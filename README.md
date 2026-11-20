# horus-environments

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [horus-runtime](https://pypi.org/project/horus-runtime/) plugin that runs
Horus tasks inside an isolated Python environment it provisions for you. Point a
task at one of these executors and it will create the environment on the task
target (once, then reuse it on later runs), `pip install` your requirements, and
run your command or Python code inside it.

Three backends, one interface:

| `kind` | Backend | Creates the env with |
| --- | --- | --- |
| `conda_python_environment` | Conda | `conda create -p <dir>` |
| `uv_python_environment` | [uv](https://docs.astral.sh/uv/) | `uv venv <dir>` |
| `virtualenv_python_environment` | stdlib `venv` | `python -m venv <dir>` |

## Install

```bash
pip install horus-environments
```

## Options

All backends share these fields:

| Field | Default | Purpose |
| --- | --- | --- |
| `requirements` | `[]` | Packages pip-installed into the env |
| `env` | `{}` | Extra environment variables for the process |
| `environment_dir` | `.horus_python_environment` | Env location, relative to the task working dir. Left at the default, the env is shared (see below) |
| `recreate` | `false` | Wipe and rebuild the env before running. With the default `environment_dir`, the env is built in the task working dir instead of the shared one |

Backend-specific:

- **conda** — `conda` (executable, default `"conda"`), `python_version` (e.g. `"3.12"`), plus:
  - `channels` — conda channels searched for packages, e.g. `["conda-forge"]` (passed as `-c` in order)
  - `conda_requirements` — packages installed **from conda channels** (as opposed to the pip-installed `requirements`)
  - `environment_file` — path (on the machine running Horus) to a conda `environment.yaml`; it is uploaded to the target and the env is built from it there with `conda env create -f`, so the same config works on local and remote targets. `channels`/`conda_requirements`/`python_version` are ignored when set (pip `requirements` still install afterwards)
- **uv** — `uv` (executable, default `"uv"`), `python` (interpreter or version)
- **virtualenv** — `python` (interpreter used to build the venv, default `"python"`)

With the default `environment_dir`, the env is shared across tasks and runs: it
lives in `$HORUS_ENV_ROOT` on the target (else `.horus_envs` in the target
working directory), in a `conda-<hash>` or `venv-<hash>` directory keyed by
the backend, `requirements`, interpreter, conda packages and channels, the
`environment_file` content, `env`, and the `PIP_*`, `UV_*`, `CONDA*` and
`MAMBA*` variables on the target. Any change builds a new env. A shared env is
reused only once it is fully built (`.horus-ready` inside it) and its
interpreter exists; otherwise it is moved to `<env>.stale.<pid>` and rebuilt
(a leftover owned by another user stays there). Pip does not run again
on reuse, and a ready env is read-only: task commands must not install into it.
Its files are copies, not hard links into the conda, mamba or uv package cache.

Pin versions: a shared env keeps the versions of its first build. To rebuild,
change the spec or set `environment_dir`.

The env stays per task when the target has no `flock`, or when `requirements`
or the `environment_file` refer to local paths, URLs or pip options (`.`,
`-e`, `-r`, `pkg @ file:...`), since the hash cannot see their content.

`HORUS_ENV_ROOT` is read from the target's environment, not from `env`. It is
set by whoever runs the target; a platform that lets users define environment
variables or secrets for their tasks should reserve this name.

With an explicit `environment_dir`, an existing env (and, when you pin a
version, a matching interpreter) is reused instead of rebuilt.
`conda_requirements` are baked into `conda create`, so they resolve once at
provisioning time — change them with `recreate: true` to force a rebuild.
Set `conda` to `mamba` or `micromamba` if
that's your executable — the executor detects them and drops the conda-only
`--no-capture-output` flag they don't understand. (`conda env create -f` is
conda/mamba syntax; micromamba uses `micromamba create -f`.)

## Examples

### Run a shell command in a uv environment

```python
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_environments.executor.environment import (
    UvPythonEnvironmentExecutor,
)

task = HorusTask(
    id="fetch-report",
    name="fetch_report",
    executor=UvPythonEnvironmentExecutor(
        python="3.13",
        requirements=["requests==2.32.*"],
    ),
    runtime=CommandRuntime(command="python -m mypackage --report"),
)
await task.execute()
```

### Run Python code in a pinned Conda environment

```python
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_environments.executor.environment import (
    CondaPythonEnvironmentExecutor,
)

task = HorusTask(
    id="analyze",
    name="analyze",
    executor=CondaPythonEnvironmentExecutor(
        python_version="3.11",
        requirements=["pandas", "numpy"],
    ),
    runtime=PythonCodeStringRuntime(
        code="import pandas as pd; print(pd.__version__)",
    ),
)
await task.execute()
```

### Install compiled tools from conda-forge

For packages that ship prebuilt binaries on conda (no wheels, or C/C++
dependencies), install them from a channel instead of pip:

```python
executor = CondaPythonEnvironmentExecutor(
    python_version="3.11",
    channels=["conda-forge"],
    conda_requirements=["vina", "openbabel", "rdkit"],
    requirements=["some-pip-only-extra"],  # optional pip packages too
)
```

Or provision a whole environment from a file. The file is read on the machine
running Horus and uploaded to the target, so it works with remote targets too:

```python
executor = CondaPythonEnvironmentExecutor(environment_file="environment.yaml")
```

### Throwaway virtualenv, rebuilt every run

```python
from horus_environments.executor.environment import (
    VirtualenvPythonEnvironmentExecutor,
)

executor = VirtualenvPythonEnvironmentExecutor(
    python="python3.13",
    environment_dir=".venv-ci",
    recreate=True,
)
```

### As a workflow task (YAML)

Set `executor.kind` on any `horus_task` and the environment options become task
fields. Here a task runs `boltz` inside a uv-provisioned env pinned to Python
3.11:

```yaml
kind: horus_workflow
name: Boltz2 Virtual Screening
tasks:
  - kind: horus_task
    id: predict
    name: Boltz-2 structure + affinity
    executor:
      kind: uv_python_environment   # or conda_/virtualenv_python_environment
      python: "3.11"
      requirements:
        - boltz
      env:
        TZ: UTC
    runtime:
      kind: command
      command: boltz predict inputs --out_dir out --accelerator cpu
    target:
      kind: local
```

## License

MIT — see [LICENSE](LICENSE).
