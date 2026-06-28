# Copyright (C) 2026 YOUR_ORGANIZATION_NAME
# Licensed under the MIT License. See LICENSE for details.
"""
Example custom task for my_plugin.

This module is referenced by the ``horus.task`` entry point declared in
``pyproject.toml``. Importing it is enough for the class to self-register
inside the AutoRegistry during ``HorusContext.boot()``.

To add a new task entry point, declare it in ``pyproject.toml`` and create the
corresponding module here:

    [project.entry-points."horus.task"]
    my_custom_task = "my_plugin.task.custom_task"
"""

from typing import ClassVar

from horus_runtime.context import HorusContext
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError

from my_plugin.i18n import tr as _


class CustomTask(BaseTask):
    """
    A custom task that extends the Horus BaseTask.

    Replace this implementation with your own task logic. The ``kind`` field
    value must be unique across all registered tasks; it is the discriminator
    key used by the AutoRegistry to dispatch incoming task payloads.
    """

    registry_key: ClassVar[str] = "kind"
    kind: str = "custom_task"

    async def _run(self) -> None:
        """
        Core execution logic for the task.

        Raise ``TaskExecutionError`` on failure so the runtime can handle
        retries and status transitions correctly.
        """
        ctx = HorusContext.get_context()

        del ctx  # Unused in this template method

        # Emit a custom event or use the built-in event bus directly:
        # ctx.bus.emit(MyTaskStartedEvent(...))

        # Replace with your actual business logic:
        raise TaskExecutionError(
            _("CustomTask._run() is not implemented yet.")
        )

    def _reset(self) -> None:
        """
        Reset any internal state to allow for a retry.
        """
        raise NotImplementedError(
            "CustomTask._reset() is not implemented yet."
        )

    def is_complete(self) -> bool:
        """
        Return True if the task has completed successfully, False otherwise.
        """
        raise NotImplementedError(
            "CustomTask.is_complete() is not implemented yet."
        )
