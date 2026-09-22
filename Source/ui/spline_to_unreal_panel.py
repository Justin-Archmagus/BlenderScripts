"""
spline_to_unreal_panel.py

Panel front-end for spline_export_core. Adds a "Spline to Unreal" panel to the
3D Viewport sidebar (N key) under a "Pinball" tab.

All logic lives in spline_export_core. This file owns UI state and UI feedback --
the script front-end owns the text report instead, and neither knows about the
other.

HOW TO RUN
    Blender Text Editor -> Run Script to register. Tick the text datablock's
    "Register" checkbox to have it re-register on file load.

    THEN: hover the 3D Viewport, press N to open the sidebar, and choose the
    "Pinball" tab down the right edge. Registering does not open the sidebar, so
    with it collapsed a correctly registered panel is completely invisible.

    Re-running is safe: __main__ unregisters first, so iterating does not raise
    duplicate-class errors.

WHY THAT CONTEXT
    Registers UI classes into a running Blender. Meaningless under
    `blender --background`, which has no window to draw into.

NO DRY RUN FLAG HERE, BY DESIGN
    The script uses DRY_RUN because a script has one entry point. A checkbox
    labelled "don't actually do it" is worse in a UI: it is persisted state,
    invisible at the moment you click, and it decays the first time someone
    unticks it and saves. Two verbs instead -- Preview and Convert & Export --
    with Export disabled until a matching Preview exists.

DO NOT ADD `from __future__ import annotations` TO THIS FILE
    Blender builds RNA properties by inspecting __annotations__. PEP 563 turns
    every annotation into a string, so `name: StringProperty(...)` would register
    as the string "StringProperty(...)" and the properties would silently vanish.
    Quoted annotations on individual functions are fine; the module-wide switch
    is not.

STATUS
    First draft, never run. Registration in particular is unverified.
"""

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Collection, Context, Object, Operator, Panel, PropertyGroup

if TYPE_CHECKING:
    from spline_export_core import ExportPlan, ExportSettings

# The core lives in Source/lib, a sibling of this file's folder (Source/ui).
CORE_DIR_NAME = "lib"

# Last resort, used only when __file__ is not a real path -- which happens for a
# text datablock created inside Blender rather than opened from disk. This is the
# one machine-specific line in the file; everything else resolves relatively so
# the repo works wherever it is checked out.
CORE_DIR_FALLBACK = r"D:\dev\Blender\Scripts\BlenderScripts\Source\lib"

CORE_MODULE = "spline_export_core"

SCENE_PROP = "pinball_spline_export"

_core: ModuleType | None = None


# ------------------------------------------------------------------ bootstrap

def _load_core() -> ModuleType:
    """Import spline_export_core from this script's folder.

    Duplicated from spline_to_unreal_mesh.py on purpose: it is the code that
    makes importing possible, so it cannot itself be imported. Packaging both
    front-ends as an extension would replace this with a relative import and
    delete the duplication -- worth doing once the design settles.
    """
    import importlib
    import sys

    candidates: list[Path] = []
    try:
        here = Path(__file__).resolve().parent
    except NameError:
        pass
    else:
        candidates.append(here.parent / CORE_DIR_NAME)  # Source/ui -> Source/lib
        candidates.append(here)                         # flat layout, side by side
    candidates.append(Path(CORE_DIR_FALLBACK))

    for folder in candidates:
        if (folder / f"{CORE_MODULE}.py").is_file():
            if str(folder) not in sys.path:
                sys.path.append(str(folder))
            return importlib.reload(importlib.import_module(CORE_MODULE))

    raise RuntimeError(
        f"Could not find {CORE_MODULE}.py. Looked in: "
        f"{[str(c) for c in candidates]}. Fix CORE_DIR_FALLBACK."
    )


def _get_core() -> ModuleType:
    global _core
    if _core is None:
        _core = _load_core()
    return _core


def _active_curve(context: Context) -> Object | None:
    """Local copy of the core helper.

    poll() and draw() run on every redraw, so they must not touch the dynamic
    import. Two lines of duplication buys that.
    """
    obj = context.active_object
    return obj if obj is not None and obj.type == 'CURVE' else None


def _poll_export_collection(self: PropertyGroup, collection: Collection) -> bool:
    """Restrict the collection dropdown to collections in this view layer.

    Blender applies this only when assigning from the UI -- a value set any other
    way goes unchecked -- so build_plan() validates the same condition again.

    This runs when the dropdown is opened rather than every redraw, so reaching
    for the dynamically imported core is acceptable here. Failure is permissive:
    listing every collection beats breaking the widget.
    """
    try:
        names = _get_core().view_layer_collection_names(bpy.context.view_layer)
    except Exception:
        return True
    return collection.name in names


