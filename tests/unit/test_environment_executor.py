"""Unit tests for Python environment executors."""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.task.exceptions import (
    CommandFailedError,
    TaskExecutionError,
)

from horus_environments.executor.environment import (
    _ENV_PROBE,
    CondaPythonEnvironmentExecutor,
    PythonEnvironmentExecutor,
    UvPythonEnvironmentExecutor,
    VirtualenvPythonEnvironmentExecutor,
)

_NONZERO_CODE = 7
_DIGEST_LEN = 32


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


_PROBE = _ENV_PROBE


def _probe_output(env_root: str = "", *, lock: bool = True) -> bytes:
    """Return what the env probe prints on a target."""
    out = f"__horus_env_root__{env_root}\n"
    if lock:
        out += "__horus_env_lock__\n"
    return out.encode()


def _make_mock_target(
    proc: AsyncMock | None = None,
    env_root: str = "",
    probe: AsyncMock | None = None,
) -> MagicMock:
    """Return a mock target whose channel methods are async."""
    target = MagicMock()
    target.resolved_working_directory = "/tmp/horus"
    target.mkdir = AsyncMock()
    target.put_file = AsyncMock()
    task_proc = proc or _make_mock_proc()

    def _run_command(command: str, **_kwargs: object) -> AsyncMock:
        if command == _PROBE:
            return probe or _make_mock_proc(stdout=_probe_output(env_root))
        return task_proc

    target.run_command = AsyncMock(side_effect=_run_command)
    return target


def _task_command(target: MagicMock) -> str:
    """Return the task command sent to the mock target (not the probe)."""
    commands = [c.args[0] for c in target.run_command.call_args_list]
    return str(next(c for c in reversed(commands) if c != _PROBE))


