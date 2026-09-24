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
    Registered and exercised 2026-09-22. Both panels appear in the Pinball tab,
    and both operator pairs have been run: Preview -> Convert & Export, and
    Analyze -> Simplify.

    UV Layout panel (Analyze -> Lay Out UVs, over mesh_uv_core) added and
    tested 2026-09-24. Its UV Layout checkbox on the Spline to Unreal panel,
    sharing the same settings, added and tested the same day.
"""

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import (
    Collection,
    Context,
    Object,
    Operator,
    Panel,
    PropertyGroup,
    UILayout,
)

if TYPE_CHECKING:
    # Stub-only module from fake-bpy-module; it does not exist inside Blender,
    # so every use of it must stay in a quoted annotation.
    from bpy.stub_internal.rna_enums import OperatorReturnItems
    from mesh_simplify_core import SimplifyPlan, SimplifySettings
    from mesh_uv_core import UVPlan, UVSettings
    from spline_export_core import ExportPlan, ExportSettings

# The core lives in Source/lib, a sibling of this file's folder (Source/ui).
CORE_DIR_NAME = "lib"

# Last resort, used only when __file__ is not a real path -- which happens for a
# text datablock created inside Blender rather than opened from disk. This is the
# one machine-specific line in the file; everything else resolves relatively so
# the repo works wherever it is checked out.
CORE_DIR_FALLBACK = r"D:\dev\Blender\Scripts\BlenderScripts\Source\lib"

EXPORT_MODULE = "spline_export_core"
SIMPLIFY_MODULE = "mesh_simplify_core"
UV_MODULE = "mesh_uv_core"

SCENE_PROP = "pinball_spline_export"
SIMPLIFY_SCENE_PROP = "pinball_simplify"
UV_SCENE_PROP = "pinball_uv_layout"

_modules: dict[str, ModuleType] = {}

# Blender declares RNA properties as `name: StringProperty(...)`. That is a call
# expression, not a type, so Pyright reports it as an invalid type form. The
# syntax is Blender's and correct here; silence the check for this file only.
# pyright: reportInvalidTypeForm=false

# The 5.2 stubs declare the context of Operator.poll/execute and Panel.poll/draw
# as `Context | None`, while documenting it "(never None)". Overrides match the
# declared signature, since an override may not narrow a parameter, and assert
# the documented guarantee on their first line. Matching rather than suppressing
# keeps the override check alive for return types.

# ------------------------------------------------------------------ bootstrap

def _load_module(module_name: str) -> ModuleType:
    """Import a core module from the sibling lib folder.

    Duplicated from the cli front-ends on purpose: it is the code that makes
    importing possible, so it cannot itself be imported. Packaging the front-ends
    as an extension would replace this with a relative import and delete the
    duplication -- worth doing once the design settles.
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
        if (folder / f"{module_name}.py").is_file():
            if str(folder) not in sys.path:
                sys.path.append(str(folder))
            return importlib.reload(importlib.import_module(module_name))

    raise RuntimeError(
        f"Could not find {module_name}.py. Looked in: "
        f"{[str(c) for c in candidates]}. Fix CORE_DIR_FALLBACK."
    )


def _get_module(module_name: str) -> ModuleType:
    """Cached loader. The cache is module-level, so re-running this script from
    the Text Editor resets it and picks up core edits."""
    if module_name not in _modules:
        _modules[module_name] = _load_module(module_name)
    return _modules[module_name]


def _get_core() -> ModuleType:
    return _get_module(EXPORT_MODULE)


def _get_simplify() -> ModuleType:
    return _get_module(SIMPLIFY_MODULE)


def _get_uv() -> ModuleType:
    return _get_module(UV_MODULE)


def _active_of_type(context: Context, type_name: str) -> Object | None:
    """Local copy of the core helper.

    poll() and draw() run on every redraw, so they must not touch the dynamic
    import. Two lines of duplication buys that.
    """
    obj = context.active_object
    return obj if obj is not None and obj.type == type_name else None


