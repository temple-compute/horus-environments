# Copyright (C) 2026 YOUR_ORGANIZATION_NAME
# Licensed under the MIT License. See LICENSE for details.
"""
Localization for horus_environments.

Import ``tr`` (aliased as ``_``) in any module that has user-visible strings::

    from horus_environments.i18n import tr as _

    _("Something happened.")
    _("%(n)s item processed", "%(n)s items processed", n=count)
"""

from pathlib import Path

from horus_runtime.i18n import make_translator

tr = make_translator("horus_environments", Path(__file__).parent / "locale")