def _run_in_shell(command: str, **_kwargs: object) -> AsyncMock:
    """Run ``command`` locally and return its result as a channel process."""
    result = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, check=False
    )
    return _make_mock_proc(result.returncode, result.stdout, result.stderr)


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

    def test_conda_runtime_commands_use_conda_run_for_hooks(self) -> None:
        """Envs with activate.d hooks still go through `conda run`."""
        executor = CondaPythonEnvironmentExecutor(conda="conda")
        task = _make_command_task(executor)

        command = executor._run_command(task, "echo hi")
        python = executor._run_python_script_command(task, "/tmp/run.py")

        assert "/etc/conda/activate.d" in command
        assert "conda run --no-capture-output -p" in command
        assert "/bin/sh -c 'echo hi'" in command
        assert "conda run --no-capture-output -p" in python
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
            assert f"{exe} run -p" in command
            assert f"{exe} run -p" in python


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
        commands = [c.args[0] for c in target.run_command.call_args_list]
        assert commands[0] == _PROBE
        assert len(commands[1:]) == 1
        command = _task_command(target)
        assert "python -m venv" in command
        assert "-m pip install rich" in command
        assert "/bin/sh -c" in command
        kwargs = target.run_command.call_args.kwargs
        assert kwargs["cwd"] == expected_working_dir
        assert kwargs["env"]["EXTRA"] == "1"

    def test_concurrent_setups_build_the_environment_once(
        self, tmp_path: Path
    ) -> None:
        """Clones starting together build a shared environment once."""
        env_dir = tmp_path / "envs" / "shared"
        executor = VirtualenvPythonEnvironmentExecutor(
            environment_dir=str(env_dir)
        )
        task = _make_command_task(executor)
        log = tmp_path / "log"
        # Stand-in for the real build: slow, and only when nothing is there.
        with patch.object(
            VirtualenvPythonEnvironmentExecutor,
            "_setup_commands",
            return_value=[
                f"if [ ! -e {env_dir}/built ]; then echo build >> {log}"
                f" && sleep 0.3 && mkdir -p {env_dir}"
                f" && touch {env_dir}/built;"
                f" else echo reuse >> {log}; fi"
            ],
        ):
            command = executor._locked_setup_command(task)
        procs = [
            subprocess.Popen(["/bin/sh", "-c", command]) for _ in range(4)
        ]
        assert [proc.wait() for proc in procs] == [0] * 4
        assert sorted(log.read_text().split()) == ["build"] + ["reuse"] * 3

    @pytest.mark.parametrize("hooks", [None, "activate.d", "variables"])
    def test_conda_runs_directly_unless_env_has_activation_hooks(
        self, tmp_path: Path, hooks: str | None
    ) -> None:
        """Only envs with activate.d hooks or variables pay for `run`."""
        env_dir = tmp_path / "env"
        (env_dir / "bin").mkdir(parents=True)
        (env_dir / "bin" / "python").symlink_to(sys.executable)
        if hooks == "activate.d":
            activate_d = env_dir / "etc" / "conda" / "activate.d"
            activate_d.mkdir(parents=True)
            (activate_d / "hook.sh").write_text("")
        if hooks == "variables":
            (env_dir / "conda-meta").mkdir()
            (env_dir / "conda-meta" / "state").write_text(
                '{"env_vars": {"A": "1"}}'
            )
        # Fake micromamba: records the call, then activates and runs it.
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()
        calls = tmp_path / "calls"
        fake = fake_bin / "micromamba"
        fake.write_text(
            f'#!/bin/sh\necho "$@" >> {calls}\n'
            'export PATH="$3/bin:$PATH" CONDA_PREFIX="$3"\n'
            'shift 3\nexec "$@"\n'
        )
        fake.chmod(0o755)
        script = tmp_path / "run.py"
        script.write_text(
            "import os, sys\nprint(sys.executable, os.environ['CONDA_PREFIX'])"
        )
        executor = CondaPythonEnvironmentExecutor(
            conda="micromamba", environment_dir=str(env_dir)
        )
        task = _make_command_task(executor)

        result = subprocess.run(
            [
                "/bin/sh",
                "-c",
                executor._run_python_script_command(task, str(script)),
            ],
            env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.split() == [
            "__horus_environment_ready__",
            str(env_dir / "bin" / "python"),
            str(env_dir),
        ]
        assert calls.exists() is (hooks is not None)

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
        command = _task_command(target)
        assert "uv venv" in command
        assert "/tmp/horus/.horus_envs/venv-" in command
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
    async def test_command_exit_raises_command_failed_error(
        self, horus_context: HorusContext
    ) -> None:
        """A non-zero exit raises CommandFailedError with the exit code."""
        del horus_context
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        target = _make_mock_target(
            _make_mock_proc(
                returncode=_NONZERO_CODE,
                stdout=b"__horus_environment_ready__\n",
                stderr=b"failed",
            )
        )

        with patch.object(task, "target", target):
            with pytest.raises(
                CommandFailedError, match=str(_NONZERO_CODE)
            ) as exc_info:
                await executor._execute(task)
        assert exc_info.value.exit_code == _NONZERO_CODE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broken", ["build", "activate"])
    async def test_setup_failure_is_not_droppable(
        self, horus_context: HorusContext, tmp_path: Path, broken: str
    ) -> None:
        """A failed env build or activation is not the command's exit."""
        del horus_context
        env_dir = tmp_path / "env"
        if broken == "activate":
            # The interpreter exists, so setup reuses it; bin/activate is gone.
            (env_dir / "bin").mkdir(parents=True)
            (env_dir / "bin" / "python").symlink_to(sys.executable)
        executor = VirtualenvPythonEnvironmentExecutor(
            python="false", environment_dir=str(env_dir)
        )
        task = _make_command_task(executor, "exit 1")
        target = _make_mock_target()
        target.run_command = AsyncMock(side_effect=_run_in_shell)

        with patch.object(task, "target", target):
            with pytest.raises(TaskExecutionError, match="setup") as exc:
                await executor._execute(task)
        assert not isinstance(exc.value, CommandFailedError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("runtime", ["command", "python"])
    @pytest.mark.parametrize("backend", ["virtualenv", "conda", "partial"])
    async def test_command_exit_after_real_setup_is_droppable(
        self,
        horus_context: HorusContext,
        tmp_path: Path,
        runtime: str,
        backend: str,
    ) -> None:
        """Only the task's own command exit raises CommandFailedError."""
        del horus_context
        env_dir = tmp_path / "env"
        (env_dir / "bin").mkdir(parents=True)
        (env_dir / "bin" / "python").symlink_to(sys.executable)
        (env_dir / "bin" / "activate").write_text("")
        executor: PythonEnvironmentExecutor = (
            CondaPythonEnvironmentExecutor(environment_dir=str(env_dir))
            if backend == "conda"
            else VirtualenvPythonEnvironmentExecutor(
                environment_dir=str(env_dir)
            )
        )
        task = HorusTask(
            id="task-1",
            name="task_1",
            executor=executor,
            runtime=CommandRuntime(command=f"exit {_NONZERO_CODE}")
            if runtime == "command"
            else PythonCodeStringRuntime(
                code=f"raise SystemExit({_NONZERO_CODE})"
            ),
        )
        target = _make_mock_target()
        target.resolved_working_directory = str(tmp_path)
        target.mkdir = AsyncMock(
            side_effect=lambda path: Path(path).mkdir(parents=True)
        )
        target.put_file = AsyncMock(
            side_effect=lambda code, path: Path(path).write_bytes(code)
        )
        target.run_command = AsyncMock(side_effect=_run_in_shell)

        with (
            patch.object(task, "target", target),
            # A setup step that leaves "progress" with no trailing newline.
            patch.object(
                VirtualenvPythonEnvironmentExecutor,
                "_pip_install_command",
                return_value="printf progress"
                if backend == "partial"
                else None,
            ),
            patch(
                "horus_environments.executor.environment.horus_logger"
            ) as logger,
            pytest.raises(CommandFailedError) as exc,
        ):
            await executor._execute(task)
        assert exc.value.exit_code == _NONZERO_CODE
        logged = [c.args[0] for c in logger.log.info.call_args_list]
        assert "__horus_environment_ready__" not in str(logged)
        assert "" not in logged
        assert ("progress" in logged) is (backend == "partial")

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

    def test_relative_path_anchors_to_workflow_dir(
        self, tmp_path: Path
    ) -> None:
        """
        A relative path resolves against the workflow directory, not the
        process CWD -- otherwise the workflow only runs when Horus happens to
        be invoked from that directory.
        """
        executor = CondaPythonEnvironmentExecutor(
            environment_file="conda_env.yaml"
        )

        executor.anchor_local_paths(tmp_path)

        assert executor.environment_file == str(
            (tmp_path / "conda_env.yaml").resolve()
        )

    def test_absolute_path_is_left_alone(self, tmp_path: Path) -> None:
        """An absolute path already names its file; anchoring must not move."""
        absolute = str(tmp_path / "elsewhere" / "conda_env.yaml")
        executor = CondaPythonEnvironmentExecutor(environment_file=absolute)

        executor.anchor_local_paths(tmp_path / "workflow")

        assert executor.environment_file == absolute

    def test_template_is_not_anchored(self, tmp_path: Path) -> None:
        """A template names an artifact, so it is not a path to resolve."""
        executor = CondaPythonEnvironmentExecutor(
            environment_file="${conda_env}"
        )

        executor.anchor_local_paths(tmp_path)

        assert executor.environment_file == "${conda_env}"

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

    def test_undeclared_artifact_id_raises(self) -> None:
        """
        A placeholder that names no input artifact must fail loudly here,
        instead of reaching conda as a literal '${id}' filename.
        """
        executor = CondaPythonEnvironmentExecutor(
            environment_file="${missing_id}"
        )
        task = HorusTask(
            id="task-1",
            name="task_1",
            executor=executor,
            runtime=CommandRuntime(command="python --version"),
            inputs=[],
        )

        with pytest.raises(TaskExecutionError, match="missing_id"):
            executor._remote_environment_file(task)


async def _shared_path(
    executor: PythonEnvironmentExecutor,
    env_root: str = "/envs",
    task: HorusTask | None = None,
    probe: AsyncMock | None = None,
) -> str:
    """Resolve and return the shared environment path for a task."""
    task = task or _make_command_task(executor)
    target = _make_mock_target(env_root=env_root, probe=probe)
    with patch.object(task, "target", target):
        await executor._resolve_shared_environment(task)
    return executor._environment_path(task)


@pytest.mark.unit
class TestSharedEnvironment:
    """The default environment_dir is shared and keyed by the env spec."""

    async def test_same_spec_same_dir(self) -> None:
        """Two tasks with the same spec resolve to one shared env."""
        spec: dict[str, Any] = {
            "python_version": "3.12",
            "conda_requirements": ["vina"],
        }
        first = await _shared_path(CondaPythonEnvironmentExecutor(**spec))
        second = await _shared_path(CondaPythonEnvironmentExecutor(**spec))

        assert first == second
        assert first.startswith("/envs/conda-")

    @pytest.mark.parametrize(
        "change",
        [
            {"python_version": "3.11"},
            {"conda_requirements": ["vina", "rdkit"]},
            {"channels": ["bioconda"]},
            {"requirements": ["pandas"]},
            {"conda": "micromamba"},
        ],
    )
    async def test_different_spec_different_dir(
        self, change: dict[str, Any]
    ) -> None:
        """Any spec change gives a new env instead of reusing the old one."""
        spec: dict[str, Any] = {
            "python_version": "3.12",
            "conda_requirements": ["vina"],
        }
        base = await _shared_path(CondaPythonEnvironmentExecutor(**spec))
        changed = await _shared_path(
            CondaPythonEnvironmentExecutor(**{**spec, **change})
        )

        assert base != changed

    async def test_spec_hash_is_stable(self) -> None:
        """
        The hash of a fixed spec never changes: a change orphans every
        shared env already built on disk.
        """
        path = await _shared_path(
            VirtualenvPythonEnvironmentExecutor(requirements=["numpy==2.1.0"])
        )

        assert path == "/envs/venv-e161894a1261247a3d8801af704d5dc7"

    @pytest.mark.skipif(shutil.which("flock") is None, reason="no flock")
    def test_concurrent_setups_build_once(self, tmp_path: Path) -> None:
        """Two tasks set up the same shared env at once: one build."""
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        env_dir = tmp_path / "venv-test"
        python = env_dir / "bin" / "python"
        executor._shared_paths[task.id] = str(env_dir)
        log = tmp_path / "log"
        create = (
            f"sleep 0.5 && mkdir -p {env_dir}/bin && : > {python}"
            f" && chmod +x {python} && echo create >> {log}"
        )

        with patch.object(
            VirtualenvPythonEnvironmentExecutor,
            "_create_command",
            return_value=create,
        ):
            command = executor._locked_setup_command(task)
        procs = [
            subprocess.Popen(["/bin/sh", "-c", command]) for _ in range(2)
        ]

        assert [proc.wait() for proc in procs] == [0, 0]
        assert log.read_text().split() == ["create"]
        subprocess.run(["chmod", "-R", "u+w", str(env_dir)], check=True)

    async def test_executor_env_is_hashed(self) -> None:
        """A different index or conda override gives a new env."""
        base = await _shared_path(UvPythonEnvironmentExecutor())
        index = await _shared_path(
            UvPythonEnvironmentExecutor(env={"PIP_INDEX_URL": "https://a"})
        )
        cuda = await _shared_path(
            UvPythonEnvironmentExecutor(env={"CONDA_OVERRIDE_CUDA": "12.4"})
        )

        assert base != index
        assert base != cuda
        assert index != cuda

    async def test_target_installer_vars_are_hashed(self) -> None:
        """Installer variables set on the target give a new env."""
        plain = _make_mock_proc(stdout=_probe_output("/envs"))
        mirror = _make_mock_proc(
            stdout=_probe_output("/envs")
            + b"__horus_env_var__PIP_INDEX_URL=https://mirror\n"
        )

        first = await _shared_path(
            VirtualenvPythonEnvironmentExecutor(), probe=plain
        )
        second = await _shared_path(
            VirtualenvPythonEnvironmentExecutor(), probe=mirror
        )

        assert first != second

    @pytest.mark.parametrize(
        "requirement",
        [".", "./pkg", "-e .", "-r requirements.txt", "pkg @ file:///x"],
    )
    async def test_local_requirements_are_not_shared(
        self, requirement: str
    ) -> None:
        """A requirement whose content the hash cannot see stays per task."""
        executor = UvPythonEnvironmentExecutor(requirements=[requirement])
        task = _make_command_task(executor)

        path = await _shared_path(executor, task=task)

        assert path == f"{task.working_dir}/.horus_python_environment"
        assert "--no-config" not in " ".join(executor._setup_commands(task))

    async def test_environment_file_with_local_refs_is_not_shared(
        self, tmp_path: Path
    ) -> None:
        """An env file that pulls in other files stays per task."""
        env_yaml = tmp_path / "environment.yaml"
        env_yaml.write_text(
            "dependencies:\n  - pip\n  - pip:\n    - -r requirements.txt\n"
        )
        executor = CondaPythonEnvironmentExecutor(
            environment_file=str(env_yaml)
        )
        task = _make_command_task(executor)

        path = await _shared_path(executor, task=task)

        assert path == f"{task.working_dir}/.horus_python_environment"

    async def test_shared_uv_ignores_config_files(self) -> None:
        """A shared uv build does not read uv config from the task dir."""
        executor = UvPythonEnvironmentExecutor(requirements=["numpy"])
        task = _make_command_task(executor)
        await _shared_path(executor, task=task)

        setup = " ".join(executor._setup_commands(task))

        assert "uv venv --no-config /envs/venv-" in setup
        assert "uv pip install --no-config --python /envs/venv-" in setup

    async def test_environment_file_content_is_hashed(
        self, tmp_path: Path
    ) -> None:
        """A one-line change in the env file gives a new env."""
        env_yaml = tmp_path / "environment.yaml"
        executor = CondaPythonEnvironmentExecutor(
            environment_file=str(env_yaml)
        )
        env_yaml.write_text("dependencies: [python=3.12]\n")
        before = await _shared_path(executor)
        env_yaml.write_text("dependencies: [python=3.13]\n")
        after = await _shared_path(executor)

        assert before != after

    async def test_artifact_environment_file_is_read_on_target(
        self, tmp_path: Path
    ) -> None:
        """An env file given as an artifact is read from the target."""
        staged = tmp_path / "conda_env.yaml"
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
        target = _make_mock_target(env_root="/envs")
        target.path_on_target = MagicMock(return_value=str(staged))
        target.get_file = AsyncMock(return_value=b"dependencies: []\n")

        with patch.object(task, "target", target):
            await executor._resolve_shared_environment(task)

        target.get_file.assert_awaited_once_with(str(staged))
        assert executor._environment_path(task).startswith("/envs/conda-")

    async def test_backends_do_not_share_a_dir(self) -> None:
        """Uv and stdlib venvs with the same requirements stay apart."""
        uv = await _shared_path(UvPythonEnvironmentExecutor())
        venv = await _shared_path(VirtualenvPythonEnvironmentExecutor())

        assert uv.startswith("/envs/venv-")
        assert uv != venv

    async def test_env_root_falls_back_to_target_working_dir(self) -> None:
        """Without HORUS_ENV_ROOT the env lives in the target working dir."""
        path = await _shared_path(CondaPythonEnvironmentExecutor(), "")

        assert path.startswith("/tmp/horus/.horus_envs/conda-")

    @pytest.mark.parametrize(
        ("stdout", "returncode", "expected"),
        [
            # rc noise before the sentinel is ignored
            (b"motd\n" + _probe_output("/envs"), 0, "/envs/venv-"),
            (_probe_output("/"), 0, "/venv-"),
            (_probe_output("envs"), 0, "/tmp/horus/.horus_envs/venv-"),
            (_probe_output("/envs", lock=False), 0, None),
            (_probe_output("/envs"), 1, None),
        ],
    )
    async def test_probe_output(
        self, stdout: bytes, returncode: int, expected: str | None
    ) -> None:
        """
        Only the sentinel line sets the root; a relative root falls back to
        the working dir; no flock or a failed probe keeps the env per task.
        """
        executor = VirtualenvPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        probe = _make_mock_proc(returncode, stdout)

        path = await _shared_path(executor, task=task, probe=probe)

        if expected is None:
            assert path == f"{task.working_dir}/.horus_python_environment"
        else:
            assert path.startswith(expected)
            assert len(path.rpartition("venv-")[2]) == _DIGEST_LEN

    async def test_explicit_environment_dir_is_not_shared(self) -> None:
        """An explicit environment_dir keeps per-task behaviour."""
        executor = CondaPythonEnvironmentExecutor(environment_dir="/opt/env")
        task = _make_command_task(executor)
        target = _make_mock_target(env_root="/envs")

        with patch.object(task, "target", target):
            await executor._resolve_shared_environment(task)

        target.run_command.assert_not_awaited()
        assert executor._environment_path(task) == "/opt/env"
        assert ".horus-ready" not in " ".join(executor._setup_commands(task))

    def test_ready_marker_gates_reuse_and_pip(self, tmp_path: Path) -> None:
        """
        Only a ready env with its interpreter is reused, without pip, and is
        read-only; a killed or failed build or a lost interpreter is wiped
        and rebuilt.
        """
        executor = VirtualenvPythonEnvironmentExecutor(requirements=["x"])
        task = _make_command_task(executor)
        env_dir = tmp_path / "venv-test"
        python = env_dir / "bin" / "python"
        executor._shared_paths[task.id] = str(env_dir)
        log = tmp_path / "log"
        pip = tmp_path / "pip_fails"

        def setup() -> int:
            command = executor._locked_setup_command(task)
            return subprocess.run(
                ["/bin/sh", "-c", command], check=False
            ).returncode

        def unlock() -> None:
            subprocess.run(["chmod", "-R", "u+w", str(env_dir)], check=True)

        create = (
            f"mkdir -p {env_dir}/bin && : > {python} && chmod +x {python}"
            f" && echo create >> {log}"
        )
        with (
            patch.object(
                VirtualenvPythonEnvironmentExecutor,
                "_create_command",
                return_value=create,
            ),
            patch.object(
                VirtualenvPythonEnvironmentExecutor,
                "_pip_install_command",
                return_value=f"echo pip >> {log} && [ ! -e {pip} ]",
            ),
        ):
            # Killed mid-build: the dir exists but was never marked ready.
            env_dir.mkdir()
            (env_dir / "partial").touch()
            assert setup() == 0
            assert not (env_dir / "partial").exists()
            assert (env_dir / ".horus-ready").exists()
            assert env_dir.stat().st_mode & 0o222 == 0
            assert setup() == 0
            assert log.read_text().split() == ["create", "pip"]

            # A failed pip step leaves no marker, so the next run rebuilds.
            log.unlink()
            unlock()
            (env_dir / ".horus-ready").unlink()
            pip.touch()
            assert setup() != 0
            assert not (env_dir / ".horus-ready").exists()
            pip.unlink()
            assert setup() == 0
            assert log.read_text().split() == ["create", "pip"] * 2

            # Marked ready but the interpreter is gone: rebuilt.
            log.unlink()
            unlock()
            python.unlink()
            subprocess.run(["chmod", "-R", "a-w", str(env_dir)], check=True)
            assert setup() == 0
            assert python.exists()
            assert log.read_text().split() == ["create", "pip"]
            unlock()

    async def test_recreate_builds_in_task_dir(self) -> None:
        """recreate: true never wipes the shared env, it builds per task."""
        executor = VirtualenvPythonEnvironmentExecutor(recreate=True)
        task = _make_command_task(executor)
        executor._shared_paths[task.id] = "/envs/venv-stale"
        target = _make_mock_target(env_root="/envs")

        with patch.object(task, "target", target):
            await executor._resolve_shared_environment(task)

        target.run_command.assert_not_awaited()
        path = executor._environment_path(task)
        assert path == f"{task.working_dir}/.horus_python_environment"
        setup = " ".join(executor._setup_commands(task))
        assert "/envs/" not in setup
        assert f"rm -rf {path}" in setup

    @pytest.mark.asyncio
    async def test_execute_builds_in_horus_env_root(
        self, horus_context: HorusContext
    ) -> None:
        """_execute probes HORUS_ENV_ROOT and builds the env there."""
        del horus_context
        executor = CondaPythonEnvironmentExecutor()
        task = _make_command_task(executor)
        target = _make_mock_target(env_root="/cache/org/conda-envs")

        with patch.object(task, "target", target):
            await executor._execute(task)

        probe = target.run_command.call_args_list[0]
        assert probe.args[0] == _PROBE
        assert probe.kwargs["detach"] is False
        command = _task_command(target)
        assert "flock /cache/org/conda-envs/conda-" in command
        assert "conda create -y -p /cache/org/conda-envs/conda-" in command
        assert ".horus_python_environment" not in command
