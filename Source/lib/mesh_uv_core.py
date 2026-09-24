"""
mesh_uv_core.py

Shared logic for laying out straight UV islands and marking seams on a
curve-converted, solidified mesh. No UI, no presentation -- same contract as
spline_export_core and mesh_simplify_core.

HOW TO RUN
    Not runnable on its own; import it. See cli/uv_layout_mesh.py.

WHAT IT DOES
    A converted, solidified ramp is a closed tube of quads: two shells (the
    inside and outside of the profile) joined by a rim, and each shell is a
    width x rows grid. This splits it into three islands and lays each out as an
    axis-aligned rectangle:

        inner  the shell with the shorter profile -- the inside of the U
        outer  the other shell, with V REVERSED relative to inner
        band   every rim face: both long rims plus both end caps, unrolled from
               one closed ring into one continuous strip

    Positions are computed from the grid, not unwrapped. U is cumulative distance
    across the profile, V cumulative distance along the length, each averaged
    over the island so every row is one straight horizontal line and every column
    one straight vertical line (what Follow Active Quads' Length Average does).

    Each island is scaled uniformly to fill fill_height of the UV height, and
    the islands sit side by side. Uniform per island, so a checker stays square
    within each one; texel density differs BETWEEN islands -- the band, about
    twice as long as a shell, gets about half the density along its length. A
    single shared scale was tried first (2026-09-24) and rejected: fitting the
    band's length left both shells too small to use. If the islands are too wide
    to fit side by side, U alone is compressed uniformly and width_fit reports
    by how much.

ORIENTATION
    V is fixed by intent. Inner runs row 0 (the curve's start) at the bottom.
    Outer runs the other way, which the shader relies on. The band starts at the
    row-0 end of column 0 and runs the same way as inner along that first rim,
    returning the other way along the far rim -- unavoidable for one loop.

    U is then chosen per island so no face is mirrored: a mirrored island has
    reversed winding and breaks the tangent basis for normal maps. The outer
    shell faces the opposite way from inner, so giving twin vertices the same UV
    would mirror it; reversing V is exactly what un-mirrors it. The mirrored-face
    count per island is measured, not assumed, and should read 0.

SEAMS
    Every edge between two islands, plus the one rim edge where the band ring is
    cut. Seams do not drive this layout -- the islands come from the grid -- but
    they make the result visible and keep a manual re-unwrap working. Existing
    seams on the mesh are replaced.

WHY IT VERIFIES THE GRID FIRST
    Reuses mesh_simplify_core.recover_grid(), which refuses unless the vertex
    order is a genuine row-major grid. Measured 2026-09-24: pipeline output
    after Simplify (InnerRearRamp.001, width 4) is 100% row-major, while a mesh
    modelled before these scripts (CentrifugeRamp.001) reads 61.7% and is refused.
    Such meshes need regenerating from their curve, not a topology walk here.

    recover_grid() also rejects width < 3, a Simplify rule. A flat ribbon
    simplified to its two boundary columns would therefore be refused here too.

STATUS
    Written 2026-09-24 against the measured topology of InnerRearRamp.001
    (776 verts, width 4 x 97 rows x 2 shells, 774 faces = 576 shell + 198 rim),
    and tested in Blender the same day through the UV Layout panel.
    Single-shell meshes (no Solidify, no band) are supported but have never
    been measured.

    Wired into spline_export_core 2026-09-24 as an optional step, and tested
    in Blender through Convert & Export the same day.
"""

import importlib
from dataclasses import dataclass, field, fields
from enum import Enum, auto

import bmesh
import bpy
from mathutils import Vector

# Grid recovery is shared with Simplify rather than duplicated. Reloaded for the
# same reason spline_export_core reloads it: front-ends reload only the module
# they import.
import mesh_simplify_core as simplify_core

importlib.reload(simplify_core)

GridModel = simplify_core.GridModel

INNER, OUTER, BAND = "inner", "outer", "band"
ISLAND_ORDER = (INNER, OUTER, BAND)

# Profiles within this fraction of each other cannot be told apart by length.
AMBIGUOUS_PROFILE_RATIO = 0.01


class UVError(Exception):
    """A precondition failed. The message is written for the person running it."""


