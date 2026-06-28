# Copyright (C) 2026 YOUR_ORGANIZATION_NAME
# Licensed under the MIT License. See LICENSE for details.
"""
Localization for my_plugin.

Import ``tr`` (aliased as ``_``) in any module that has user-visible strings::

    from my_plugin.i18n import tr as _

    _("Something happened.")
    _("%(n)s item processed", "%(n)s items processed", n=count)
"""

from pathlib import Path

from horus_runtime.i18n import make_translator

tr = make_translator("my_plugin", Path(__file__).parent / "locale")
