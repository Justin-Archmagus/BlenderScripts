"""
spline_export_core.py

Shared logic for converting a curve into an Unreal-bound mesh. No UI, no
operators, no presentation, no module-level Blender state -- so the same code
backs both the Text Editor script and the panel operators.

HOW TO RUN
    Not runnable on its own; import it. See spline_to_unreal_mesh.py for the
    script front-end.

DESIGN
    This module returns data, never formatted output. build_plan() is read-only:
    it raises PlanError on any precondition failure and otherwise returns
    everything needed to report or execute. execute_plan() is the only function
    here that mutates anything.

    Presentation belongs to the front-end. The script renders a text report into
    a Text datablock; the panel draws widgets and calls Operator.report(). Neither
    concern appears here, which is why warnings carry a WarningCode rather than
    pre-formatted prose.

    build_plan() is safe from an operator's execute() but NOT from Panel.draw() --
    it evaluates the depsgraph, far too expensive to run every redraw. Panels
    cache what build_plan() returned and draw from the cache.

STATUS
    Extracted from spline_to_unreal_mesh.py on 2026-09-22, then restructured to
    return data instead of formatted output.

    Verified 2026-09-22: build_plan() and the conversion half of execute_plan()
    both run correctly, tested via the script with write_fbx=False. The FBX write
    path -- preset kwargs reaching bpy.ops.export_scene.fbx, the overwrite guard,
    and whether Unreal accepts the result -- has still never been exercised.
"""

import ast
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

import bpy

PRESET_SUBDIR = "operator/export_scene.fbx"
PRESET_FILE = "Unreal_-_mesh.py"

# Curve objects paired with a mesh follow <Name>_BezierCurve in this scene. Plain
# descriptive names (InnerRearRamp) are preferred and pass through untouched.
_CURVE_SUFFIXES = ("_BezierCurve", "_BézierCurve", "_Curve")

_PRESET_ASSIGN = re.compile(r"^\s*op\.([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")


class PlanError(Exception):
    """A precondition failed. The message is written for the person running it."""


class WarningCode(Enum):
    """Front-ends switch on these to pick an icon, a severity, or whether to
    disable a button. The message is a fallback, not the identity."""

    NO_FACES = auto()
    TARGET_EXISTS = auto()
    MISSING_EXPORT_DIR = auto()


@dataclass(frozen=True)
class PlanWarning:
    code: WarningCode
    message: str
    blocking: bool = False


@dataclass(frozen=True)
class ExportSettings:
    """Front-end agnostic inputs.

    The script fills this from module constants; the panel fills it from a
    PropertyGroup. Keeping the front-ends behind one shape is the whole point of
    this module.

    Frozen because an ExportPlan holds the settings it was built from. If a caller
    could mutate them afterwards, the plan's derived fields (out_path above all)
    would silently stop matching its own settings.
    """

    export_dir: str
    export_collection: str = "Export"
    name_override: str = ""
    origin_center: str = 'MEDIAN'
    write_fbx: bool = True
    allow_overwrite: bool = False


@dataclass
class ExportPlan:
    """Everything build_plan() learned, with nothing applied yet.

    Carries the settings it was built from, so execute_plan() cannot be handed a
    plan and a contradicting set of settings. out_path is None exactly when
    settings.export_dir is empty, which build_plan() only permits when write_fbx
    is False.
    """

    source: bpy.types.Object
    settings: ExportSettings
    mesh_name: str
    out_path: Path | None
    preset_path: Path | None
    preset_kwargs: dict[str, Any]
    verts: int
    polys: int
    unit_scale: float
    unit_system: str
    target_exists: bool
    stale_preset_path: str | None = None
    unparsed_preset_lines: list[str] = field(default_factory=list)
    warnings: list[PlanWarning] = field(default_factory=list)

    @property
    def blocking_warnings(self) -> list[PlanWarning]:
        """Anything that would make execute_plan() raise. Panels disable Export
        on a non-empty list rather than letting the user click into an error."""
        return [w for w in self.warnings if w.blocking]


@dataclass
class ExportResult:
    mesh_object_name: str
    verts: int
    polys: int
    fbx_path: Path | None = None


# -------------------------------------------------------------------- helpers