class WarningCode(Enum):
    AMBIGUOUS_INNER = auto()
    MIRRORED_FACES = auto()
    SHARED_DATA = auto()
    LINKED_DATA = auto()


@dataclass(frozen=True)
class UVWarning:
    code: WarningCode
    message: str
    blocking: bool = False


@dataclass(frozen=True)
class UVSettings:
    """Front-end agnostic inputs. Frozen, like the other cores' settings."""

    # Refuse if the grid model explains less than this share of edges.
    min_grid_confidence: float = 0.90

    # UV-space gap left and right of, and between, islands.
    margin: float = 0.01

    # Share of the UV height each island fills, centred vertically.
    fill_height: float = 0.95


@dataclass(frozen=True)
class IslandLayout:
    """One island's measured size and where it lands in UV space."""

    name: str
    faces: int
    # Mesh units, before scaling.
    width: float
    height: float
    # UV units per mesh unit along V; U uses this times the analysis' width_fit.
    scale: float
    uv_min: tuple[float, float]
    uv_max: tuple[float, float]
    # True when U runs from the grid's last column (or the shell-1 side, for the
    # band) rather than the first, to keep the island unmirrored.
    u_reversed: bool
    # Measured on the computed UVs. Anything but 0 means inconsistent winding.
    mirrored_faces: int


@dataclass(kw_only=True)
class UVAnalysis:
    """What laying out this mesh would do, as plain values only."""

    settings: UVSettings
    grid: GridModel
    inner_shell: int
    profile_lengths: tuple[float, ...]
    islands: tuple[IslandLayout, ...]
    # Factor applied to every island's U so they fit side by side. 1.0 means no
    # compression; below 1.0 the islands are squeezed horizontally.
    width_fit: float
    seams_to_mark: int
    seams_existing: int
    # Existing active UV layer, or None if one will be created.
    uv_layer: str | None
    warnings: list[UVWarning] = field(default_factory=list)

    @property
    def blocking_warnings(self) -> list[UVWarning]:
        return [w for w in self.warnings if w.blocking]


@dataclass(kw_only=True)
class UVPlan(UVAnalysis):
    """An analysis bound to the object it will edit in place."""

    source: bpy.types.Object
    # source.data, narrowed to Mesh once in build_uv_plan().
    mesh: bpy.types.Mesh


@dataclass
class UVResult:
    uv_layer: str
    uv_layer_created: bool
    faces_written: int
    seams_marked: int
    seams_cleared: int


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