# ------------------------------------------------------------------- ui state

class PINBALL_PG_spline_warning(PropertyGroup):
    """One warning from the last preview.

    A CollectionProperty cannot hold a dataclass, so core's PlanWarning is
    flattened into RNA here. The blocking flag survives, which is what the panel
    needs to pick an icon and refuse to export.
    """

    message: StringProperty(name="Message")
    blocking: BoolProperty(name="Blocking", default=False)


class PINBALL_PG_spline_export(PropertyGroup):
    """Panel inputs plus a cached projection of the last preview.

    The cache holds plain values, never the ExportPlan itself. An ExportPlan
    references a live bpy Object, and touching one after the object is deleted
    raises ReferenceError rather than returning None -- so caching the plan
    across operator invocations would be a crash waiting for a delete key.
    """

    # --- inputs, mirroring core.ExportSettings
    export_dir: StringProperty(
        name="Folder",
        subtype='DIR_PATH',
        default=r"D:\dev\blender_models",
    )
    # A datablock pointer rather than a name: it gives the native selector with
    # search, it survives renames, and poll keeps invalid choices out of the list.
    export_collection: PointerProperty(
        name="Collection",
        type=Collection,
        description="Collection the generated mesh is linked into",
        poll=_poll_export_collection,
    )
    name_override: StringProperty(
        name="Name",
        description="Blank derives the name from the source curve",
    )
    origin_center: EnumProperty(
        name="Origin",
        items=[
            ('MEDIAN', "Median", "Blender's Origin to Geometry default"),
            ('BOUNDS', "Bounds", "Bounding-box centre"),
        ],
        default='MEDIAN',
    )
    write_fbx: BoolProperty(
        name="Write FBX",
        description="Off converts the mesh but touches no files",
        default=True,
    )
    allow_overwrite: BoolProperty(
        name="Allow Overwrite",
        description="Replace the target file if it already exists",
        default=False,
    )

    # --- cached preview, for display only
    preview_valid: BoolProperty(default=False)
    preview_source: StringProperty()
    preview_mesh_name: StringProperty()
    preview_out_path: StringProperty()
    preview_verts: IntProperty()
    preview_polys: IntProperty()
    preview_blocked: BoolProperty(default=False)
    preview_warnings: CollectionProperty(type=PINBALL_PG_spline_warning)


def _props(context: Context) -> PINBALL_PG_spline_export:
    return getattr(context.scene, SCENE_PROP)


def _settings_from(
    core: ModuleType, props: PINBALL_PG_spline_export
) -> "ExportSettings":
    return core.ExportSettings(
        export_dir=props.export_dir,
        # Core is name-based, so the pointer is resolved here. Read fresh each
        # time, so a rename between Preview and Export is picked up.
        export_collection=(
            props.export_collection.name if props.export_collection else ""
        ),
        name_override=props.name_override,
        origin_center=props.origin_center,
        write_fbx=props.write_fbx,
        allow_overwrite=props.allow_overwrite,
    )


def _store_preview(props: PINBALL_PG_spline_export, plan: "ExportPlan") -> None:
    props.preview_warnings.clear()
    for warning in plan.warnings:
        item = props.preview_warnings.add()
        item.message = warning.message
        item.blocking = warning.blocking

    props.preview_source = plan.source.name
    props.preview_mesh_name = plan.mesh_name
    props.preview_out_path = str(plan.out_path) if plan.out_path else ""
    props.preview_verts = plan.verts
    props.preview_polys = plan.polys
    props.preview_blocked = bool(plan.blocking_warnings)
    props.preview_valid = True


def _is_stale(context: Context, props: PINBALL_PG_spline_export) -> bool:
    """Whether the cache still describes the active object. Name comparison only,
    so it is cheap enough for draw() and poll()."""
    obj = context.active_object
    return props.preview_source != (obj.name if obj is not None else "")


# ------------------------------------------------------------------ operators

class PINBALL_OT_spline_preview(Operator):
    bl_idname = "pinball.spline_preview"
    bl_label = "Preview"
    bl_description = "Inspect the active curve and report what an export would do"
    # No UNDO: this writes only the preview cache, which is not scene content
    # anyone would want on the undo stack.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: Context) -> bool:
        return context.mode == 'OBJECT' and _active_curve(context) is not None

    def execute(self, context: Context) -> set[str]:
        core = _get_core()
        props = _props(context)
        try:
            plan = core.build_plan(context, _settings_from(core, props))
        except core.PlanError as exc:
            props.preview_valid = False
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        _store_preview(props, plan)
        self.report({'INFO'}, f"{plan.verts} verts, {plan.polys} polys")
        return {'FINISHED'}


