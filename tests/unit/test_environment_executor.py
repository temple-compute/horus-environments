"""Unit tests for Python environment executors."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.task.exceptions import TaskExecutionError

from horus_environments.executor.environment import (
    CondaPythonEnvironmentExecutor,
    PythonEnvironmentExecutor,
    UvPythonEnvironmentExecutor,
    VirtualenvPythonEnvironmentExecutor,
)

_NONZERO_CODE = 7


def _make_mock_proc(
    returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> AsyncMock:
    """Return an AsyncMock channel process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)

    async def _stream() -> object:
        for line in stdout.splitlines(keepends=True):
            yield ("stdout", line)
        for line in stderr.splitlines(keepends=True):
            yield ("stderr", line)

    # The executor consumes proc.stream() with `async for`, so stream must
    # return an async generator (not an awaitable).
    proc.stream = MagicMock(return_value=_stream())
    return proc


def _make_mock_target(proc: AsyncMock | None = None) -> MagicMock:
    """Return a mock target whose channel methods are async."""
    target = MagicMock()
    target.working_directory = "/tmp/horus"
    target.mkdir = AsyncMock()
    target.put_file = AsyncMock()
    target.run_command = AsyncMock(return_value=proc or _make_mock_proc())
    return target


def _make_command_task(
    executor: PythonEnvironmentExecutor, command: str = "python --version"
) -> HorusTask:
    """Create a HorusTask using a command runtime."""
    return HorusTask(
        id="task-1",
        name="task_1",
        executor=executor,
        runtime=CommandRuntime(command=command),
    )


@pytest.mark.unit
class TestEnvironmentExecutorRegistration:
    """Verify concrete executors register under separate kinds."""

    def test_base_executor_is_not_registered(self) -> None:
        """The shared base class must not appear in the registry."""
        assert PythonEnvironmentExecutor not in BaseExecutor.registry.values()

    @pytest.mark.parametrize(
        ("kind", "executor_cls"),
        [
            ("conda_python_environment", CondaPythonEnvironmentExecutor),
            ("uv_python_environment", UvPythonEnvironmentExecutor),
            (
                "virtualenv_python_environment",
                VirtualenvPythonEnvironmentExecutor,
            ),
        ],
    )
    def test_concrete_executors_are_registered(
        self,
        kind: str,
        executor_cls: type[PythonEnvironmentExecutor],
    ) -> None:
        """Each environment backend must have its own registry kind."""
        assert BaseExecutor.registry[kind] is executor_cls

    def test_runtimes_filter_allows_commands_and_python(self) -> None:
        """Environment executors support command and Python string runtimes."""
        assert CommandRuntime in PythonEnvironmentExecutor.runtimes
        assert PythonCodeStringRuntime in PythonEnvironmentExecutor.runtimes


