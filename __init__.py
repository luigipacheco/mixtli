# SPDX-License-Identifier: GPL-3.0-or-later
"""
Mixtli - flatten materials to colour attributes and build point clouds.

Metadata lives in blender_manifest.toml; extensions must not declare bl_info.
"""

from . import assets, compat, gis, ui, view

_MODULES = (ui, gis, view, assets, compat)


def register():
    # If a later module raises, the earlier ones stay registered and the next
    # attempt trips over "already registered as a subclass". Undo instead, so a
    # failed enable leaves nothing behind and can be retried cleanly.
    started = []
    try:
        for mod in _MODULES:
            mod.register()
            started.append(mod)
    except Exception:
        for mod in reversed(started):
            try:
                mod.unregister()
            except Exception:
                pass
        raise


def unregister():
    for mod in reversed(_MODULES):
        try:
            mod.unregister()
        except Exception:
            pass
