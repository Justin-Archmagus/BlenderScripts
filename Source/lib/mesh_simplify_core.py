"""
mesh_simplify_core.py

Shared logic for collapsing redundant profile columns out of a curve-converted
mesh. No UI, no presentation -- same contract as spline_export_core.

HOW TO RUN
    Not runnable on its own; import it. See cli/simplify_mesh.py.

WHAT IT DOES
    A curve converted with a bevel produces a regular grid: W columns across the
    profile, R rows along the length, and with Solidify two shells welded by a
    rim. Most columns sit in the middle of a straight run of the profile and
    carry no shape -- dissolving their lengthwise edge loops loses nothing.

    With Solidify, each end of the ramp is also capped by a strip of rim quads
    joining the two shells' end rows. The rim edges of that strip that fall in a
    redundant column are dissolved too. Without that, an end-row vertex keeps its
    rim edge, stays 3-valent after its lengthwise edge goes, and survives --
    2 shells x (dissolved columns) stray vertices at each end.

    Columns are classified by the angle the profile turns through at them. A
    column whose incoming and outgoing directions are collinear within
    angle_threshold is redundant. Boundary columns are always kept.

    Measured on InnerRearRamp at width 11, that rule selects exactly columns
    {0, 2, 8, 10} to keep -- the same set picked by hand. It generalises: at
    width 5 it keeps {0, 1, 3, 4} without anyone re-deriving anything.

WHY IT VERIFIES THE GRID FIRST
    The column arithmetic is only valid on a genuine row-major grid. Dissolving
    the wrong edges on an unexpected topology would quietly wreck the mesh, so
    recover_grid() reconciles vertex and edge counts against the model and
    raises rather than guessing.

ENTRY POINTS
    Active-object workflow (cli/simplify_mesh.py, the Simplify panel):
        build_simplify_plan()  -> SimplifyPlan, read-only, Edit or Object mode
        execute_simplify_plan() mutates the plan's mesh in place

    Mesh-level, for callers that own the mesh (the export pipeline):
        analyze_mesh()   -> SimplifyAnalysis, read-only, plain values only
        simplify_mesh()     mutates the given mesh, Object mode

    Both mutating paths re-analyse before dissolving and refuse if the mesh no
    longer matches what was planned.

STATUS
    Written 2026-09-22 against measurements from InnerRearRamp, and executed
    successfully the same day on that mesh: 2134 -> 804 verts, 2132 -> 788 faces,
    1344 edges dissolved, via both the cli front-end and the panel. The angle
    detector selected {0, 2, 8, 10}, matching the hand-picked set.

    Run again 2026-09-23 on InnerRearRamp.001 at width 7 (keep {0, 1, 5, 6}):
    1358 -> 788 verts, which left the end rows full width -- 2 shells x 3
    dissolved columns = 6 stray vertices at each end. The vertex prediction had
    been corrected to match those leftovers rather than to remove them.

    Fixed and tested in Blender 2026-09-23: end-cap rim edges in redundant columns are now
    dissolved too, and rim edges count toward grid confidence (that mesh read
    92.5% only because its 204 rim edges were uncounted; with them, 100%). On the
    same mesh expect 576 + 6 = 582 edges dissolved and 2 x 97 x 4 = 776 verts.
    Also refuses multi-user and library-linked mesh data.

    Tested in Blender 2026-09-23: split into analysis and application so the export
    pipeline can simplify its converted duplicate. Execution now re-checks rows,
    shells and the dissolve set as well as width before touching anything.
"""

import math
from collections import Counter
from dataclasses import dataclass, field, fields
from enum import Enum, auto

import bmesh
import bpy
from mathutils import Vector


class SimplifyError(Exception):
    """A precondition failed. The message is written for the person running it."""


class WarningCode(Enum):
    NOTHING_TO_DISSOLVE = auto()
    KEEPS_EVERY_COLUMN = auto()
    LOW_GRID_CONFIDENCE = auto()
    SHARED_DATA = auto()
    LINKED_DATA = auto()


@dataclass(frozen=True)
class SimplifyWarning:
    code: WarningCode
    message: str
    blocking: bool = False


