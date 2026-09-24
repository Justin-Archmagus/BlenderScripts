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

    Verified end to end 2026-09-22. build_plan() and execute_plan() both run
    correctly including the FBX write path: the parsed "Unreal - mesh" preset
    kwargs reach bpy.ops.export_scene.fbx, a file is produced, and Unreal imports
    it looking correct. Exercised from both the cli front-end and the panel.

    So the axis and scale settings carried in that preset -- axis_forward='-Z',
    axis_up='Y', bake_space_transform=True -- are confirmed against the real
    importer, not just against the docs.

    Added and tested in Blender 2026-09-23: optional simplify step. ExportSettings.simplify
    carries SimplifySettings or None; build_plan() analyses the evaluated mesh
    and execute_plan() simplifies the converted duplicate before the transform
    and origin steps. Best-effort: when simplify cannot help, the export
    proceeds unsimplified with a warning.

    Hardened and tested in Blender 2026-09-23: excluded/hidden/locked export collections
    refused, Blender-relative export folders resolved, name override sanitized,
    name clashes warned, operator results checked, and execute_plan() rolls back
    its partial mesh (and any file it newly created) on failure.
"""

import ast
import importlib
import re
import unicodedata
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal, get_args

import bpy

# The export can simplify its converted mesh, so this core depends on the
# simplify core -- a sibling in Source/lib, already on sys.path by the time any
# front-end imports this module. Reloaded here because front-ends reload only
# the module they import: without it, an edit to mesh_simplify_core would stay
# invisible to the export until Blender restarts, the exact trap the front-end
# bootstraps exist to avoid. reload() re-executes in place, so a front-end that
# also holds this module sees the same, current, object.
import mesh_simplify_core as simplify_core

importlib.reload(simplify_core)

PRESET_SUBDIR = "operator/export_scene.fbx"
PRESET_FILE = "Unreal_-_mesh.py"

# Curve objects paired with a mesh follow <Name>_BezierCurve in this scene. Plain
# descriptive names (InnerRearRamp) are preferred and pass through untouched.
_CURVE_SUFFIXES = ("_BezierCurve", "_BézierCurve", "_Curve")

_PRESET_ASSIGN = re.compile(r"^\s*op\.([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")

# The values bpy.ops.object.origin_set accepts for center, per the 5.2 docs.
# A plain assignment rather than a `type` statement: get_args() cannot see
# through a 3.12+ TypeAliasType, and build_plan() validates against this.
OriginCenter = Literal['MEDIAN', 'BOUNDS']


class PlanError(Exception):
    """A precondition failed. The message is written for the person running it."""


class ExportError(PlanError):
    """execute_plan() failed partway and rolled back what it had created.

    A PlanError subclass so a front-end catching PlanError still reports it.
    """


class WarningCode(Enum):
    """Front-ends switch on these to pick an icon, a severity, or whether to
    disable a button. The message is a fallback, not the identity."""

    NO_FACES = auto()
    TARGET_EXISTS = auto()
    MISSING_EXPORT_DIR = auto()
    NAME_SANITIZED = auto()
    NAME_TAKEN = auto()
    SIMPLIFY_SKIPPED = auto()
    SIMPLIFY_NOTE = auto()


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
    origin_center: OriginCenter = 'MEDIAN'
    # None skips simplification. Settings rather than a bool, so on/off and the
    # parameters travel as one value and cannot disagree.
    simplify: simplify_core.SimplifySettings | None = None
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
    # source.data, narrowed to Curve once in build_plan() so nothing downstream
    # touches the untyped Object.data union again.
    curve: bpy.types.Curve
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
    # What simplifying will do, analysed on the evaluated mesh. None when
    # simplify is off, or on but not possible -- the latter with a warning.
    simplify: simplify_core.SimplifyAnalysis | None = None
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
    # Final counts, after simplification if it ran.
    verts: int
    polys: int
    fbx_path: Path | None = None
    simplify: simplify_core.SimplifyResult | None = None


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


def evaluate_source(
    obj: bpy.types.Object, simplify: simplify_core.SimplifySettings | None
) -> tuple[int, int, simplify_core.SimplifyAnalysis | None, str | None]:
    """What the conversion would produce, without converting.

    Returns vertex and polygon counts, plus -- when simplify settings are given
    -- the simplify analysis of that same geometry, or the reason it is not
    possible. One evaluation serves both, since the temporary mesh is the
    expensive part. The analysis holds plain values only, so it survives the
    to_mesh_clear() below.

    Uses the evaluated depsgraph so modifiers (Solidify here) are included. This
    is the expensive call that keeps build_plan() out of Panel.draw().
    """
    depsgraph: bpy.types.Depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated: bpy.types.Object = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        # None for object types with no geometry. The source is always a curve,
        # which yields a mesh (possibly empty), so this is a guard, not a path.
        if mesh is None:
            raise PlanError(f"{obj.name!r} produced no geometry when evaluated.")
        verts, polys = len(mesh.vertices), len(mesh.polygons)
        if simplify is None:
            return verts, polys, None, None
        try:
            return verts, polys, simplify_core.analyze_mesh(mesh, simplify), None
        except simplify_core.SimplifyError as exc:
            return verts, polys, None, str(exc)
    finally:
        evaluated.to_mesh_clear()


def _require(result: AbstractSet[str], action: str) -> None:
    """Raise unless an operator call finished.

    AbstractSet rather than set: operators return a set of Literal strings, and
    set is invariant, so set[str] would reject it.

    bpy.ops reports most failures by returning {'CANCELLED'}, not by raising, so
    an unchecked call fails silently and the next step runs on bad state.
    """
    if 'FINISHED' not in result:
        raise ExportError(f"{action} did not finish (returned {sorted(result)}).")


def _discard(ids: list[bpy.types.ID]) -> None:
    """Best-effort removal of datablocks a failed run created.

    Never raises: it runs while another exception is propagating, and masking
    that exception would hide the actual failure.
    """
    alive: list[bpy.types.ID] = []
    for id_block in ids:
        try:
            id_block.name  # A freed ID raises ReferenceError on any access.
        except ReferenceError:
            continue
        if id_block not in alive:
            alive.append(id_block)
    try:
        bpy.data.batch_remove(alive)
    except Exception:
        pass


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
    """Names of the collections active in this view layer, excluding the root.

    Not the same set as bpy.data.collections, which spans the whole file. Linking
    the duplicate into a collection that is absent from this view layer leaves it
    out of view_layer.objects, and select_only() then cannot make it active -- so
    the export target has to come from here.

    Excluded collections (the view layer checkbox) are skipped along with their
    whole subtree. They still appear in the layer collection tree, but their
    objects are not in view_layer.objects, so for this purpose they are absent.

    The root is excluded deliberately: a scene's master collection is not a member
    of bpy.data.collections, so execute_plan() could not look it up by name.
    """
    names: set[str] = set()

    def walk(layer_collection: bpy.types.LayerCollection) -> None:
        for child in layer_collection.children:
            if child.exclude:
                continue
            names.add(child.collection.name)
            walk(child)

    walk(view_layer.layer_collection)
    return names


def collection_blocker(view_layer: bpy.types.ViewLayer, name: str) -> str | None:
    """Why objects linked into this collection could not be selected, or None.

    Checks the whole chain from the root down, since hiding or locking a parent
    applies to everything beneath it. execute_plan() also verifies the selection
    directly after linking; this exists to refuse early, with a readable reason,
    before anything is created.
    """
    def find(
        layer_collection: bpy.types.LayerCollection,
        trail: list[bpy.types.LayerCollection],
    ) -> list[bpy.types.LayerCollection] | None:
        for child in layer_collection.children:
            here = [*trail, child]
            if child.collection.name == name:
                return here
            found = find(child, here)
            if found is not None:
                return found
        return None

    chain = find(view_layer.layer_collection, [])
    if chain is None:
        return f"Collection {name!r} is not in this view layer."

    for layer_collection in chain:
        collection = layer_collection.collection
        if layer_collection.hide_viewport or collection.hide_viewport:
            return (
                f"Collection {collection.name!r} is hidden in the viewport, so the "
                f"generated mesh could not be selected for conversion. Unhide it."
            )
        if collection.hide_select:
            return (
                f"Collection {collection.name!r} is not selectable, so the "
                f"generated mesh could not be selected for conversion. Re-enable "
                f"selection on it in the Outliner."
            )
    return None


def resolve_export_dir(raw: str) -> Path | None:
    """The export folder as an absolute path, or None when unset.

    A DIR_PATH property filled from Blender's file browser is stored relative to
    the .blend ('//..\\exports\\') whenever the Relative Paths preference is on,
    which is the default. Path() does not understand that prefix -- on Windows
    '//x' reads as a UNC network share -- so it must go through bpy.path.abspath.
    """
    if not raw:
        return None
    if raw.startswith("//") and not bpy.data.filepath:
        raise PlanError(
            f"Export folder {raw!r} is relative to the .blend file, but this file "
            f"has never been saved, so there is nothing to resolve it against. "
            f"Save the file, or choose an absolute folder."
        )
    return Path(bpy.path.abspath(raw)).resolve()


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

    # Exact class check, not isinstance: TextCurve and SurfaceCurve subclass
    # Curve. Narrows Object.data for the type checker, which the type string
    # cannot. Redundant with active_curve() at runtime; that one stays cheap
    # enough for poll() and draw(), this one is what the plan carries.
    curve = source.data
    if type(curve) is not bpy.types.Curve:
        raise PlanError(f"{source.name!r} has {type(curve).__name__} data, not Curve.")

    if not settings.export_collection:
        raise PlanError("No export collection set.")

    # The Literal annotation is not enforced at runtime, and the script front-end
    # passes a plain string. Checked here so a typo fails at Preview rather than
    # inside origin_set, after the conversion has already run.
    if settings.origin_center not in get_args(OriginCenter):
        raise PlanError(
            f"origin_center {settings.origin_center!r} must be one of "
            f"{get_args(OriginCenter)}."
        )

    # Stronger than an existence check in bpy.data: anything in the view layer is
    # in bpy.data.collections, but not the reverse.
    if settings.export_collection not in view_layer_collection_names(context.view_layer):
        raise PlanError(
            f"Collection {settings.export_collection!r} is not in this view layer. "
            f"An object linked there would not be selectable, so the export would "
            f"fail partway through."
        )
    blocker = collection_blocker(context.view_layer, settings.export_collection)
    if blocker is not None:
        raise PlanError(blocker)

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

    warnings: list[PlanWarning] = []

    # The override is sanitized too. It becomes a filename, so unsanitized it
    # could carry an Unreal-hostile dot, a Windows-invalid character, or a '..\'
    # that writes outside the export folder.
    if settings.name_override:
        mesh_name = sanitize_for_unreal(settings.name_override)
        if mesh_name != settings.name_override:
            warnings.append(PlanWarning(
                WarningCode.NAME_SANITIZED,
                f"Name override {settings.name_override!r} was sanitized to "
                f"{mesh_name!r}.",
            ))
    else:
        mesh_name = derive_export_name(source.name)

    # Blender resolves a name clash by renaming the new object, so the mesh --
    # and the object inside the FBX -- would silently get a numbered suffix.
    existing = bpy.data.objects.get(mesh_name)
    if existing is not None:
        if existing == source:
            detail = (
                f"The source curve is itself named {mesh_name!r}, so the new mesh "
                f"will get a numbered name like '{mesh_name}.001'. Rename the curve "
                f"(e.g. {mesh_name}_BezierCurve) or set a name override."
            )
        else:
            detail = (
                f"An object named {mesh_name!r} already exists, likely an earlier "
                f"export, so the new mesh will get a numbered name like "
                f"'{mesh_name}.001'. Delete or rename the old one first."
            )
        warnings.append(PlanWarning(
            WarningCode.NAME_TAKEN,
            f"{detail} The FBX file is still named {mesh_name}.fbx, but the "
            f"object inside it carries the numbered name.",
        ))

    # None only when export_dir is empty, which the write_fbx guard above already
    # rules out for any plan that will actually write a file. When write_fbx is
    # False but a directory is set, this still reports where the file would land.
    export_dir = resolve_export_dir(settings.export_dir)
    out_path: Path | None = (
        export_dir / f"{mesh_name}.fbx" if export_dir is not None else None
    )
    verts, polys, analysis, skip_reason = evaluate_source(source, settings.simplify)
    units = context.scene.unit_settings

    # Simplify is best-effort inside an export: when it cannot help, the export
    # still goes ahead unsimplified, and says why. None of these warnings block.
    if settings.simplify is not None:
        if analysis is not None and analysis.blocking_warnings:
            skip_reason = "; ".join(w.message for w in analysis.blocking_warnings)
            analysis = None
        if analysis is None:
            warnings.append(PlanWarning(
                WarningCode.SIMPLIFY_SKIPPED,
                f"Simplify skipped, exporting unsimplified: {skip_reason}",
            ))
        else:
            for note in analysis.warnings:
                warnings.append(PlanWarning(
                    WarningCode.SIMPLIFY_NOTE, f"Simplify: {note.message}"
                ))

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
        curve=curve,
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
        simplify=analysis,
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
    curve_copy = plan.curve.copy()
    duplicate.data = curve_copy
    duplicate.name = plan.mesh_name

    # Everything this run creates, so a failure can remove it. All or nothing: a
    # half-converted duplicate left in the Export collection would be picked up
    # by the next run's name-clash check, or exported by hand as if it were good.
    created: list[bpy.types.ID] = [duplicate, curve_copy]
    created_file = False
    try:
        export_collection.objects.link(duplicate)

        select_only(context, duplicate)
        if not duplicate.select_get() or context.view_layer.objects.active != duplicate:
            raise ExportError(
                f"Could not select the duplicate in {settings.export_collection!r}; "
                f"the collection may be hidden or locked."
            )

        _require(bpy.ops.object.convert(target='MESH'), "Convert to mesh")
        # convert can finish having converted nothing. Carrying on with a curve
        # would apply the transform to it -- the deformation the ordering below
        # exists to avoid -- and then export the curve as if it were the mesh.
        # The exact class check also narrows duplicate.data for what follows.
        mesh = duplicate.data
        if type(mesh) is not bpy.types.Mesh:
            raise ExportError(
                f"Conversion left {duplicate.name!r} as a {duplicate.type}, not a MESH."
            )
        created.append(mesh)

        # Simplify straight after converting, before the transform and origin
        # steps. The plan analysed the evaluated mesh in this same local space,
        # so simplify_mesh()'s re-check sees identical geometry; and origin_set
        # MEDIAN averages the vertices, so it has to see the final vertex set.
        # A mismatch raises, which the handler below turns into a rollback.
        simplified: simplify_core.SimplifyResult | None = None
        if plan.simplify is not None:
            simplified = simplify_core.simplify_mesh(mesh, plan.simplify)

        # Order matters: convert first so the curve geometry and Solidify bake
        # into real vertices, then apply rot/scale on the resulting mesh. Applying
        # the transform to the curve beforehand rescales control points and
        # reinterprets bevel depth, which deforms the ramp.
        _require(
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True),
            "Apply rotation and scale",
        )
        _require(
            bpy.ops.object.origin_set(
                type='ORIGIN_GEOMETRY', center=settings.origin_center
            ),
            "Origin to geometry",
        )
        duplicate.location = (0.0, 0.0, 0.0)
        # transform_apply and origin_set edit single-user data in place, so this
        # is still the mesh convert produced.
        mesh.name = plan.mesh_name

        written: Path | None = None
        if settings.write_fbx and out_path is not None:
            select_only(context, duplicate)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            created_file = not plan.target_exists
            _require(
                bpy.ops.export_scene.fbx(filepath=str(out_path), **plan.preset_kwargs),
                "FBX export",
            )
            if not out_path.is_file():
                raise ExportError(f"FBX export reported success but wrote no {out_path}.")
            written = out_path
    except Exception as exc:
        _discard(created)
        # Only a file this run created is removed. An overwritten one cannot be
        # restored -- which is why overwriting is a separate opt-in.
        if created_file and out_path is not None:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ExportError(
            f"{exc} The partially built mesh was removed (the selection was not "
            f"restored)."
        ) from exc

    return ExportResult(
        mesh_object_name=duplicate.name,
        verts=len(mesh.vertices),
        polys=len(mesh.polygons),
        fbx_path=written,
        simplify=simplified,
    )