def sanitize_for_unreal(name: str) -> str:
    """Fold accents and replace anything Unreal will not accept in an asset name.

    Dots matter most: they are path separators in Unreal asset references, so a
    Blender duplicate suffix like .001 cannot survive into the filename.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_only = stripped.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", ascii_only).strip("_")
    return cleaned or "UnnamedMesh"


def derive_export_name(curve_name: str) -> str:
    for suffix in _CURVE_SUFFIXES:
        if curve_name.endswith(suffix):
            return sanitize_for_unreal(curve_name[: -len(suffix)])
    return sanitize_for_unreal(curve_name)


def find_preset() -> Path:
    for root in bpy.utils.preset_paths(PRESET_SUBDIR):
        candidate = Path(root) / PRESET_FILE
        if candidate.is_file():
            return candidate
    raise PlanError(
        f"FBX preset {PRESET_FILE!r} not found in any preset path for "
        f"{PRESET_SUBDIR!r}. Re-save it from the FBX export dialog."
    )


def load_preset_kwargs(
    preset_path: Path,
) -> tuple[dict[str, Any], list[str], str | None]:
    """Read an operator preset into kwargs for bpy.ops.export_scene.fbx().

    Presets assign onto bpy.context.active_operator, which only exists after an
    interactive operator run, so they cannot simply be exec'd from a script.
    Parsing keeps the preset file as the single source of truth: edit it in the
    FBX dialog and this follows.
    """
    kwargs: dict[str, Any] = {}
    unparsed: list[str] = []
    for line in preset_path.read_text(encoding="utf-8").splitlines():
        match = _PRESET_ASSIGN.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        try:
            kwargs[key] = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            unparsed.append(line.strip())

    # Older copies of this preset carry a hardcoded filepath from whatever was
    # exported when it was saved. Honouring it would overwrite an unrelated file.
    stale_path = kwargs.pop("filepath", None)
    return kwargs, unparsed, stale_path


def evaluated_counts(obj: bpy.types.Object) -> tuple[int, int]:
    """Vertex/polygon counts the conversion would produce, without converting.

    Uses the evaluated depsgraph so modifiers (Solidify here) are included. This
    is the expensive call that keeps build_plan() out of Panel.draw().
    """
    depsgraph: bpy.types.Depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated: bpy.types.Object = obj.evaluated_get(depsgraph)
    mesh: bpy.types.Mesh = evaluated.to_mesh()
    try:
        return len(mesh.vertices), len(mesh.polygons)
    finally:
        evaluated.to_mesh_clear()


def select_only(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Operators act on the selection, and the FBX preset exports the selection.

    The preset's object_types includes 'OTHER', which covers curves -- so a stray
    selected curve silently lands in the FBX as a second mesh.
    """
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    context.view_layer.objects.active = obj


# ----------------------------------------------------------------- plan/apply

def active_curve(context: bpy.types.Context) -> bpy.types.Object | None:
    """The source curve, or None. Cheap enough for Panel.draw() and poll()."""
    obj = context.active_object
    return obj if obj is not None and obj.type == 'CURVE' else None


def view_layer_collection_names(view_layer: bpy.types.ViewLayer) -> set[str]:
    """Names of the collections linked into this view layer, excluding the root.

    Not the same set as bpy.data.collections, which spans the whole file. Linking
    the duplicate into a collection that is absent from this view layer leaves it
    out of view_layer.objects, and select_only() then cannot make it active -- so
    the export target has to come from here.

    The root is excluded deliberately: a scene's master collection is not a member
    of bpy.data.collections, so execute_plan() could not look it up by name.
    """
    names: set[str] = set()

    def walk(layer_collection: bpy.types.LayerCollection) -> None:
        for child in layer_collection.children:
            names.add(child.collection.name)
            walk(child)

    walk(view_layer.layer_collection)
    return names