def _active_curve(context: Context) -> Object | None:
    return _active_of_type(context, 'CURVE')


def _active_mesh(context: Context) -> Object | None:
    return _active_of_type(context, 'MESH')


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

class PINBALL_PG_warning(PropertyGroup):
    """One warning from the last preview, from either core.

    A CollectionProperty cannot hold a dataclass, so PlanWarning and
    SimplifyWarning are both flattened into RNA here. They have the same shape,
    and the blocking flag survives -- which is what the panels need to pick an
    icon and refuse to run.
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
    # Only the switch lives here. The parameters are the Simplify panel's, drawn
    # under this checkbox too, so there is one set of settings, not two to drift.
    simplify: BoolProperty(
        name="Simplify",
        description="Reduce geometry of converted mesh if possible",
        default=True,
    )
    # Switch only, like simplify: the parameters are the UV Layout panel's.
    uv_layout: BoolProperty(
        name="UV Layout",
        description="Lay out straight UV islands and mark seams on the converted "
                    "mesh, after Simplify. Blocks the export if not possible",
        default=True,
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
    # One display line; empty when simplify is off or will be skipped (the skip
    # reason is among the warnings).
    preview_simplify: StringProperty()
    # Same, for the UV layout; empty when off or impossible (see warnings).
    preview_uv_layout: StringProperty()
    preview_blocked: BoolProperty(default=False)
    preview_warnings: CollectionProperty(type=PINBALL_PG_warning)


def _props(context: Context) -> PINBALL_PG_spline_export:
    return getattr(context.scene, SCENE_PROP)


def _settings_from(
    core: ModuleType,
    props: PINBALL_PG_spline_export,
    simplify_props: "PINBALL_PG_simplify",
    uv_props: "PINBALL_PG_uv_layout",
) -> "ExportSettings":
    # The export core exposes the cores it depends on, so the settings classes
    # come from the same module objects the export will use.
    simplify = (
        _simplify_settings(core.simplify_core, simplify_props) if props.simplify else None
    )
    uv_layout = _uv_settings(core.uv_core, uv_props) if props.uv_layout else None
    return core.ExportSettings(
        export_dir=props.export_dir,
        # Core is name-based, so the pointer is resolved here. Read fresh each
        # time, so a rename between Preview and Export is picked up.
        export_collection=(
            props.export_collection.name if props.export_collection else ""
        ),
        name_override=props.name_override,
        origin_center=props.origin_center,
        simplify=simplify,
        uv_layout=uv_layout,
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
    analysis = plan.simplify
    props.preview_simplify = (
        f"Simplify: keep {list(analysis.keep_columns)}, "
        f"{plan.verts} -> {analysis.predicted_verts} verts"
        if analysis is not None else ""
    )
    uv = plan.uv_layout
    props.preview_uv_layout = (
        f"UV layout: {len(uv.islands)} islands, {uv.seams_to_mark} seams, "
        f"{sum(i.mirrored_faces for i in uv.islands)} mirrored"
        if uv is not None else ""
    )
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
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        return context.mode == 'OBJECT' and _active_curve(context) is not None

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _props(context)
        try:
            core = _get_core()
            settings = _settings_from(
                core, props, _simplify_props(context), _uv_props(context))
            plan = core.build_plan(context, settings)
        except Exception as exc:
            # Broad on purpose, as in the simplify operators: a PlanError, a failed
            # module load, or an OSError reading the preset all belong in the
            # status bar, not as a traceback in a hidden console.
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
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        props = _props(context)
        return (
            context.mode == 'OBJECT'
            and _active_curve(context) is not None
            and props.preview_valid
            and not props.preview_blocked
            and not _is_stale(context, props)
        )

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _props(context)
        try:
            core = _get_core()
            # Rebuilt rather than reused: the cache is a display projection, and
            # the scene may have changed since Preview ran.
            settings = _settings_from(
                core, props, _simplify_props(context), _uv_props(context))
            plan = core.build_plan(context, settings)
            result = core.execute_plan(context, plan)
        except Exception as exc:
            # execute_plan() rolls back its own partial mesh before raising, so
            # reporting is all that is left to do here.
            props.preview_valid = False
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        # The scene now contains a new object, so the cache no longer describes it.
        props.preview_valid = False
        destination = result.fbx_path or "no file written"
        simplified = (
            f" (simplified {result.simplify.verts_before} -> "
            f"{result.simplify.verts_after} verts)"
            if result.simplify is not None else ""
        )
        laid_out = (
            f", UVs laid out ({result.uv_layout.seams_marked} seams)"
            if result.uv_layout is not None else ""
        )
        self.report(
            {'INFO'},
            f"{result.mesh_object_name}: {result.polys} polys{simplified}{laid_out} "
            f"-> {destination}",
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------- panel

class PINBALL_PT_spline_export(Panel):
    bl_label = "Spline to Unreal"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Pinball"

    def draw(self, context: Context | None) -> None:
        assert context is not None
        # Cheap reads only. build_plan() evaluates the depsgraph and must never
        # be called from here -- draw() runs on every redraw.
        layout = self.layout
        # RNA types Panel.layout as optional because it only exists while the
        # panel is drawing. Inside draw() it always does; state that for the
        # type checker rather than guarding a case that cannot happen.
        assert layout is not None
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
        col.prop(props, "simplify")
        sub = col.column()
        sub.enabled = props.simplify
        _draw_simplify_settings(sub, _simplify_props(context))
        sub.label(text="Shared with the Simplify panel", icon='LINKED')
        col = layout.column()
        col.prop(props, "uv_layout")
        sub = col.column()
        sub.enabled = props.uv_layout
        _draw_uv_settings(sub, _uv_props(context))
        sub.label(text="Shared with the UV Layout panel", icon='LINKED')
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
                if props.preview_simplify:
                    box.label(text=props.preview_simplify, icon='MOD_DECIM')
                if props.preview_uv_layout:
                    box.label(text=props.preview_uv_layout, icon='UV')
                if props.preview_out_path:
                    box.label(text=Path(props.preview_out_path).name, icon='EXPORT')
                for warning in props.preview_warnings:
                    box.label(
                        text=warning.message,
                        icon='CANCEL' if warning.blocking else 'ERROR',
                    )

        # poll() greys this out on its own, so no manual enabled juggling here.
        layout.operator(PINBALL_OT_spline_export.bl_idname, icon='EXPORT')


# ------------------------------------------------------------- simplify state

class PINBALL_PG_simplify(PropertyGroup):
    """Simplify inputs plus a cached projection of the last analysis.

    Same discipline as the export group: plain values only, never the
    SimplifyPlan, which holds a live object reference.
    """

    angle_threshold_deg: FloatProperty(
        name="Angle Threshold",
        description="Profile turn below this many degrees counts as collinear, "
                    "so that column carries no shape and is redundant",
        default=1.0, min=0.0, max=180.0, soft_max=45.0, precision=3,
    )
    min_grid_confidence: FloatProperty(
        name="Min Grid Confidence",
        description="Refuse if the row-major grid model explains less than this "
                    "share of the mesh's edges",
        default=0.90, min=0.0, max=1.0,
    )
    use_column_override: BoolProperty(
        name="Override Columns",
        description="Ignore angle detection and keep exactly the columns listed",
        default=False,
    )
    column_override: StringProperty(
        name="Keep",
        description="Comma-separated 0-based column indices, e.g. 0,2,8,10",
        default="",
    )

    # --- cached analysis, for display only
    preview_valid: BoolProperty(default=False)
    preview_source: StringProperty()
    preview_width: IntProperty()
    preview_rows: IntProperty()
    preview_shells: IntProperty()
    preview_confidence: FloatProperty()
    preview_keep: StringProperty()
    preview_dissolve: StringProperty()
    preview_edges_hit: IntProperty()
    preview_verts_before: IntProperty()
    preview_verts_after: IntProperty()
    preview_blocked: BoolProperty(default=False)
    preview_warnings: CollectionProperty(type=PINBALL_PG_warning)


def _simplify_props(context: Context) -> PINBALL_PG_simplify:
    return getattr(context.scene, SIMPLIFY_SCENE_PROP)


def _draw_simplify_settings(layout: UILayout, props: PINBALL_PG_simplify) -> None:
    """The simplify parameters, drawn identically by both panels."""
    layout.prop(props, "use_column_override")
    if props.use_column_override:
        layout.prop(props, "column_override")
    else:
        layout.prop(props, "angle_threshold_deg")
    layout.prop(props, "min_grid_confidence")


def _parse_columns(text: str) -> tuple[int, ...]:
    """Parse '0,2,8,10' into a tuple. Raises ValueError on anything else, which
    the operator turns into a UI error rather than a console traceback."""
    cleaned = text.replace(" ", "")
    if not cleaned:
        return ()
    return tuple(int(part) for part in cleaned.split(",") if part)


def _simplify_settings(
    core: ModuleType, props: PINBALL_PG_simplify
) -> "SimplifySettings":
    override = (
        _parse_columns(props.column_override) if props.use_column_override else ()
    )
    return core.SimplifySettings(
        angle_threshold_deg=props.angle_threshold_deg,
        keep_columns_override=override,
        min_grid_confidence=props.min_grid_confidence,
    )


def _store_simplify_preview(
    props: PINBALL_PG_simplify, plan: "SimplifyPlan"
) -> None:
    props.preview_warnings.clear()
    for warning in plan.warnings:
        item = props.preview_warnings.add()
        item.message = warning.message
        item.blocking = warning.blocking

    grid = plan.grid
    props.preview_source = plan.source.name
    props.preview_width = grid.width
    props.preview_rows = grid.rows
    props.preview_shells = grid.shells
    props.preview_confidence = grid.confidence
    props.preview_keep = ", ".join(str(c) for c in plan.keep_columns)
    props.preview_dissolve = ", ".join(str(c) for c in plan.dissolve_columns)
    props.preview_edges_hit = plan.edges_to_dissolve
    props.preview_verts_before = grid.verts
    props.preview_verts_after = plan.predicted_verts
    props.preview_blocked = bool(plan.blocking_warnings)
    props.preview_valid = True


def _simplify_is_stale(context: Context, props: PINBALL_PG_simplify) -> bool:
    obj = context.active_object
    return props.preview_source != (obj.name if obj is not None else "")


# --------------------------------------------------------- simplify operators

class PINBALL_OT_simplify_preview(Operator):
    bl_idname = "pinball.simplify_preview"
    bl_label = "Analyze"
    bl_description = "Recover the grid and report which profile columns are redundant"
    # No UNDO: writes only the analysis cache.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        return _active_mesh(context) is not None

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _simplify_props(context)
        try:
            core = _get_simplify()
            plan = core.build_simplify_plan(context, _simplify_settings(core, props))
        except ValueError as exc:
            props.preview_valid = False
            self.report({'ERROR'}, f"Bad column list: {exc}")
            return {'CANCELLED'}
        except Exception as exc:
            # Broad on purpose: a SimplifyError or a failed module load both
            # belong in the status bar, not as a traceback in a hidden console.
            props.preview_valid = False
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        _store_simplify_preview(props, plan)
        self.report(
            {'INFO'},
            f"width {plan.grid.width}: keep {list(plan.keep_columns)}, "
            f"{plan.grid.verts} -> {plan.predicted_verts} verts",
        )
        return {'FINISHED'}


class PINBALL_OT_simplify_apply(Operator):
    bl_idname = "pinball.simplify_apply"
    bl_label = "Simplify"
    bl_description = "Dissolve the redundant profile columns. Edits the mesh in place"
    # UNDO matters more here than for the export: this edits in place with no
    # duplicate, so Ctrl+Z is the only way back.
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        props = _simplify_props(context)
        return (
            _active_mesh(context) is not None
            and props.preview_valid
            and not props.preview_blocked
            and not _simplify_is_stale(context, props)
        )

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _simplify_props(context)
        try:
            core = _get_simplify()
            # Rebuilt rather than reused: the cache is a display projection.
            plan = core.build_simplify_plan(context, _simplify_settings(core, props))
            result = core.execute_simplify_plan(context, plan)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        props.preview_valid = False
        self.report(
            {'INFO'},
            f"{result.verts_before} -> {result.verts_after} verts, "
            f"{result.faces_before} -> {result.faces_after} faces",
        )
        return {'FINISHED'}


class PINBALL_PT_simplify(Panel):
    bl_label = "Simplify"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Pinball"

    def draw(self, context: Context | None) -> None:
        assert context is not None
        # Cheap reads only. build_simplify_plan() walks every edge and must never
        # be called from here.
        layout = self.layout
        assert layout is not None  # See PINBALL_PT_spline_export.draw.
        props = _simplify_props(context)
        mesh = _active_mesh(context)

        header = layout.box()
        if mesh is None:
            header.label(text="Select a converted mesh", icon='ERROR')
        else:
            header.label(text=mesh.name, icon='OUTLINER_OB_MESH')

        _draw_simplify_settings(layout.column(), props)

        layout.separator()
        layout.operator(PINBALL_OT_simplify_preview.bl_idname, icon='VIEWZOOM')

        if props.preview_valid:
            if _simplify_is_stale(context, props):
                layout.label(text="Selection changed, analyze again",
                             icon='FILE_REFRESH')
            else:
                box = layout.box()
                box.label(
                    text=f"{props.preview_width} wide x {props.preview_rows} long"
                         f" x {props.preview_shells} shells",
                    icon='MESH_GRID',
                )
                box.label(text=f"grid confidence {props.preview_confidence:.1%}")
                box.label(text=f"keep {props.preview_keep}", icon='CHECKMARK')
                box.label(text=f"dissolve {props.preview_dissolve}", icon='X')
                box.label(text=f"{props.preview_verts_before} -> "
                               f"{props.preview_verts_after} verts")
                for warning in props.preview_warnings:
                    box.label(
                        text=warning.message,
                        icon='CANCEL' if warning.blocking else 'ERROR',
                    )

        # poll() greys this out on its own.
        layout.operator(PINBALL_OT_simplify_apply.bl_idname, icon='MOD_DECIM')


# ------------------------------------------------------------------- uv state

class PINBALL_PG_uv_layout(PropertyGroup):
    """UV layout inputs plus a cached projection of the last analysis.

    Same discipline as the other groups: plain values only, never the UVPlan,
    which holds a live object reference.
    """

    fill_height: FloatProperty(
        name="Fill Height",
        description="Share of the UV height each island fills, centred. Each "
                    "island is scaled uniformly, so density differs between "
                    "islands but never within one",
        default=0.95, min=0.05, max=1.0, subtype='FACTOR',
    )
    margin: FloatProperty(
        name="Margin",
        description="UV-space gap left and right of, and between, islands",
        default=0.01, min=0.0, max=0.099, precision=3,
    )
    min_grid_confidence: FloatProperty(
        name="Min Grid Confidence",
        description="Refuse if the row-major grid model explains less than this "
                    "share of the mesh's edges",
        default=0.90, min=0.0, max=1.0,
    )

    # --- cached analysis, for display only
    preview_valid: BoolProperty(default=False)
    preview_source: StringProperty()
    preview_grid: StringProperty()
    # One display line per island, newline-joined. A CollectionProperty would
    # need its own PropertyGroup for what is only ever drawn as text.
    preview_islands: StringProperty()
    preview_width_fit: FloatProperty()
    preview_seams: StringProperty()
    preview_blocked: BoolProperty(default=False)
    preview_warnings: CollectionProperty(type=PINBALL_PG_warning)


def _uv_props(context: Context) -> PINBALL_PG_uv_layout:
    return getattr(context.scene, UV_SCENE_PROP)


def _draw_uv_settings(layout: UILayout, props: PINBALL_PG_uv_layout) -> None:
    """The UV layout parameters, drawn identically by both panels."""
    layout.prop(props, "fill_height")
    layout.prop(props, "margin")
    layout.prop(props, "min_grid_confidence")


def _uv_settings(core: ModuleType, props: PINBALL_PG_uv_layout) -> "UVSettings":
    return core.UVSettings(
        min_grid_confidence=props.min_grid_confidence,
        margin=props.margin,
        fill_height=props.fill_height,
    )


def _store_uv_preview(props: PINBALL_PG_uv_layout, plan: "UVPlan") -> None:
    props.preview_warnings.clear()
    for warning in plan.warnings:
        item = props.preview_warnings.add()
        item.message = warning.message
        item.blocking = warning.blocking

    grid = plan.grid
    props.preview_source = plan.source.name
    props.preview_grid = (
        f"{grid.width} wide x {grid.rows} long x {grid.shells} shells, "
        f"{grid.confidence:.1%}"
    )
    props.preview_islands = "\n".join(
        f"{i.name}: {i.faces} faces, U {'reversed' if i.u_reversed else 'forward'}, "
        f"{i.mirrored_faces} mirrored"
        for i in plan.islands
    )
    props.preview_width_fit = plan.width_fit
    props.preview_seams = (
        f"{plan.seams_to_mark} seams (replacing {plan.seams_existing}), "
        f"layer {plan.uv_layer or 'UVMap (new)'}"
    )
    props.preview_blocked = bool(plan.blocking_warnings)
    props.preview_valid = True


def _uv_is_stale(context: Context, props: PINBALL_PG_uv_layout) -> bool:
    obj = context.active_object
    return props.preview_source != (obj.name if obj is not None else "")


# --------------------------------------------------------------- uv operators

class PINBALL_OT_uv_preview(Operator):
    bl_idname = "pinball.uv_preview"
    bl_label = "Analyze"
    bl_description = "Recover the grid and report the UV islands and seams it would lay out"
    # No UNDO: writes only the analysis cache.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        return _active_mesh(context) is not None

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _uv_props(context)
        try:
            core = _get_uv()
            plan = core.build_uv_plan(context, _uv_settings(core, props))
        except Exception as exc:
            # Broad on purpose, as in the other operators: a UVError, a
            # SimplifyError from grid recovery, or a failed module load all
            # belong in the status bar.
            props.preview_valid = False
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        _store_uv_preview(props, plan)
        mirrored = sum(i.mirrored_faces for i in plan.islands)
        self.report(
            {'INFO'},
            f"{len(plan.islands)} islands, {plan.seams_to_mark} seams, "
            f"{mirrored} mirrored faces",
        )
        return {'FINISHED'}


class PINBALL_OT_uv_apply(Operator):
    bl_idname = "pinball.uv_apply"
    bl_label = "Lay Out UVs"
    bl_description = ("Write straight UV islands and mark seams. Edits the mesh in "
                      "place, replacing its UVs and every seam")
    # In place, like Simplify: Ctrl+Z is the way back.
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: Context | None) -> bool:
        assert context is not None
        props = _uv_props(context)
        return (
            _active_mesh(context) is not None
            and props.preview_valid
            and not props.preview_blocked
            and not _uv_is_stale(context, props)
        )

    def execute(self, context: Context | None) -> "set[OperatorReturnItems]":
        assert context is not None
        props = _uv_props(context)
        try:
            core = _get_uv()
            # Rebuilt rather than reused: the cache is a display projection.
            plan = core.build_uv_plan(context, _uv_settings(core, props))
            result = core.execute_uv_plan(context, plan)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        props.preview_valid = False
        created = " (created)" if result.uv_layer_created else ""
        self.report(
            {'INFO'},
            f"{result.faces_written} faces -> {result.uv_layer}{created}, "
            f"{result.seams_marked} seams marked, {result.seams_cleared} cleared",
        )
        return {'FINISHED'}


class PINBALL_PT_uv_layout(Panel):
    bl_label = "UV Layout"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Pinball"

    def draw(self, context: Context | None) -> None:
        assert context is not None
        # Cheap reads only. build_uv_plan() walks every face and must never be
        # called from here.
        layout = self.layout
        assert layout is not None  # See PINBALL_PT_spline_export.draw.
        props = _uv_props(context)
        mesh = _active_mesh(context)

        header = layout.box()
        if mesh is None:
            header.label(text="Select a converted mesh", icon='ERROR')
        else:
            header.label(text=mesh.name, icon='OUTLINER_OB_MESH')

        _draw_uv_settings(layout.column(), props)

        layout.separator()
        layout.operator(PINBALL_OT_uv_preview.bl_idname, icon='VIEWZOOM')

        if props.preview_valid:
            if _uv_is_stale(context, props):
                layout.label(text="Selection changed, analyze again",
                             icon='FILE_REFRESH')
            else:
                box = layout.box()
                box.label(text=props.preview_grid, icon='MESH_GRID')
                for line in props.preview_islands.splitlines():
                    box.label(text=line, icon='UV')
                if props.preview_width_fit < 1.0:
                    box.label(text=f"U compressed to {props.preview_width_fit:.1%} "
                                   f"to fit side by side", icon='ERROR')
                box.label(text=props.preview_seams, icon='EDGESEL')
                for warning in props.preview_warnings:
                    box.label(
                        text=warning.message,
                        icon='CANCEL' if warning.blocking else 'ERROR',
                    )

        # poll() greys this out on its own.
        layout.operator(PINBALL_OT_uv_apply.bl_idname, icon='UV_DATA')


# --------------------------------------------------------------- registration

# Order matters: PINBALL_PG_warning must register before either group whose
# CollectionProperty points at it. The factory unregisters in reverse.
_CLASSES = (
    PINBALL_PG_warning,
    PINBALL_PG_spline_export,
    PINBALL_PG_simplify,
    PINBALL_PG_uv_layout,
    PINBALL_OT_spline_preview,
    PINBALL_OT_spline_export,
    PINBALL_OT_simplify_preview,
    PINBALL_OT_simplify_apply,
    PINBALL_OT_uv_preview,
    PINBALL_OT_uv_apply,
    PINBALL_PT_spline_export,
    PINBALL_PT_simplify,
    PINBALL_PT_uv_layout,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(_CLASSES)

# Scene property name -> the PropertyGroup it points at.
_SCENE_PROPS = {
    SCENE_PROP: PINBALL_PG_spline_export,
    SIMPLIFY_SCENE_PROP: PINBALL_PG_simplify,
    UV_SCENE_PROP: PINBALL_PG_uv_layout,
}


def register() -> None:
    _register_classes()
    for name, group in _SCENE_PROPS.items():
        setattr(bpy.types.Scene, name, PointerProperty(type=group))


def unregister() -> None:
    for name in _SCENE_PROPS:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
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
    _panels = [c.bl_label for c in _CLASSES if issubclass(c, Panel)]
    print(
        f"{_panels} registered ({len(_CLASSES)} classes). "
        f"3D Viewport -> N -> {PINBALL_PT_spline_export.bl_category!r} tab."
    )
