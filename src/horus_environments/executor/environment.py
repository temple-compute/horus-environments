"""
Python environment executors for Horus tasks.
"""

import asyncio
import re
import shlex
from contextlib import aclosing
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.logging import horus_logger
from horus_runtime.settings import runtime_settings
from pydantic import Field

from horus_environments.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonEnvironmentExecutor(BaseExecutor):
    """
    Shared implementation for Python environment-backed executors.
    """

    add_to_registry: ClassVar[bool] = False

    runtimes: ClassVar[RuntimeFilterType] = (
        CommandRuntime,
        PythonCodeStringRuntime,
    )

    requirements: list[str] = Field(default_factory=list)
    """
    Python package requirements installed into the environment with pip.
    """

    env: dict[str, str] = Field(default_factory=dict)
    """
    Extra environment variables passed to the target process.
    """

    environment_dir: str = ".horus_python_environment"
    """
    Directory, relative to the task working directory, where the environment
    is created.
    """

    recreate: bool = False
    """
    Recreate the environment before running the task.
    """

    def _environment_path(self, task: "BaseTask") -> str:
        """Return the target-side path where the environment lives."""
        return f"{task.working_dir}/{self.environment_dir}"

    def _python_bin(self, task: "BaseTask") -> str:
        """Return the target-side Python executable inside the environment."""
        return f"{self._environment_path(task)}/bin/python"

    def _environment_log_name(self) -> str:
        """Return a human-readable backend name for setup logs."""
        return type(self).__name__

    def _log_command(self, message: str) -> str:
        """Return a shell command that emits one setup log line."""
        return f"printf '%s\\n' {shlex.quote(message)}"

    def _create_log_command(self, task: "BaseTask") -> str:
        """Return a shell log command for environment creation."""
        return self._log_command(
            _("Creating %(executor)s at %(path)s")
            % {
                "executor": self._environment_log_name(),
                "path": self._environment_path(task),
            }
        )

    def _reuse_log_command(self, task: "BaseTask") -> str:
        """Return a shell log command for environment reuse."""
        return self._log_command(
            _("Using existing %(executor)s at %(path)s")
            % {
                "executor": self._environment_log_name(),
                "path": self._environment_path(task),
            }
        )

    def _pip_install_command(self, task: "BaseTask") -> str | None:
        """Return the pip install command, or ``None`` with no requirements."""
        if not self.requirements:
            return None
        requirements = " ".join(shlex.quote(req) for req in self.requirements)
        return (
            f"{shlex.quote(self._python_bin(task))}"
            f" -m pip install {requirements}"
        )

    @staticmethod
    def _version_probe_snippet() -> str:
        """Python one-liner that prints the interpreter's ``major.minor``."""
        return (
            "import sys; "
            "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        )

    def _reuse_or_create_command(
        self, task: "BaseTask", create: str, version: str | None
    ) -> str:
        """
        Return a shell snippet that reuses the env when the interpreter exists
        (and, when ``version`` is given, matches ``major.minor``), otherwise
        wipes and recreates it with ``create``.
        """
        env_path = shlex.quote(self._environment_path(task))
        python_bin = shlex.quote(self._python_bin(task))
        if version is not None:
            probe = shlex.quote(self._version_probe_snippet())
            stale = (
                f"[ ! -x {python_bin} ]"
                f' || [ "$({python_bin} -c {probe} 2>/dev/null)"'
                f" != {shlex.quote(version)} ]"
            )
            return (
                f"if {stale};"
                f" then {self._create_log_command(task)}"
                f" && rm -rf {env_path} && {create};"
                f" else {self._reuse_log_command(task)};"
                f" fi"
            )
        return (
            f"if [ -x {python_bin} ];"
            f" then {self._reuse_log_command(task)};"
            f" else {self._create_log_command(task)} && {create};"
            f" fi"
        )

    def _create_environment_command(self, task: "BaseTask") -> str:
        """
        Return the shell command that ensures the environment exists.
        """
        raise NotImplementedError

    def _run_command(self, task: "BaseTask", prepared_command: str) -> str:
        """Return a command runtime invocation inside the environment."""
        activate = shlex.quote(f"{self._environment_path(task)}/bin/activate")
        return f". {activate} && /bin/sh -c {shlex.quote(prepared_command)}"

    def _run_python_script_command(
        self, task: "BaseTask", script_path: str
    ) -> str:
        """Return a Python runtime invocation inside the environment."""
        return (
            f"{shlex.quote(self._python_bin(task))} {shlex.quote(script_path)}"
        )

    def _setup_commands(self, task: "BaseTask") -> list[str]:
        """Return all shell commands required before the runtime executes."""
        commands: list[str] = []
        env_path = shlex.quote(self._environment_path(task))
        if self.recreate:
            commands.append(
                self._log_command(
                    _("Recreating Python environment at %(path)s")
                    % {"path": self._environment_path(task)}
                )
            )
            commands.append(f"rm -rf {env_path}")
        commands.append(self._create_environment_command(task))
        install = self._pip_install_command(task)
        if install is not None:
            commands.append(
                self._log_command(
                    _(
                        "Installing %(count)d Python requirement(s) into "
                        "%(path)s"
                    )
                    % {
                        "count": len(self.requirements),
                        "path": self._environment_path(task),
                    }
                )
            )
            commands.append(install)
        return commands

    async def _runtime_command(self, task: "BaseTask") -> str:
        """Prepare the task runtime and return its environment command."""
        if isinstance(task.runtime, CommandRuntime):
            command = await task.runtime.setup_runtime(task)
            return self._run_command(task, command)

        if isinstance(task.runtime, PythonCodeStringRuntime):
            code = await task.runtime.setup_runtime(task)
            script_path = f"{task.working_dir}/.horus_python_runtime.py"
            await task.target.put_file(code.encode(), script_path)
            return self._run_python_script_command(task, script_path)

        raise TaskExecutionError(
            _("Unsupported runtime %(runtime)s for %(executor)s")
            % {
                "runtime": type(task.runtime).__name__,
                "executor": type(self).__name__,
            }
        )

    async def _execute(self, task: "BaseTask") -> None:
        """
        Provision the selected Python environment and execute the runtime.
        """
        await task.target.mkdir(task.working_dir)
        run_command = await self._runtime_command(task)
        full_command = " && ".join([*self._setup_commands(task), run_command])

        horus_logger.log.debug(
            _("Executing task %(task_id)s in %(executor)s: %(command)s")
            % {
                "task_id": task.id,
                "executor": type(self).__name__,
                "command": full_command,
            }
        )

        env = {
            **self.env,
            runtime_settings.SIDE_ARTIFACTS_DIR_ENV: str(
                task.side_artifacts_dir
            ),
        }
        proc = await task.target.run_command(
            full_command,
            cwd=task.working_dir,
            env=env,
        )

        try:
            async with aclosing(proc.stream()) as stream:
                async for stream_name, line in stream:
                    if stream_name == "stdout":
                        horus_logger.log.info(line.decode("utf-8").rstrip())
                    elif stream_name == "stderr":
                        horus_logger.log.warning(line.decode("utf-8").rstrip())
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        # Draining the stream hits EOF but doesn't reap the process; wait() is
        # what yields a reliable returncode (matches ShellExecutor).
        await proc.wait()
        if proc.returncode != 0:
            raise TaskExecutionError(
                _(
                    "Python environment command exited with return code "
                    "%(code)s"
                )
                % {"code": proc.returncode}
            )


class CondaPythonEnvironmentExecutor(PythonEnvironmentExecutor):
    """
    Execute tasks in a Conda environment on the task target.
    """

    add_to_registry: ClassVar[bool] = True

    kind: str = "conda_python_environment"
    kind_name: ClassVar[str] = "Conda Python Environment"
    kind_description: ClassVar[str] = _(
        "Executes command and Python runtimes inside a Conda environment."
    )

    conda: str = "conda"
    """Conda executable available on the target."""

    python_version: str | None = None
    """Optional Python version passed to ``conda create``."""

    channels: list[str] = Field(default_factory=list)
    """Conda channels, passed as ``-c`` flags in the given order."""

    conda_requirements: list[str] = Field(default_factory=list)
    """
    Packages installed from conda channels (as opposed to the pip-installed
    ``requirements``). They are baked into the ``conda create`` line, so they
    are resolved once when the environment is provisioned; change them with
    ``recreate: true`` (or a fresh ``environment_dir``) to force a rebuild.
    """

    environment_file: str | None = None
    """
    Path (on the target) to a conda ``environment.yaml``. When set the env is
    created from it with ``conda env create -f`` and ``channels`` /
    ``conda_requirements`` / ``python_version`` are ignored — the file is
    authoritative. Pip ``requirements`` are still installed afterwards.
    """

    def _environment_log_name(self) -> str:
        """Return a human-readable backend name for setup logs."""
        return "Conda Python environment"

    def _channel_args(self) -> str:
        """Return the ``-c <channel>`` flags for the configured channels."""
        return " ".join(f"-c {shlex.quote(c)}" for c in self.channels)

    def _create_environment_command(self, task: "BaseTask") -> str:
        """Return the Conda environment creation command."""
        env_path = shlex.quote(self._environment_path(task))
        conda = shlex.quote(self.conda)

        if self.environment_file is not None:
            create = (
                f"{conda} env create"
                f" -f {shlex.quote(self.environment_file)} -p {env_path}"
            )
            # The file owns the interpreter; reuse on existence (no version
            # probe).
            return self._reuse_or_create_command(task, create, None)

        parts = [f"{conda} create -y"]
        channels = self._channel_args()
        if channels:
            parts.append(channels)
        parts.append(f"-p {env_path}")
        parts.append(
            f"python={shlex.quote(self.python_version)}"
            if self.python_version
            else "python"
        )
        parts.append("pip")
        parts.extend(shlex.quote(req) for req in self.conda_requirements)
        create = " ".join(parts)
        return self._reuse_or_create_command(
            task, create, self._requested_python_version()
        )

    def _requested_python_version(self) -> str | None:
        """Return the requested ``major.minor`` version, if one was given."""
        if self.python_version is None:
            return None
        match = re.search(
            r"(?<!\d)(\d+\.\d+)(?:\.\d+)?(?!\d)", self.python_version
        )
        return match.group(1) if match else None

    def _pip_install_command(self, task: "BaseTask") -> str | None:
        """Install requirements through ``conda run``."""
        if not self.requirements:
            return None
        conda = shlex.quote(self.conda)
        env_path = shlex.quote(self._environment_path(task))
        requirements = " ".join(shlex.quote(req) for req in self.requirements)
        return (
            f"{conda} run -p {env_path} python -m pip install {requirements}"
        )

    def _run_flags(self) -> str:
        """Return extra flags for ``<conda> run``.

        ``conda run`` buffers stdout/stderr and only flushes at the end, which
        would hide Horus's live logs, so we pass ``--no-capture-output``. The
        drop-in replacements ``mamba``/``micromamba`` exec the command directly
        (already unbuffered) and *reject* that flag, so it is only emitted for
        real ``conda``.
        """
        executable = self.conda.rsplit("/", 1)[-1]
        return "" if "mamba" in executable else "--no-capture-output "

    def _run_command(self, task: "BaseTask", prepared_command: str) -> str:
        """Run a shell command with ``conda run``."""
        conda = shlex.quote(self.conda)
        env_path = shlex.quote(self._environment_path(task))
        return (
            f"{conda} run {self._run_flags()}-p {env_path}"
            f" /bin/sh -c {shlex.quote(prepared_command)}"
        )

    def _run_python_script_command(
        self, task: "BaseTask", script_path: str
    ) -> str:
        """Run a Python script with ``conda run``."""
        conda = shlex.quote(self.conda)
        env_path = shlex.quote(self._environment_path(task))
        return (
            f"{conda} run {self._run_flags()}-p {env_path}"
            f" python {shlex.quote(script_path)}"
        )


class UvPythonEnvironmentExecutor(PythonEnvironmentExecutor):
    """
    Execute tasks in a uv-managed virtual environment on the task target.
    """

    add_to_registry: ClassVar[bool] = True

    kind: str = "uv_python_environment"
    kind_name: ClassVar[str] = "uv Python Environment"
    kind_description: ClassVar[str] = _(
        "Executes command and Python runtimes inside a uv virtual environment."
    )

    uv: str = "uv"
    """uv executable available on the target."""

    python: str | None = None
    """Optional interpreter or Python version passed to ``uv venv``."""

    def _environment_log_name(self) -> str:
        """Return a human-readable backend name for setup logs."""
        return "uv Python environment"

    def _requested_python_version(self) -> str | None:
        """
        Return the requested major.minor version when it can be inferred.
        """
        if self.python is None:
            return None
        match = re.search(r"(?<!\d)(\d+\.\d+)(?:\.\d+)?(?!\d)", self.python)
        return match.group(1) if match else None

    def _create_environment_command(self, task: "BaseTask") -> str:
        """Return the uv venv creation command."""
        env_path = shlex.quote(self._environment_path(task))
        uv = shlex.quote(self.uv)
        python = f" --python {shlex.quote(self.python)}" if self.python else ""
        create = f"{uv} venv{python} {env_path}"
        return self._reuse_or_create_command(
            task, create, self._requested_python_version()
        )

    def _pip_install_command(self, task: "BaseTask") -> str | None:
        """Install requirements through uv pip."""
        if not self.requirements:
            return None
        uv = shlex.quote(self.uv)
        requirements = " ".join(shlex.quote(req) for req in self.requirements)
        return (
            f"{uv} pip install --python {shlex.quote(self._python_bin(task))}"
            f" {requirements}"
        )


class VirtualenvPythonEnvironmentExecutor(PythonEnvironmentExecutor):
    """
    Execute tasks in a standard-library venv on the task target.
    """

    add_to_registry: ClassVar[bool] = True

    kind: str = "virtualenv_python_environment"
    kind_name: ClassVar[str] = "Virtualenv Python Environment"
    kind_description: ClassVar[str] = _(
        "Executes command and Python runtimes inside a Python virtualenv."
    )

    python: str = "python"
    """Python interpreter used to create the virtual environment."""

    def _environment_log_name(self) -> str:
        """Return a human-readable backend name for setup logs."""
        return "virtualenv Python environment"

    def _create_environment_command(self, task: "BaseTask") -> str:
        """Return the stdlib venv creation command."""
        env_path = shlex.quote(self._environment_path(task))
        return (
            f"if [ -x {shlex.quote(self._python_bin(task))} ];"
            f" then {self._reuse_log_command(task)};"
            f" else {self._create_log_command(task)}"
            f" && {shlex.quote(self.python)} -m venv {env_path};"
            f" fi"
        )