@pytest.mark.unit
class TestEnvironmentCommandBuilders:
    """Verify backend-specific shell command generation."""

    def test_virtualenv_setup_uses_stdlib_venv(self) -> None:
        """Virtualenv executor creates a venv with the configured Python."""
        executor = VirtualenvPythonEnvironmentExecutor(python="python3.13")
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "Creating virtualenv Python environment" in command
        assert "Using existing virtualenv Python environment" in command
        assert "python3.13 -m venv" in command
        assert ".horus_python_environment" in command

    def test_uv_setup_uses_uv_venv_with_python(self) -> None:
        """Uv executor creates the environment through uv."""
        executor = UvPythonEnvironmentExecutor(python="3.13")
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "sys.version_info.major" in command
        assert "rm -rf" in command
        assert "Creating uv Python environment" in command
        assert "Using existing uv Python environment" in command
        assert "uv venv --python 3.13" in command
        assert "!= 3.13" in command

    def test_uv_setup_detects_interpreter_style_version(self) -> None:
        """Uv extracts major.minor versions from interpreter-like strings."""
        executor = UvPythonEnvironmentExecutor(python="python3.11")
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "uv venv --python python3.11" in command
        assert "!= 3.11" in command

    def test_conda_setup_uses_conda_create_with_python_version(self) -> None:
        """Conda executor creates the environment with conda create."""
        executor = CondaPythonEnvironmentExecutor(python_version="3.13")
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "Creating Conda Python environment" in command
        assert "Using existing Conda Python environment" in command
        assert "conda create -y -p" in command
        assert "python=3.13" in command
        assert " pip" in command
        # A pinned version must recreate the env when it drifts, not blindly
        # reuse whatever interpreter already exists.
        assert "sys.version_info.major" in command
        assert "!= 3.13" in command
        assert "rm -rf" in command

    def test_conda_setup_without_version_uses_existence_check(self) -> None:
        """Without a pinned version, conda reuses any existing interpreter."""
        executor = CondaPythonEnvironmentExecutor()
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "conda create -y -p" in command
        assert "python " in command  # unpinned interpreter
        assert "sys.version_info.major" not in command
        assert "rm -rf" not in command

    def test_conda_setup_includes_channels_and_conda_reqs(self) -> None:
        """Channels and conda packages are baked into the create command."""
        executor = CondaPythonEnvironmentExecutor(
            python_version="3.11",
            channels=["conda-forge", "bioconda"],
            conda_requirements=["vina", "rdkit"],
        )
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "conda create -y" in command
        assert "-c conda-forge" in command
        assert "-c bioconda" in command
        assert "python=3.11" in command
        assert " pip " in command
        assert " vina" in command
        assert " rdkit" in command
        # Channels precede the -p target, packages follow it.
        assert command.index("-c conda-forge") < command.index("-p ")
        assert command.index("-p ") < command.index("vina")

    def test_conda_setup_shell_quotes_conda_requirements(self) -> None:
        """Conda package specs are shell-quoted."""
        executor = CondaPythonEnvironmentExecutor(
            conda_requirements=["numpy>=2", "some package"]
        )
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        assert "'numpy>=2'" in command
        assert "'some package'" in command

    def test_conda_setup_from_environment_file(self) -> None:
        """An environment_file is created with `conda env create -f`."""
        executor = CondaPythonEnvironmentExecutor(
            environment_file="env.yaml",
            # These are ignored when a file is given.
            channels=["conda-forge"],
            conda_requirements=["vina"],
            python_version="3.11",
        )
        task = _make_command_task(executor)

        command = executor._create_environment_command(task)

        # The command points at the staged (uploaded) copy, not the local path.
        assert "conda env create -f" in command
        assert "/.horus_conda_environment.yaml -p" in command
        assert "-f env.yaml" not in command
        assert "vina" not in command
        assert "-c conda-forge" not in command
        # No version probe when the file owns the interpreter.
        assert "sys.version_info.major" not in command

    def test_requirements_are_installed_with_pip(self) -> None:
        """Requirements are shell-quoted and installed into the environment."""
        executor = VirtualenvPythonEnvironmentExecutor(
            requirements=["numpy==2.0", "my package"]
        )
        task = _make_command_task(executor)

        command = executor._pip_install_command(task)

        assert command is not None
        assert "-m pip install" in command
        assert "numpy==2.0" in command
        assert "'my package'" in command

    def test_conda_requirements_install_uses_conda_run(self) -> None:
        """Conda installs requirements from inside the Conda environment."""
        executor = CondaPythonEnvironmentExecutor(requirements=["pandas"])
        task = _make_command_task(executor)

        command = executor._pip_install_command(task)

        assert command is not None
        assert command.startswith("conda run -p")
        assert "python -m pip install pandas" in command

    def test_conda_without_requirements_skips_install(self) -> None:
        """Conda does not emit an install command without requirements."""
        executor = CondaPythonEnvironmentExecutor()
        task = _make_command_task(executor)

        assert executor._pip_install_command(task) is None

    def test_uv_requirements_install_uses_uv_pip(self) -> None:
        """Uv installs requirements through uv pip."""
        executor = UvPythonEnvironmentExecutor(requirements=["httpx"])
        task = _make_command_task(executor)

        command = executor._pip_install_command(task)

        assert command is not None
        assert command.startswith("uv pip install --python")
        assert " httpx" in command

    def test_recreate_removes_environment_before_setup(self) -> None:
        """recreate=True removes the old environment before creation."""
        executor = VirtualenvPythonEnvironmentExecutor(recreate=True)
        task = _make_command_task(executor)

        commands = executor._setup_commands(task)

        assert "Recreating Python environment" in commands[0]
        assert commands[1].startswith("rm -rf")
        assert "python -m venv" in commands[2]

    def test_requirements_install_logs_count(self) -> None:
        """Requirement setup logs the install count before pip runs."""
        executor = VirtualenvPythonEnvironmentExecutor(
            requirements=["rich", "httpx"]
        )
        task = _make_command_task(executor)

        commands = executor._setup_commands(task)

        assert "Installing 2 Python requirement(s)" in commands[-2]
        assert "-m pip install rich httpx" in commands[-1]

    def test_command_runtime_is_wrapped_in_environment_shell(self) -> None:
        """Command runtimes execute with the virtualenv activated."""
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)

        command = executor._run_command(task, "python -c 'print(1)'")

        expected = ".horus_python_environment/bin/activate && /bin/sh -c"
        assert expected in command
        assert "/bin/sh -c" in command

    def test_conda_runtime_commands_use_conda_run(self) -> None:
        """Conda wraps commands with `conda run --no-capture-output`."""
        executor = CondaPythonEnvironmentExecutor(conda="conda")
        task = _make_command_task(executor)

        command = executor._run_command(task, "echo hi")
        python = executor._run_python_script_command(task, "/tmp/run.py")

        assert command.startswith("conda run --no-capture-output -p")
        assert "/bin/sh -c" in command
        assert python.startswith("conda run --no-capture-output -p")
        assert " python /tmp/run.py" in python

    def test_mamba_runtime_commands_omit_no_capture_output(self) -> None:
        """mamba/micromamba reject --no-capture-output; it must be omitted."""
        for exe in ("mamba", "micromamba", "/opt/homebrew/bin/micromamba"):
            executor = CondaPythonEnvironmentExecutor(conda=exe)
            task = _make_command_task(executor)

            command = executor._run_command(task, "echo hi")
            python = executor._run_python_script_command(task, "/tmp/run.py")

            assert "--no-capture-output" not in command
            assert "--no-capture-output" not in python
            assert command.startswith(f"{exe} run -p")
            assert python.startswith(f"{exe} run -p")


