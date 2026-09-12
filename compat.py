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


def register():
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    repair()


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
