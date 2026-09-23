"""
analyze_mesh_topology.py

Diagnostic, read-only. Characterises the active mesh so Simplify can be designed
against measured structure rather than assumptions about how curve conversion
orders its vertices.

HOW TO RUN
    Blender Text Editor -> Run Script, with the generated mesh as the active
    object. Works in both Object and Edit mode.

WHY THAT CONTEXT
    Reads bpy.context.active_object.

    Read-only. In Object mode it builds a throwaway bmesh and frees it; in Edit
    mode it reads the live edit bmesh without writing back or freeing it. Nothing
    is modified either way, so this is safe to run at any time.

WHAT IT ANSWERS
    The index-delta histogram is the point of this script. In a row-major grid of
    width W, every edge joins indices differing by exactly 1 (across the profile)
    or exactly W (along the length) -- plus, with Solidify, the shell stride
    (rim edges joining each vertex to its twin on the other shell). If those
    spikes dominate the histogram, the vertex layout is predictable and Simplify
    can pick columns arithmetically -- column = index % W -- instead of walking
    edge loops topologically.

    If it does NOT spike, the layout is irregular and Simplify has to walk the
    topology instead. Either is workable; they are very different amounts of code,
    which is why this runs first.

WHERE THE OUTPUT GOES
    System console and a Text datablock named by REPORT_TEXT_NAME. See
    spline_to_unreal_mesh.py for why the datablock is the channel that works.

NOTE
    The report emitter here duplicates the one in spline_to_unreal_mesh.py. If
    this script becomes permanent, that belongs in Source/lib as shared CLI
    presentation -- separate from spline_export_core, which stays free of it.
"""

from collections import Counter
from datetime import datetime

import bmesh
import bpy

REPORT_TEXT_NAME = "mesh_topology_report.txt"
RULE = "=" * 71

# How many rows of the delta histogram to print.
TOP_DELTAS = 12

# How many leading vertices to dump. If the row-major theory holds, the first W
# of these are one complete cross-section of the bevel profile.
PROFILE_PREVIEW = 16


def count_shells(bm: bmesh.types.BMesh) -> int:
    """Connected components, by flood fill over edges.

    Solidify produces two shells unless its rim welds them into one, so this
    distinguishes "two loose surfaces" from "one closed solid".
    """
    seen: set[int] = set()
    shells = 0
    for start in bm.verts:
        if start.index in seen:
            continue
        shells += 1
        stack = [start]
        while stack:
            current = stack.pop()
            if current.index in seen:
                continue
            seen.add(current.index)
            for edge in current.link_edges:
                for other in edge.verts:
                    if other.index not in seen:
                        stack.append(other)
    return shells


def analyze(bm: bmesh.types.BMesh) -> list[str]:
    bm.verts.index_update()
    bm.edges.index_update()
    verts = list(bm.verts)
    edge_count = len(bm.edges)

    lines = [
        f"  verts / edges / faces : {len(verts)} / {edge_count} / {len(bm.faces)}",
        f"  boundary edges        : {sum(1 for e in bm.edges if e.is_boundary)}",
        f"  connected shells      : {count_shells(bm)}",
    ]

    valence = Counter(len(v.link_edges) for v in verts)
    lines.append(f"  vertex valence        : {dict(sorted(valence.items()))}")

    sides = Counter(len(f.verts) for f in bm.faces)
    lines.append(f"  face sides            : {dict(sorted(sides.items()))}")

    if edge_count == 0:
        lines.append("  (no edges; nothing further to infer)")
        return lines

    deltas = Counter(abs(e.verts[0].index - e.verts[1].index) for e in bm.edges)
    lines.append("")
    lines.append(f"  index-delta histogram, top {TOP_DELTAS}:")
    for delta, count in deltas.most_common(TOP_DELTAS):
        share = 100.0 * count / edge_count
        bar = "#" * min(40, int(share / 2))
        lines.append(f"      delta {delta:<7} x{count:<7} {share:5.1f}%  {bar}")

    # The widest-support delta that is not 1 is the grid width if the row-major
    # theory holds. Reported with its coverage so the theory is falsifiable
    # rather than assumed.
    others = [d for d, _ in deltas.most_common() if d != 1]
    if others:
        width = others[0]
        families = [1, width]
        lines.append("")
        lines.append(f"  grid-width hypothesis : {width}")

        # The next delta is the shell stride if Solidify made two shells. Counted
        # only when it is consistent with that -- a multiple of the width that
        # splits the vertices into whole shells -- so a stray delta cannot pad
        # the coverage. Same rule as mesh_simplify_core.recover_grid().
        if len(others) > 1:
            stride = others[1]
            shells = len(verts) // stride
            if shells > 1 and shells * stride == len(verts) and stride % width == 0:
                families.append(stride)
                lines.append(f"  shell hypothesis      : {shells} shells, rim stride {stride}")

        explained = sum(deltas.get(d, 0) for d in families)
        share = 100.0 * explained / edge_count
        lines.append(f"  edges explained by deltas {families} : "
                     f"{explained}/{edge_count}  ({share:.1f}%)")
        if share > 90.0:
            lines.append("  -> row-major layout looks SOLID; column = index % width")
        elif share > 60.0:
            lines.append("  -> partially regular; check the leftover deltas above")
        else:
            lines.append("  -> NOT row-major; Simplify must walk topology instead")

    lines.append("")
    lines.append(f"  first {PROFILE_PREVIEW} vertices, local space "
                 f"(one cross-section if the theory holds):")
    for index, vert in enumerate(verts[:PROFILE_PREVIEW]):
        co = vert.co
        lines.append(f"      [{index:3}]  "
                     f"({co.x:10.4f}, {co.y:10.4f}, {co.z:10.4f})  "
                     f"valence {len(vert.link_edges)}")

    return lines


def emit(report: str) -> None:
    print(report)
    text = bpy.data.texts.get(REPORT_TEXT_NAME) or bpy.data.texts.new(REPORT_TEXT_NAME)
    text.from_string(report)


def main() -> list[str]:
    obj = bpy.context.active_object
    if obj is None:
        return ["  ABORTED: no active object."]
    # Narrows Object.data -- a union of every data type -- which the type string
    # cannot. Exact class check, as everywhere in this repo: it also rejects
    # subclasses, so no one has to know which data types have them.
    mesh = obj.data
    if type(mesh) is not bpy.types.Mesh:
        return [f"  ABORTED: active object {obj.name!r} is a {obj.type}, not a MESH."]

    header = [
        f"  object   : {obj.name!r}  (data {mesh.name!r})",
        f"  mode     : {bpy.context.mode}",
        "",
    ]

    if bpy.context.mode == 'EDIT_MESH':
        # Live edit bmesh: read only, and never freed -- Blender owns it.
        return header + analyze(bmesh.from_edit_mesh(mesh))

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        return header + analyze(bm)
    finally:
        bm.free()


if __name__ == "__main__":
    try:
        _body = main()
    except Exception:
        import traceback
        _body = ["  ABORTED", "", traceback.format_exc()]

    emit("\n".join([
        RULE,
        f"analyze_mesh_topology  {datetime.now():%Y-%m-%d %H:%M:%S}",
        RULE,
        *_body,
        RULE,
    ]))
