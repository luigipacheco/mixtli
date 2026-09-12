# SPDX-License-Identifier: GPL-3.0-or-later
"""
Filling in menu sockets that older files predate.

When a socket is added to a node group's interface, Blender gives every
existing instance of that group the interface default - except for menu
sockets, which stay unset. An unset menu makes Menu Switch output nothing, so
a file saved before MX 10 grew its Color Mode socket would render that group's
points black rather than picking up the default.

Nothing inside a node tree can test whether a menu is unset, so the repair
happens here: once for the file that is already open, and again on every load.
Each default below is the behaviour the socket replaced, so repairing a file
never changes how it looked.
"""

import bpy
from bpy.app.handlers import persistent

# group name prefix -> {menu socket name: value matching the old behaviour}
MENU_DEFAULTS = {
    "MX 10 |": {"Color Mode": "Mix"},
}


def _defaults_for(tree):
    if tree is None:
        return None
    for prefix, defaults in MENU_DEFAULTS.items():
        if tree.name.startswith(prefix):
            return defaults
    return None


def _fix_node(node, defaults):
    fixed = 0
    for name, value in defaults.items():
        socket = node.inputs.get(name)
        if socket is None or socket.type != 'MENU' or socket.is_linked:
            continue
        if socket.default_value:            # already a real choice, leave it
            continue
        try:
            socket.default_value = value
            fixed += 1
        except Exception:
            pass
    return fixed


def _fix_modifier(mod, defaults):
    """A group dropped straight onto an object keeps its values on the modifier."""
    tree = mod.node_group
    fixed = 0
    for item in tree.interface.items_tree:
        if item.item_type != 'SOCKET' or item.in_out != 'INPUT':
            continue
        value = defaults.get(item.name)
        if value is None or item.socket_type != 'NodeSocketMenu':
            continue
        try:
            inputs = mod.properties.inputs           # 5.x; 4.x has no menu inputs
            current = inputs[item.identifier]
            if isinstance(current, str) and current:
                continue
            inputs[item.identifier] = value
            fixed += 1
        except Exception:
            pass
    return fixed


def repair():
    """Set every unset Mixtli menu socket to the value it used to behave as."""
    if not hasattr(bpy.data, "node_groups"):
        # bpy.data is a _RestrictData proxy while add-ons register at startup.
        # Nothing to repair yet either way: the file has not been read.
        return 0
    fixed = 0
    for group in bpy.data.node_groups:
        if group.library:                    # linked data is the library's to fix
            continue
        for node in group.nodes:
            defaults = _defaults_for(getattr(node, "node_tree", None))
            if defaults:
                fixed += _fix_node(node, defaults)
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type != 'NODES':
                continue
            defaults = _defaults_for(getattr(mod, "node_group", None))
            if defaults:
                fixed += _fix_modifier(mod, defaults)
    if fixed:
        print("[mixtli] filled in %d menu socket(s) from an older file" % fixed)
    return fixed


@persistent
def _on_load(_file):
    repair()


def _deferred_repair():
    """Repair the file that was already open when the add-on was enabled."""
    try:
        repair()
    except Exception as e:                       # never take registration down
        print("[mixtli] menu socket repair skipped: %s" % e)
    return None                                  # run once, do not reschedule


def register():
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    # Enabling at startup happens before the blend file is read and while
    # bpy.data is restricted, so the one-off repair waits for the first tick
    # of the event loop. Background mode has no loop; load_post still covers it.
    try:
        if not bpy.app.timers.is_registered(_deferred_repair):
            bpy.app.timers.register(_deferred_repair, first_interval=0.0)
    except Exception as e:
        print("[mixtli] could not schedule menu socket repair: %s" % e)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    try:
        if bpy.app.timers.is_registered(_deferred_repair):
            bpy.app.timers.unregister(_deferred_repair)
    except Exception:
        pass