@dataclass(frozen=True)
class SimplifySettings:
    """Front-end agnostic inputs.

    Frozen for the same reason as ExportSettings: the plan carries the settings
    it was built from, so the two cannot drift apart.
    """

    # Profile turn below this many degrees counts as collinear, so the column is
    # redundant. 1.0 is deliberately tight -- a real corner in these ramps turns
    # through tens of degrees.
    angle_threshold_deg: float = 1.0

    # Explicit 0-based columns to keep, overriding angle detection entirely.
    # Empty means "detect". Use when the geometry disagrees with intent.
    keep_columns_override: tuple[int, ...] = ()

    # Refuse if the grid model explains less than this share of edges.
    min_grid_confidence: float = 0.90


@dataclass
class GridModel:
    """The structure recovered from the mesh, with its own evidence."""

    width: int
    rows: int
    shells: int
    verts: int
    edges: int
    confidence: float
    shell_stride: int

    def column_of(self, vertex_index: int) -> int:
        """Column for a vertex index.

        Valid across every shell only because shell_stride is a multiple of
        width -- checked in recover_grid(), not assumed here.
        """
        return vertex_index % self.width


@dataclass(kw_only=True)
class SimplifyAnalysis:
    """What simplifying a mesh would do, as plain values only.

    Holds no bpy references, so it outlives the mesh it was computed from. The
    export pipeline depends on that: it analyses a temporary evaluated mesh at
    plan time, frees it, and carries this forward to execution.
    """

    settings: SimplifySettings
    grid: GridModel
    keep_columns: tuple[int, ...]
    dissolve_columns: tuple[int, ...]
    column_angles_deg: tuple[float, ...]
    # Total, lengthwise plus end-cap rim. The rim share is reported separately
    # because it is checkable by hand in a dry run: 2 ends x dissolved columns.
    edges_to_dissolve: int
    rim_edges_to_dissolve: int
    predicted_verts: int
    warnings: list[SimplifyWarning] = field(default_factory=list)

    @property
    def blocking_warnings(self) -> list[SimplifyWarning]:
        return [w for w in self.warnings if w.blocking]


@dataclass(kw_only=True)
class SimplifyPlan(SimplifyAnalysis):
    """An analysis bound to the object it will edit in place.

    kw_only on both classes is what lets this add required fields after the
    base class's defaulted warnings field.
    """

    source: bpy.types.Object
    # source.data, narrowed to Mesh once in build_simplify_plan() so nothing
    # downstream touches the untyped Object.data union again.
    mesh: bpy.types.Mesh


@dataclass
class SimplifyResult:
    verts_before: int
    verts_after: int
    faces_before: int
    faces_after: int
    edges_dissolved: int


# -------------------------------------------------------------------- helpers

def _open_bmesh(mesh: bpy.types.Mesh) -> tuple[bmesh.types.BMesh, bool]:
    """A bmesh for the mesh, plus whether it is the live edit-mode one.

    The caller must not free a live edit bmesh; Blender owns it.
    """
    if bpy.context.mode == 'EDIT_MESH':
        return bmesh.from_edit_mesh(mesh), True
    bm = bmesh.new()
    bm.from_mesh(mesh)
    return bm, False