@pytest.mark.unit
class TestEnvironmentExecutorExecute:
    """Verify end-to-end executor behavior against the target channel."""

    @pytest.mark.asyncio
    async def test_execute_runs_setup_install_and_command(
        self, horus_context: HorusContext
    ) -> None:
        """A command runtime is provisioned and delegated to run_command."""
        del horus_context
        executor = VirtualenvPythonEnvironmentExecutor(
            requirements=["rich"], env={"EXTRA": "1"}
        )
        task = _make_command_task(executor, "python -c 'print(42)'")
        target = _make_mock_target(_make_mock_proc(stdout=b"42\n"))

        with patch.object(task, "target", target):
            await executor._execute(task)

        expected_working_dir = "/tmp/horus/task-1"
        target.mkdir.assert_called_once_with(expected_working_dir)
        target.run_command.assert_called_once()
        command = target.run_command.call_args[0][0]
        assert "python -m venv" in command
        assert "-m pip install rich" in command
        assert "/bin/sh -c" in command
        kwargs = target.run_command.call_args.kwargs
        assert kwargs["cwd"] == expected_working_dir
        assert kwargs["env"]["EXTRA"] == "1"

    @pytest.mark.asyncio
    async def test_execute_python_string_uploads_and_runs_script(
        self, horus_context: HorusContext
    ) -> None:
        """Python string runtimes are written as a target-side script."""
        del horus_context
        executor = UvPythonEnvironmentExecutor()
        task = HorusTask(
            id="task-2",
            name="task_2",
            executor=executor,
            runtime=PythonCodeStringRuntime(code="print('hi')"),
        )
        target = _make_mock_target()

        with patch.object(task, "target", target):
            await executor._execute(task)

        target.put_file.assert_called_once_with(
            b"print('hi')", "/tmp/horus/task-2/.horus_python_runtime.py"
        )
        command = target.run_command.call_args[0][0]
        assert "uv venv" in command
        assert ".horus_python_environment/bin/python" in command
        assert ".horus_python_runtime.py" in command

    @pytest.mark.asyncio
    async def test_execute_conda_uploads_environment_file(
        self, horus_context: HorusContext, tmp_path: Path
    ) -> None:
        """A conda environment_file is shipped to the target before use."""
        del horus_context
        env_yaml = tmp_path / "environment.yaml"
        env_yaml.write_text("name: demo\n")
        executor = CondaPythonEnvironmentExecutor(
            environment_file=str(env_yaml)
        )
        task = _make_command_task(executor)
        target = _make_mock_target()

        with patch.object(task, "target", target):
            await executor._execute(task)

        remote_path = "/tmp/horus/task-1/.horus_conda_environment.yaml"
        target.put_file.assert_awaited_once_with(env_yaml, remote_path)
        command = target.run_command.call_args[0][0]
        assert f"conda env create -f {remote_path}" in command

    @pytest.mark.asyncio
    async def test_execute_conda_missing_environment_file_raises(
        self, horus_context: HorusContext, tmp_path: Path
    ) -> None:
        """A missing environment_file surfaces a task error."""
        del horus_context
        missing = tmp_path / "does-not-exist.yaml"
        executor = CondaPythonEnvironmentExecutor(
            environment_file=str(missing)
        )
        task = _make_command_task(executor)
        target = _make_mock_target()
        target.put_file = AsyncMock(side_effect=FileNotFoundError)

        with patch.object(task, "target", target):
            with pytest.raises(TaskExecutionError, match="environment_file"):
                await executor._execute(task)

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_raises(
        self, horus_context: HorusContext
    ) -> None:
        """A non-zero environment command exit code raises a task error."""
        del horus_context
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        target = _make_mock_target(
            _make_mock_proc(returncode=_NONZERO_CODE, stderr=b"failed")
        )

        with patch.object(task, "target", target):
            with pytest.raises(TaskExecutionError, match=str(_NONZERO_CODE)):
                await executor._execute(task)

    @pytest.mark.asyncio
    async def test_execute_cancellation_kills_process(
        self, horus_context: HorusContext
    ) -> None:
        """Cancellation kills the target process before propagating."""
        del horus_context
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        proc = _make_mock_proc()
        proc.kill = MagicMock()

        # The executor cancels while iterating the stream, so raise there.
        async def _cancelled_stream() -> object:
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

        proc.stream = MagicMock(return_value=_cancelled_stream())
        target = _make_mock_target(proc)

        with patch.object(task, "target", target):
            with pytest.raises(asyncio.CancelledError):
                await executor._execute(task)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


@pytest.mark.unit
class TestCondaEnvironmentFileResolution:
    """
    ``environment_file`` either names a file next to the workflow, or an input
    artifact already staged on the target.
    """

    async def test_template_resolves_to_artifact_and_skips_upload(
        self, tmp_path: Path
    ) -> None:
        """
        With ``environment_file: ${id}`` the file is whatever the transfer
        layer put on the target, so nothing is uploaded from this machine.
        """
        staged = tmp_path / "conda_env.yaml"
        staged.write_text("dependencies: [python=3.12]\n")

        executor = CondaPythonEnvironmentExecutor(
            environment_file="${conda_env}"
        )
        task = HorusTask(
            id="task-1",
            name="task_1",
            executor=executor,
            runtime=CommandRuntime(command="python --version"),
            inputs=[FileArtifact(id="conda_env", path=staged)],
        )
        target = AsyncMock()
        target.path_on_target = MagicMock(return_value=str(staged))

        with patch.object(task, "target", target):
            await executor._stage_environment(task)
            remote = executor._remote_environment_file(task)

        target.put_file.assert_not_awaited()
        assert remote == str(staged)