def build_plan(context: bpy.types.Context, settings: ExportSettings) -> ExportPlan:
    """Gather everything needed to report or execute. Changes nothing."""
    if context.mode != 'OBJECT':
        raise PlanError(f"Blender is in {context.mode}. Tab into Object Mode first.")

    source = active_curve(context)
    if source is None:
        active = context.active_object
        if active is None:
            raise PlanError("No active object. Select the source curve.")
        raise PlanError(f"Active object {active.name!r} is a {active.type}, not a CURVE.")

    if not settings.export_collection:
        raise PlanError("No export collection set.")

    # Stronger than an existence check in bpy.data: anything in the view layer is
    # in bpy.data.collections, but not the reverse.
    if settings.export_collection not in view_layer_collection_names(context.view_layer):
        raise PlanError(
            f"Collection {settings.export_collection!r} is not in this view layer. "
            f"An object linked there would not be selectable, so the export would "
            f"fail partway through."
        )

    if settings.write_fbx and not hasattr(bpy.ops.export_scene, "fbx"):
        raise PlanError("FBX export is unavailable. Enable the FBX format extension.")

    if settings.write_fbx and not settings.export_dir:
        raise PlanError("No export directory set.")

    preset_path: Path | None = None
    preset_kwargs: dict[str, Any] = {}
    unparsed: list[str] = []
    stale_path: str | None = None
    if settings.write_fbx:
        preset_path = find_preset()
        preset_kwargs, unparsed, stale_path = load_preset_kwargs(preset_path)

    mesh_name = settings.name_override or derive_export_name(source.name)

    # None only when export_dir is empty, which the write_fbx guard above already
    # rules out for any plan that will actually write a file. When write_fbx is
    # False but a directory is set, this still reports where the file would land.
    out_path: Path | None = (
        Path(settings.export_dir) / f"{mesh_name}.fbx" if settings.export_dir else None
    )
    verts, polys = evaluated_counts(source)
    units = context.scene.unit_settings

    warnings: list[PlanWarning] = []
    if polys == 0:
        warnings.append(PlanWarning(
            WarningCode.NO_FACES,
            "The evaluated curve has no faces. Converting it yields an edge-only "
            "mesh and Unreal will import empty geometry. Give the curve a bevel "
            "or extrude first.",
        ))

    target_exists = False
    if settings.write_fbx and out_path is not None:
        if out_path.exists():
            target_exists = True
            warnings.append(PlanWarning(
                WarningCode.TARGET_EXISTS,
                f"{out_path.name} already exists and "
                + ("will be overwritten." if settings.allow_overwrite
                   else "allow_overwrite is False."),
                blocking=not settings.allow_overwrite,
            ))
        elif not out_path.parent.is_dir():
            warnings.append(PlanWarning(
                WarningCode.MISSING_EXPORT_DIR,
                f"Export directory does not exist and will be created: "
                f"{out_path.parent}",
            ))

    return ExportPlan(
        source=source,
        settings=settings,
        mesh_name=mesh_name,
        out_path=out_path,
        preset_path=preset_path,
        preset_kwargs=preset_kwargs,
        verts=verts,
        polys=polys,
        unit_scale=units.scale_length,
        unit_system=units.system,
        target_exists=target_exists,
        stale_preset_path=stale_path,
        unparsed_preset_lines=unparsed,
        warnings=warnings,
    )


def execute_plan(context: bpy.types.Context, plan: ExportPlan) -> ExportResult:
    """Apply the plan. The only mutating function in this module.

    Takes no settings argument on purpose: the plan already carries the settings
    it was derived from, so there is no way to execute a plan under settings it
    was not built for.
    """
    settings = plan.settings
    # Bound to a local so the None check below narrows it for a type checker.
    out_path = plan.out_path

    # All write preconditions together, nested under write_fbx so the None check
    # narrows out_path for everything that follows it.
    if settings.write_fbx:
        if out_path is None:
            # Unreachable via build_plan, which rejects this combination up front.
            # Explicit so a future change surfaces as a readable error rather than
            # an AttributeError inside the mkdir below.
            raise PlanError(
                "Plan has no output path but write_fbx is set. This plan was built "
                "without an export directory and cannot write a file."
            )
        if plan.target_exists and not settings.allow_overwrite:
            raise PlanError(
                f"{out_path.name} already exists and allow_overwrite is False. "
                f"Rename the export, or set allow_overwrite once you are sure."
            )

    source = plan.source
    export_collection: bpy.types.Collection = bpy.data.collections[
        settings.export_collection
    ]

    duplicate: bpy.types.Object = source.copy()
    duplicate.data = source.data.copy()
    duplicate.name = plan.mesh_name
    export_collection.objects.link(duplicate)

    select_only(context, duplicate)
    bpy.ops.object.convert(target='MESH')

    # Order matters: convert first so the curve geometry and Solidify bake into
    # real vertices, then apply rot/scale on the resulting mesh. Applying the
    # transform to the curve beforehand rescales control points and reinterprets
    # bevel depth, which deforms the ramp.
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center=settings.origin_center)
    duplicate.location = (0.0, 0.0, 0.0)
    duplicate.data.name = plan.mesh_name

    written: Path | None = None
    if settings.write_fbx and out_path is not None:
        select_only(context, duplicate)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.fbx(filepath=str(out_path), **plan.preset_kwargs)
        written = out_path

    return ExportResult(
        mesh_object_name=duplicate.name,
        verts=len(duplicate.data.vertices),
        polys=len(duplicate.data.polygons),
        fbx_path=written,
    )
