# SPDX-License-Identifier: GPL-3.0-or-later
"""
Mixtli - flatten materials to colour attributes and build point clouds.

Metadata lives in blender_manifest.toml; extensions must not declare bl_info.
"""

from . import ui


def register():
    ui.register()


def unregister():
    ui.unregister()