def _signed_area(uvs: list[tuple[float, float]]) -> float:
    """Positive when the UV polygon winds counter-clockwise, i.e. unmirrored."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(uvs, uvs[1:] + uvs[:1]):
        total += x0 * y1 - x1 * y0
    return 0.5 * total


def _band_stations(grid: GridModel) -> list[tuple[int, int]]:
    """(row, column) of each rim edge, in order round the closed ring.

    Starts at row 0 of column 0 and runs up that long rim first, so the band
    shares inner's direction there. 2R + 2W - 4 stations, one per rim edge.
    """
    r_last, c_last = grid.rows - 1, grid.width - 1
    return (
        [(r, 0) for r in range(grid.rows)]
        + [(r_last, c) for c in range(1, grid.width)]
        + [(r, c_last) for r in range(r_last - 1, -1, -1)]
        + [(0, c) for c in range(c_last - 1, 0, -1)]
    )


class _Layout:
    """The full per-face computation. Internal: holds bmesh-derived indices."""

    def __init__(self, bm: bmesh.types.BMesh, settings: UVSettings) -> None:
        if not 0.0 <= settings.margin < 0.1:
            raise UVError(f"margin {settings.margin} must be in [0, 0.1).")
        if not 0.0 < settings.fill_height <= 1.0:
            raise UVError(f"fill_height {settings.fill_height} must be in (0, 1].")

        self.grid = grid = simplify_core.recover_grid(bm, settings.min_grid_confidence)
        if grid.shells not in (1, 2):
            raise UVError(f"Expected 1 or 2 shells, recovered {grid.shells}.")
        bm.faces.index_update()

        self.co: list[Vector] = [v.co.copy() for v in bm.verts]
        W, R, S = grid.width, grid.rows, grid.shells

        stations = _band_stations(grid) if S == 2 else []
        expected = S * (R - 1) * (W - 1) + len(stations)
        if len(bm.faces) != expected:
            raise UVError(
                f"{len(bm.faces)} faces, but {S} shells x {R - 1} x {W - 1} grid "
                f"cells + {len(stations)} rim faces = {expected}. The mesh is not "
                f"the closed tube this layout assumes."
            )

        # Per-shell axes: cumulative averaged distance across and along.
        self.u_axis: list[list[float]] = []
        self.v_axis: list[list[float]] = []
        for shell in range(S):
            self.u_axis.append(self._cumulative(
                [[self._p(shell, r, c) for c in range(W)] for r in range(R)]))
            self.v_axis.append(self._cumulative(
                [[self._p(shell, r, c) for r in range(R)] for c in range(W)]))
        self.profile_lengths = tuple(u[-1] for u in self.u_axis)
        self.inner_shell = min(range(S), key=lambda s: self.profile_lengths[s])

        # Band: V along the ring, U across the wall thickness.
        self.station_of = {rc: k for k, rc in enumerate(stations)}
        self.band_v: list[float] = [0.0]
        self.band_width = 0.0
        if stations:
            ring = stations + stations[:1]
            for a, b in zip(ring, ring[1:]):
                step = sum((self._p(s, *b) - self._p(s, *a)).length for s in (0, 1)) / 2
                self.band_v.append(self.band_v[-1] + step)
            self.band_width = sum(
                (self._p(1, *rc) - self._p(0, *rc)).length for rc in stations
            ) / len(stations)

        self.face_island: dict[int, str] = {}
        raw: dict[int, list[tuple[float, float]]] = {}
        for face in bm.faces:
            island, uvs = self._classify(face)
            self.face_island[face.index] = island
            raw[face.index] = uvs

        # Orient, then measure, each island.
        sizes = {
            INNER: (self.profile_lengths[self.inner_shell],
                    self.v_axis[self.inner_shell][-1]),
            OUTER: (self.profile_lengths[1 - self.inner_shell],
                    self.v_axis[1 - self.inner_shell][-1]) if S == 2 else (0.0, 0.0),
            BAND: (self.band_width, self.band_v[-1]),
        }
        present = [n for n in ISLAND_ORDER if any(i == n for i in self.face_island.values())]
        self.u_reversed: dict[str, bool] = {}
        for name in present:
            faces = [f for f, i in self.face_island.items() if i == name]
            width = sizes[name][0]
            if sum(_signed_area(raw[f]) for f in faces) < 0.0:
                self.u_reversed[name] = True
                for f in faces:
                    raw[f] = [(width - u, v) for u, v in raw[f]]
            else:
                self.u_reversed[name] = False

        # Each island fills fill_height, centred; left to right; U compressed
        # only if the set would not otherwise fit.
        if any(sizes[n][0] <= 0.0 or sizes[n][1] <= 0.0 for n in present):
            raise UVError("Degenerate mesh: an island has zero size.")
        m = settings.margin
        y0 = (1.0 - settings.fill_height) / 2.0
        scales = {n: settings.fill_height / sizes[n][1] for n in present}
        natural_w = sum(sizes[n][0] * scales[n] for n in present)
        self.width_fit = min(1.0, (1.0 - m * (len(present) + 1)) / natural_w)

        self.face_uvs: dict[int, list[tuple[float, float]]] = {}
        islands: list[IslandLayout] = []
        x = m
        for name in present:
            w, h = sizes[name]
            su, sv = scales[name] * self.width_fit, scales[name]
            faces = [f for f, i in self.face_island.items() if i == name]
            for f in faces:
                self.face_uvs[f] = [(x + u * su, y0 + v * sv) for u, v in raw[f]]
            islands.append(IslandLayout(
                name=name,
                faces=len(faces),
                width=w,
                height=h,
                scale=sv,
                uv_min=(x, y0),
                uv_max=(x + w * su, y0 + h * sv),
                u_reversed=self.u_reversed[name],
                mirrored_faces=sum(1 for f in faces
                                   if _signed_area(self.face_uvs[f]) <= 0.0),
            ))
            x += w * su + m
        self.islands = tuple(islands)

        # Seams: island boundaries, plus the cut that opens the band ring.
        cut = {0, grid.shell_stride} if S == 2 else set()
        self.seam_edges: set[int] = set()
        self.seams_existing = 0
        bm.edges.index_update()
        for edge in bm.edges:
            if edge.seam:
                self.seams_existing += 1
            owners = {self.face_island[f.index] for f in edge.link_faces}
            ends = {v.index for v in edge.verts}
            if len(owners) > 1 or ends == cut:
                self.seam_edges.add(edge.index)

    # ---------------------------------------------------------------- internals

    def _p(self, shell: int, row: int, col: int) -> Vector:
        g = self.grid
        return self.co[shell * g.shell_stride + row * g.width + col]

    @staticmethod
    def _cumulative(lines: list[list[Vector]]) -> list[float]:
        """Cumulative distance along parallel polylines, averaged across them."""
        out = [0.0]
        for i in range(len(lines[0]) - 1):
            step = sum((line[i + 1] - line[i]).length for line in lines) / len(lines)
            out.append(out[-1] + step)
        return out

    def _locate(self, index: int) -> tuple[int, int, int]:
        g = self.grid
        shell, rest = divmod(index, g.shell_stride) if g.shells > 1 else (0, index)
        row, col = divmod(rest, g.width)
        return shell, row, col

    def _classify(self, face: bmesh.types.BMFace) -> tuple[str, list[tuple[float, float]]]:
        """Which island a face belongs to, and its raw UVs in loop order."""
        where = [self._locate(loop.vert.index) for loop in face.loops]
        shells = {s for s, _, _ in where}
        cells = {(r, c) for _, r, c in where}

        if len(where) == 4 and len(shells) == 1:
            rows = {r for r, _ in cells}
            cols = {c for _, c in cells}
            if len(cells) == 4 and max(rows) - min(rows) == 1 and max(cols) - min(cols) == 1:
                shell = shells.pop()
                u, v = self.u_axis[shell], self.v_axis[shell]
                if shell == self.inner_shell:
                    return INNER, [(u[c], v[r]) for _, r, c in where]
                # Reversed along the length: the shader depends on this.
                return OUTER, [(u[c], v[-1] - v[r]) for _, r, c in where]

        if len(where) == 4 and shells == {0, 1} and len(cells) == 2:
            ks = sorted(self.station_of.get(rc, -1) for rc in cells)
            last = len(self.station_of) - 1
            if ks == [0, last]:
                # The face that closes the ring: its station-0 side sits at the
                # far end of the strip, which is what makes the cut a seam.
                ks = [last, last + 1]
            if ks[0] >= 0 and ks[1] - ks[0] == 1:
                def station(rc: tuple[int, int]) -> int:
                    k = self.station_of[rc]
                    return last + 1 if k == 0 and ks[0] == last else k
                return BAND, [(0.0 if s == 0 else self.band_width,
                               self.band_v[station((r, c))])
                              for s, r, c in where]

        raise UVError(
            f"Face {face.index} (vertices at shell/row/col {where}) is neither a "
            f"grid cell nor a rim face. The mesh is not the tube this assumes."
        )


# ----------------------------------------------------------------- plan/apply

def _analysis_from(layout: _Layout, bm: bmesh.types.BMesh,
                   settings: UVSettings) -> UVAnalysis:
    warnings: list[UVWarning] = []
    lengths = layout.profile_lengths
    if len(lengths) == 2 and abs(lengths[0] - lengths[1]) <= AMBIGUOUS_PROFILE_RATIO * max(lengths):
        warnings.append(UVWarning(
            WarningCode.AMBIGUOUS_INNER,
            f"Shell profiles are {lengths[0]:.4f} and {lengths[1]:.4f} long -- too "
            f"close to tell inner from outer. Shell {layout.inner_shell} was picked.",
        ))
    mirrored = {i.name: i.mirrored_faces for i in layout.islands if i.mirrored_faces}
    if mirrored:
        warnings.append(UVWarning(
            WarningCode.MIRRORED_FACES,
            f"Mirrored faces per island {mirrored}: face winding is inconsistent. "
            f"Recalculate normals before laying out UVs.",
        ))

    active = bm.loops.layers.uv.active
    return UVAnalysis(
        settings=settings,
        grid=layout.grid,
        inner_shell=layout.inner_shell,
        profile_lengths=lengths,
        islands=layout.islands,
        width_fit=layout.width_fit,
        seams_to_mark=len(layout.seam_edges),
        seams_existing=layout.seams_existing,
        uv_layer=active.name if active is not None else None,
        warnings=warnings,
    )


def analyze_bmesh(bm: bmesh.types.BMesh, settings: UVSettings) -> UVAnalysis:
    """Analyse a caller-owned bmesh without touching its geometry.

    The export uses this on a simplified in-memory copy of the evaluated mesh,
    so its plan describes the mesh the UVs will actually be laid out on.
    """
    return _analysis_from(_Layout(bm, settings), bm, settings)


def analyze_mesh(mesh: bpy.types.Mesh, settings: UVSettings) -> UVAnalysis:
    """Analyse any mesh without touching it."""
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        return analyze_bmesh(bm, settings)
    finally:
        bm.free()


def build_uv_plan(context: bpy.types.Context, settings: UVSettings) -> UVPlan:
    """Work out the layout for the active mesh. Changes nothing."""
    obj = context.active_object
    if obj is None:
        raise UVError("No active object. Select the converted mesh.")
    mesh = obj.data
    if type(mesh) is not bpy.types.Mesh:
        raise UVError(f"Active object {obj.name!r} is a {obj.type}, not a MESH.")

    bm, is_live = _open_bmesh(mesh)
    try:
        analysis = _analysis_from(_Layout(bm, settings), bm, settings)
    finally:
        if not is_live:
            bm.free()

    # Same ownership rules as Simplify: this edits the mesh in place.
    ownership: list[UVWarning] = []
    if not mesh.is_editable:
        ownership.append(UVWarning(
            WarningCode.LINKED_DATA,
            f"Mesh {mesh.name!r} is linked from a library and cannot be edited "
            f"in this file.",
            blocking=True,
        ))
    sharers = mesh.users - int(mesh.use_fake_user)
    if sharers > 1:
        ownership.append(UVWarning(
            WarningCode.SHARED_DATA,
            f"Mesh {mesh.name!r} has {sharers} users, so changing its UVs in place "
            f"would change every one of them. Make it single-user first "
            f"(Object > Relations > Make Single User).",
            blocking=True,
        ))

    base = {f.name: getattr(analysis, f.name) for f in fields(UVAnalysis)}
    base["warnings"] = [*analysis.warnings, *ownership]
    return UVPlan(**base, source=obj, mesh=mesh)


def _apply(bm: bmesh.types.BMesh, expected: UVAnalysis) -> UVResult:
    """Write UVs and seams to bm, after confirming it still matches `expected`."""
    if expected.blocking_warnings:
        raise UVError("; ".join(w.message for w in expected.blocking_warnings))

    layout = _Layout(bm, expected.settings)
    was = (expected.grid.width, expected.grid.rows, expected.grid.shells,
           expected.inner_shell, tuple(i.faces for i in expected.islands))
    now = (layout.grid.width, layout.grid.rows, layout.grid.shells,
           layout.inner_shell, tuple(i.faces for i in layout.islands))
    if now != was:
        raise UVError(
            f"Mesh no longer matches the plan (width, rows, shells, inner shell, "
            f"island faces: {was} -> {now}). Re-plan before executing."
        )

    uv_layer = bm.loops.layers.uv.active
    created = uv_layer is None
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.new("UVMap")

    for face in bm.faces:
        for loop, uv in zip(face.loops, layout.face_uvs[face.index]):
            loop[uv_layer].uv = uv

    cleared = 0
    for edge in bm.edges:
        want = edge.index in layout.seam_edges
        if edge.seam and not want:
            cleared += 1
        edge.seam = want

    return UVResult(
        uv_layer=uv_layer.name,
        uv_layer_created=created,
        faces_written=len(layout.face_uvs),
        seams_marked=len(layout.seam_edges),
        seams_cleared=cleared,
    )


def execute_uv_plan(context: bpy.types.Context, plan: UVPlan) -> UVResult:
    """Apply the plan to its mesh, in Edit or Object mode. Mutates the mesh."""
    mesh = plan.mesh
    bm, is_live = _open_bmesh(mesh)
    try:
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


def layout_mesh(mesh: bpy.types.Mesh, expected: UVAnalysis) -> UVResult:
    """Apply an analysis to a mesh outside Edit Mode. Mutates the mesh.

    For callers that own the mesh outright -- the export pipeline runs this on
    its converted duplicate.
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
