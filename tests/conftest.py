# Copyright (C) 2026 YOUR_ORGANIZATION_NAME
# Licensed under the MIT License. See LICENSE for details.
"""
Shared pytest configuration and fixtures for my_plugin tests.
"""

from collections.abc import Generator

import pytest
from horus_runtime.context import HorusContext, _runtime_ctx
from horus_runtime.registry.auto_registry import AutoRegistry


def pytest_configure(config: pytest.Config) -> None:
    """
    Register custom markers.
    """
    # Register custom markers for test categorization.
    # This is optional but can be useful for filtering tests.
    config.addinivalue_line("markers", "unit: mark test as a unit test")
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )


@pytest.fixture(scope="session", autouse=True)
def init_registry() -> None:
    """
    Load all registered plugins (including my_plugin) once per session.

    ``AutoRegistry.init_registry()`` discovers every installed package that
    declares a ``horus.*`` entry point and imports its module, triggering
    class registration. This must run before any Pydantic model that contains
    a registry field is instantiated.
    """
    AutoRegistry.init_registry()


@pytest.fixture
def horus_context() -> Generator[HorusContext]:
    """
    Provide a fresh ``HorusContext`` for each test and reset the context
    variable afterwards so tests do not leak state into each other.
    """
    ctx = HorusContext()
    ctx.bus.start()
    token = _runtime_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _runtime_ctx.reset(token)
