# SPDX-License-Identifier: GPL-3.0-or-later
"""
Registering the bundled node groups as an asset library.

Blender has no dedicated hook for an add-on to publish an asset library, so
the working pattern is to add a preferences entry on register and take it
away again on unregister. That entry is re-created every startup, so it is
idempotent and self-healing: if the extension is updated and lands in a new
directory, the path is corrected rather than duplicated.
"""

import os

import bpy

LIBRARY_NAME = "Mixtli"


def assets_dir():
    return os.path.join(os.path.dirname(__file__), "assets")


def _entry(libs):
    for lib in libs:
        if lib.name == LIBRARY_NAME:
            return lib
    return None


def register():
    directory = assets_dir()
    if not os.path.isdir(directory):
        return
    try:
        libs = bpy.context.preferences.filepaths.asset_libraries
    except AttributeError:
        return
    lib = _entry(libs)
    if lib is None:
        try:
            libs.new(name=LIBRARY_NAME, directory=directory)
        except Exception as e:
            print("[mixtli] could not register asset library: %s" % e)
            return
        lib = _entry(libs)
    if lib is None:
        return
    # an update moves the extension to a new versioned folder
    if os.path.normcase(os.path.normpath(lib.path)) != \
            os.path.normcase(os.path.normpath(directory)):
        lib.path = directory
    # Blender 5.x dropped APPEND_REUSE and made PACK the default; 4.2-4.5
    # only know the older names. The statically declared RNA enum still
    # advertises all four, so ask by trying rather than by inspecting.
    for method in ('APPEND_REUSE', 'PACK', 'APPEND'):
        try:
            lib.import_method = method
            break
        except Exception:
            continue


def unregister():
    try:
        libs = bpy.context.preferences.filepaths.asset_libraries
    except AttributeError:
        return
    lib = _entry(libs)
    if lib is not None:
        try:
            libs.remove(lib)
        except Exception:
            pass
