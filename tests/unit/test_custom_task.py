# Copyright (C) 2026 YOUR_ORGANIZATION_NAME
# Licensed under the MIT License. See LICENSE for details.
"""
Unit tests for CustomTask.
"""

import pytest
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError

from my_plugin.task.custom_task import CustomTask


@pytest.mark.unit
class TestCustomTaskRegistration:
    """
    Verify that CustomTask registers correctly in the AutoRegistry.
    """

    def test_registered_under_kind(self) -> None:
        """
        Sample tests.
        """
        assert "custom_task" in BaseTask.registry
        assert BaseTask.registry["custom_task"] is CustomTask

    def test_kind_field(self) -> None:
        """
        Verify that the `kind` field of CustomTask is correctly set.
        """
        assert (
            CustomTask(
                id="test_task",
                name="test_task",
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo 'Hello, World!'"),
                target=LocalTarget(),
            ).kind
            == "custom_task"
        )


@pytest.mark.unit
class TestCustomTaskRun:
    """Verify CustomTask._run() behaviour."""

    @pytest.mark.asyncio
    async def test_run_raises_not_implemented(
        self, horus_context: HorusContext
    ) -> None:
        """
        The template task raises ``TaskExecutionError`` until the developer
        fills in the implementation. Replace this test with real assertions
        once ``_run`` is implemented.
        """
        del horus_context  # Unused in this template test

        # TODO: replace with a real executor/runtime once implemented
        with pytest.raises(TaskExecutionError):
            task = CustomTask(
                id="test_custom_task",
                name="test_custom_task",
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo 'Hello, World!'"),
                target=LocalTarget(),
            )
            await task._run()


@pytest.mark.unit
class TestCustomTaskReset:
    """Verify CustomTask._reset() behaviour."""

    def test_reset_raises_not_implemented(self) -> None:
        """
        The template task raises NotImplementedError until the developer fills
        in the implementation. Replace this test with real assertions once
        ``_reset`` is implemented.
        """
        with pytest.raises(NotImplementedError):
            task = CustomTask(
                id="test_custom_task",
                name="test_custom_task",
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo 'Hello, World!'"),
                target=LocalTarget(),
            )
            task._reset()


@pytest.mark.unit
class TestCustomTaskIsComplete:
    """Verify CustomTask.is_complete() behaviour."""

    def test_is_complete_raises_not_implemented(self) -> None:
        """
        The template task raises NotImplementedError until the developer fills
        in the implementation. Replace this test with real assertions once
        ``is_complete`` is implemented.
        """
        with pytest.raises(NotImplementedError):
            task = CustomTask(
                id="test_custom_task",
                name="test_custom_task",
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo 'Hello, World!'"),
                target=LocalTarget(),
            )
            task.is_complete()