class PINBALL_OT_spline_export(Operator):
    bl_idname = "pinball.spline_export"
    bl_label = "Convert & Export"
    bl_description = "Convert the previewed curve to a mesh and export it"
    # UNDO covers the scene changes. It does NOT unwrite the FBX -- which is why
    # allow_overwrite exists as a separate deliberate opt-in.
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: Context) -> bool:
        props = _props(context)
        return (
            context.mode == 'OBJECT'
            and _active_curve(context) is not None
            and props.preview_valid
            and not props.preview_blocked
            and not _is_stale(context, props)
        )

    def execute(self, context: Context) -> set[str]:
        core = _get_core()
        props = _props(context)
        try:
            # Rebuilt rather than reused: the cache is a display projection, and
            # the scene may have changed since Preview ran.
            plan = core.build_plan(context, _settings_from(core, props))
            result = core.execute_plan(context, plan)
        except core.PlanError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # The scene now contains a new object, so the cache no longer describes it.
        props.preview_valid = False
        destination = result.fbx_path or "no file written"
        self.report(
            {'INFO'},
            f"{result.mesh_object_name}: {result.polys} polys -> {destination}",
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------- panel

class PINBALL_PT_spline_export(Panel):
    bl_label = "Spline to Unreal"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Pinball"

    def draw(self, context: Context) -> None:
        # Cheap reads only. build_plan() evaluates the depsgraph and must never
        # be called from here -- draw() runs on every redraw.
        layout = self.layout
        props = _props(context)
        curve = _active_curve(context)

        header = layout.box()
        if context.mode != 'OBJECT':
            header.label(text="Switch to Object Mode", icon='ERROR')
        elif curve is None:
            header.label(text="Select a curve", icon='ERROR')
        else:
            header.label(text=curve.name, icon='OUTLINER_OB_CURVE')

        col = layout.column()
        col.prop(props, "name_override")
        col.prop(props, "origin_center")
        col.prop(props, "export_collection")
        # An ID pointer cannot carry a default, so it starts empty. Say so rather
        # than letting Preview fail with an error the user has to go read.
        if props.export_collection is None:
            col.label(text="Pick a target collection", icon='ERROR')

        col = layout.column()
        col.prop(props, "write_fbx")
        sub = col.column()
        sub.enabled = props.write_fbx
        sub.prop(props, "export_dir")
        sub.prop(props, "allow_overwrite")

        layout.separator()
        layout.operator(PINBALL_OT_spline_preview.bl_idname, icon='VIEWZOOM')

        if props.preview_valid:
            if _is_stale(context, props):
                layout.label(text="Selection changed, preview again",
                             icon='FILE_REFRESH')
            else:
                box = layout.box()
                box.label(
                    text=f"{props.preview_verts} verts, {props.preview_polys} polys",
                    icon='CHECKMARK',
                )
                if props.preview_out_path:
                    box.label(text=Path(props.preview_out_path).name, icon='EXPORT')
                for warning in props.preview_warnings:
                    box.label(
                        text=warning.message,
                        icon='CANCEL' if warning.blocking else 'ERROR',
                    )

        # poll() greys this out on its own, so no manual enabled juggling here.
        layout.operator(PINBALL_OT_spline_export.bl_idname, icon='EXPORT')


# --------------------------------------------------------------- registration

# Order matters: PINBALL_PG_spline_warning must register before the group whose
# CollectionProperty points at it. The factory unregisters in reverse.
_CLASSES = (
    PINBALL_PG_spline_warning,
    PINBALL_PG_spline_export,
    PINBALL_OT_spline_preview,
    PINBALL_OT_spline_export,
    PINBALL_PT_spline_export,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(_CLASSES)


def register() -> None:
    _register_classes()
    setattr(
        bpy.types.Scene,
        SCENE_PROP,
        PointerProperty(type=PINBALL_PG_spline_export),
    )


def unregister() -> None:
    if hasattr(bpy.types.Scene, SCENE_PROP):
        delattr(bpy.types.Scene, SCENE_PROP)
    _unregister_classes()


if __name__ == "__main__":
    # Re-running from the Text Editor would otherwise raise on duplicate classes.
    try:
        unregister()
    except Exception:
        pass
    register()

    # Registering is otherwise completely silent, which makes success and failure
    # look identical -- especially with the sidebar collapsed, where a working
    # panel is invisible. Say so explicitly.
    print(
        f"{PINBALL_PT_spline_export.bl_label!r} registered "
        f"({len(_CLASSES)} classes). "
        f"3D Viewport -> N -> {PINBALL_PT_spline_export.bl_category!r} tab."
    )