def recover_grid(bm: bmesh.types.BMesh, min_confidence: float) -> GridModel:
    """Recover the grid from index deltas, and check it actually reconciles.

    In a row-major grid every edge joins indices differing by 1 (across the
    profile), by W (along the length), or by the shell stride (the Solidify rim).
    The dominant non-1 delta is W.

    Rim edges count toward confidence once a second shell is found. Leaving them
    out made every solidified mesh read below the 95% warning line -- a warning
    that always fires is one nobody reads.
    """
    bm.verts.index_update()
    bm.edges.index_update()
    vert_count, edge_count = len(bm.verts), len(bm.edges)
    if edge_count == 0:
        raise SimplifyError("Mesh has no edges.")

    deltas = Counter(abs(e.verts[0].index - e.verts[1].index) for e in bm.edges)
    non_unit = [d for d, _ in deltas.most_common() if d != 1]
    if not non_unit:
        raise SimplifyError("Only delta-1 edges; this is not a row-major grid.")

    width = non_unit[0]
    if width < 3:
        raise SimplifyError(f"Recovered width {width} is too small to simplify.")

    # The next-largest delta is the shell stride if Solidify welded two shells.
    stride = non_unit[1] if len(non_unit) > 1 else vert_count
    shells = max(1, round(vert_count / stride)) if stride else 1

    explained = deltas.get(1, 0) + deltas.get(width, 0)
    if shells > 1:
        explained += deltas.get(stride, 0)
    confidence = explained / edge_count
    if confidence < min_confidence:
        raise SimplifyError(
            f"Grid model explains only {confidence:.1%} of edges (need "
            f"{min_confidence:.0%}). This mesh is not the regular grid the "
            f"column arithmetic assumes; refusing rather than guessing."
        )

    if shells > 1 and stride % width != 0:
        raise SimplifyError(
            f"Shell stride {stride} is not a multiple of width {width}, so a "
            f"single column formula cannot address both shells."
        )

    rows = (vert_count // shells) // width
    if rows * width * shells != vert_count:
        raise SimplifyError(
            f"{vert_count} verts is not {shells} shells x {rows} rows x "
            f"{width} columns; the grid does not reconcile."
        )

    return GridModel(
        width=width,
        rows=rows,
        shells=shells,
        verts=vert_count,
        edges=edge_count,
        confidence=confidence,
        shell_stride=stride,
    )


def profile_turn_angles(bm: bmesh.types.BMesh, grid: GridModel) -> tuple[float, ...]:
    """Degrees the profile turns through at each column of one cross-section.

    Boundary columns get 180.0 (treated as maximally significant) so they are
    never dissolved. Row 0 of shell 0 is the reference cross-section.
    """
    verts = list(bm.verts)
    section = [verts[i].co.copy() for i in range(grid.width)]

    angles = []
    for column in range(grid.width):
        if column in (0, grid.width - 1):
            angles.append(180.0)
            continue
        incoming: Vector = section[column] - section[column - 1]
        outgoing: Vector = section[column + 1] - section[column]
        if incoming.length == 0.0 or outgoing.length == 0.0:
            angles.append(0.0)
            continue
        angles.append(math.degrees(incoming.angle(outgoing, 0.0)))
    return tuple(angles)


# ----------------------------------------------------------------- plan/apply

def _analyze(bm: bmesh.types.BMesh, settings: SimplifySettings) -> SimplifyAnalysis:
    """The geometry half of planning: which columns go, and what that leaves.

    Reads bm only. Warnings here are about the geometry; whether the mesh may be
    edited in place is the caller's concern, since only the caller knows who
    owns it.
    """
    grid = recover_grid(bm, settings.min_grid_confidence)
    angles = profile_turn_angles(bm, grid)

    if settings.keep_columns_override:
        keep = tuple(sorted(set(settings.keep_columns_override)))
        out_of_range = [c for c in keep if not 0 <= c < grid.width]
        if out_of_range:
            raise SimplifyError(
                f"keep_columns_override {out_of_range} outside 0..{grid.width - 1}."
            )
    else:
        keep = tuple(
            c for c, angle in enumerate(angles)
            if angle >= settings.angle_threshold_deg
        )

    dissolve = tuple(c for c in range(grid.width) if c not in keep)

    # Every edge that will go, counted here so the dry run can report it
    # without touching anything.
    rim = _end_rim_edges(bm, grid, dissolve)
    doomed = _lengthwise_edges(bm, grid, dissolve) + rim

    warnings: list[SimplifyWarning] = []
    if not dissolve:
        warnings.append(SimplifyWarning(
            WarningCode.NOTHING_TO_DISSOLVE,
            f"Every column turns through at least "
            f"{settings.angle_threshold_deg} degrees, so none are redundant.",
            blocking=True,
        ))
    if len(keep) == grid.width:
        warnings.append(SimplifyWarning(
            WarningCode.KEEPS_EVERY_COLUMN,
            "Keep set covers the whole profile; nothing would change.",
            blocking=True,
        ))
    if grid.confidence < 0.95:
        warnings.append(SimplifyWarning(
            WarningCode.LOW_GRID_CONFIDENCE,
            f"Grid model explains {grid.confidence:.1%} of edges. Check the "
            f"result; anything below ~95% means unexpected topology.",
        ))

    # Every row, end rows included, keeps only the kept columns: with its
    # end-cap rim edge dissolved as well, an end-row vertex in a dissolved
    # column drops to 2 edges like any other, and use_verts removes it.
    #
    # Unverified for a single shell (no Solidify, so no rim): there the
    # end-row vertex sits on an open boundary and is assumed to dissolve at
    # 2 edges too. Compare this against the result on the first such run.
    predicted = grid.shells * grid.rows * len(keep)
    return SimplifyAnalysis(
        settings=settings,
        grid=grid,
        keep_columns=keep,
        dissolve_columns=dissolve,
        column_angles_deg=angles,
        edges_to_dissolve=len(doomed),
        rim_edges_to_dissolve=len(rim),
        predicted_verts=predicted,
        warnings=warnings,
    )


def analyze_mesh(mesh: bpy.types.Mesh, settings: SimplifySettings) -> SimplifyAnalysis:
    """Analyse any mesh without touching it, including a temporary one.

    For callers that do not have an active object to plan against -- the export
    pipeline analyses the curve's evaluated mesh before converting anything.
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        return _analyze(bm, settings)
    finally:
        bm.free()


def build_simplify_plan(
    context: bpy.types.Context, settings: SimplifySettings
) -> SimplifyPlan:
    """Work out which columns to dissolve on the active mesh. Changes nothing."""
    obj = context.active_object
    if obj is None:
        raise SimplifyError("No active object. Select the converted mesh.")
    # Exact class check: narrows Object.data for the type checker, which the type
    # string cannot, and is the one check this repo uses for every data type.
    mesh = obj.data
    if type(mesh) is not bpy.types.Mesh:
        raise SimplifyError(f"Active object {obj.name!r} is a {obj.type}, not a MESH.")

    bm, is_live = _open_bmesh(mesh)
    try:
        analysis = _analyze(bm, settings)
    finally:
        if not is_live:
            bm.free()

    # This edits the mesh in place, so anything else holding it would change
    # too. Blocking warnings rather than errors so a dry run still reports the
    # full analysis.
    ownership: list[SimplifyWarning] = []
    if not mesh.is_editable:
        ownership.append(SimplifyWarning(
            WarningCode.LINKED_DATA,
            f"Mesh {mesh.name!r} is linked from a library and cannot be "
            f"edited in this file.",
            blocking=True,
        ))
    # A fake user is a user count with no one behind it; it shares nothing.
    sharers = mesh.users - int(mesh.use_fake_user)
    if sharers > 1:
        ownership.append(SimplifyWarning(
            WarningCode.SHARED_DATA,
            f"Mesh {mesh.name!r} has {sharers} users, so dissolving it in place "
            f"would change every one of them. Make it single-user first "
            f"(Object > Relations > Make Single User).",
            blocking=True,
        ))

    # Field by field rather than dataclasses.asdict(), which would also turn the
    # nested GridModel into a dict.
    base = {f.name: getattr(analysis, f.name) for f in fields(SimplifyAnalysis)}
    base["warnings"] = [*analysis.warnings, *ownership]
    return SimplifyPlan(**base, source=obj, mesh=mesh)


def _lengthwise_edges(
    bm: bmesh.types.BMesh, grid: GridModel, columns: tuple[int, ...]
) -> list[bmesh.types.BMEdge]:
    """Edges running along the length within the given columns.

    Identified by an index delta of exactly the grid width, which in a row-major
    grid is precisely the lengthwise direction.
    """
    wanted = set(columns)
    found = []
    for edge in bm.edges:
        a, b = edge.verts[0].index, edge.verts[1].index
        if abs(a - b) != grid.width:
            continue
        if grid.column_of(min(a, b)) in wanted:
            found.append(edge)
    return found


def _end_rim_edges(
    bm: bmesh.types.BMesh, grid: GridModel, columns: tuple[int, ...]
) -> list[bmesh.types.BMEdge]:
    """Solidify rim edges capping each end of the ramp, within the given columns.

    A rim edge joins a vertex to its twin on the other shell, so its index delta
    is exactly the shell stride. Rim edges also run down both long sides, but
    those sit in the boundary columns -- excluded explicitly rather than trusted
    to the keep set, since keep_columns_override can leave a boundary column out
    and dissolving a side rim would open the solid along its whole length.
    """
    if grid.shells < 2:
        return []
    wanted = set(columns) - {0, grid.width - 1}
    found = []
    for edge in bm.edges:
        a, b = edge.verts[0].index, edge.verts[1].index
        if abs(a - b) != grid.shell_stride:
            continue
        if grid.column_of(min(a, b)) in wanted:
            found.append(edge)
    return found


def _apply(bm: bmesh.types.BMesh, expected: SimplifyAnalysis) -> SimplifyResult:
    """Dissolve on bm, after confirming it is still the mesh `expected` describes.

    Mutates bm only; writing it back is the caller's job. Re-analysed rather than
    trusted: the analysis may come from a different mesh than the one being
    edited -- the export plans on the evaluated mesh and executes on the
    converted one -- so the grid and dissolve set must match before anything goes.
    """
    if expected.blocking_warnings:
        raise SimplifyError("; ".join(w.message for w in expected.blocking_warnings))

    current = _analyze(bm, expected.settings)
    was = (expected.grid.width, expected.grid.rows, expected.grid.shells,
           expected.dissolve_columns)
    now = (current.grid.width, current.grid.rows, current.grid.shells,
           current.dissolve_columns)
    if now != was:
        raise SimplifyError(
            f"Mesh no longer matches the plan (width, rows, shells, dissolve: "
            f"{was} -> {now}). Re-plan before executing."
        )

    grid = current.grid
    verts_before, faces_before = len(bm.verts), len(bm.faces)
    doomed = (
        _lengthwise_edges(bm, grid, expected.dissolve_columns)
        + _end_rim_edges(bm, grid, expected.dissolve_columns)
    )

    # One call, after collecting every edge: indices shift as soon as anything
    # is removed, so a second pass would address the wrong geometry.
    bmesh.ops.dissolve_edges(bm, edges=doomed, use_verts=True)

    return SimplifyResult(
        verts_before=verts_before,
        verts_after=len(bm.verts),
        faces_before=faces_before,
        faces_after=len(bm.faces),
        edges_dissolved=len(doomed),
    )


def execute_simplify_plan(
    context: bpy.types.Context, plan: SimplifyPlan
) -> SimplifyResult:
    """Apply the plan to its mesh, in Edit or Object mode. Mutates the mesh."""
    mesh = plan.mesh
    bm, is_live = _open_bmesh(mesh)
    try:
        # Reopened rather than reusing the plan's bmesh: that one was opened for
        # reading and, outside edit mode, has already been freed.
        result = _apply(bm, plan)
        if is_live:
            bmesh.update_edit_mesh(mesh)
        else:
            bm.to_mesh(mesh)
            mesh.update()
        return result
    finally:
        if not is_live:
            bm.free()


def simplify_bmesh(bm: bmesh.types.BMesh, expected: SimplifyAnalysis) -> SimplifyResult:
    """Apply an analysis to a caller-owned bmesh. Mutates bm only.

    For predicting what follows a simplify without touching any mesh -- the
    export plans its UV layout on a simplified in-memory copy of the evaluated
    mesh. Same checks as the other mutating paths.
    """
    return _apply(bm, expected)


def simplify_mesh(mesh: bpy.types.Mesh, expected: SimplifyAnalysis) -> SimplifyResult:
    """Apply an analysis to a mesh outside Edit Mode. Mutates the mesh.

    For callers that own the mesh outright and have no plan bound to an active
    object -- the export pipeline runs this on its freshly converted duplicate.
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        result = _apply(bm, expected)
        bm.to_mesh(mesh)
        mesh.update()
        return result
    finally:
        bm.free()
