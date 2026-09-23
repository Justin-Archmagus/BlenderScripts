"""
analyze_curve_bevel.py

Diagnostic, read-only. Reports where a curve's generated mesh density actually
comes from, and measures what it produces, so the right knob can be found without
guessing.

HOW TO RUN
    Blender Text Editor -> Run Script, with the source CURVE as the active
    object. Object mode.

WHY THAT CONTEXT
    Reads bpy.context.active_object.

    Read-only. to_mesh() on an evaluated object returns temporary data freed
    again by to_mesh_clear(); nothing in the scene is modified. In particular it
    does NOT call CurveProfile.initialize(), which would rewrite the profile.

THE THING THIS EXISTS TO ANSWER
    Curve mesh density has two independent axes living in different places:

      ALONG the curve  -> Curve.resolution_u, or Spline.resolution_u per spline
      ACROSS the curve -> depends on bevel_mode:
            ROUND   -> Curve.bevel_resolution, segments per QUARTER circle, so
                       the result is always a multiple of 4
            OBJECT  -> the BEVEL OBJECT's own splines, not a property of this
                       curve at all
            PROFILE -> Curve.bevel_profile, a readonly CurveProfile. Its sampled
                       count is set by initialize(totsegments); the control point
                       count and the two sampling flags also shape the result

    The report prints every candidate source, then the width actually measured
    off the evaluated mesh. Where they disagree, the measurement wins.
"""

from collections import Counter
from datetime import datetime

import bpy

REPORT_TEXT_NAME = "curve_bevel_report.txt"
RULE = "=" * 71


def spline_point_count(spline: bpy.types.Spline) -> tuple[int, str]:
    """Generated point count for one spline, with the arithmetic shown.

    resolution_u is subdivisions PER SEGMENT, so an open Bezier with N control
    points has N-1 segments and yields (N-1) * resolution_u + 1 points.

    Corrected 2026-09-22 against measurement: the earlier (resolution_u + 1) form
    predicted 105 rows where the evaluated mesh had 97. resolution_u is the count
    of points contributed per segment, not the number of subdivisions between
    them.
    """
    resolution = spline.resolution_u
    controls = (
        len(spline.bezier_points) if spline.type == 'BEZIER' else len(spline.points)
    )
    if spline.type == 'POLY':
        return controls, f"POLY, {controls} points, resolution ignored"

    segments = controls if spline.use_cyclic_u else max(0, controls - 1)
    tail = 0 if spline.use_cyclic_u else 1
    generated = segments * resolution + tail
    shape = "cyclic" if spline.use_cyclic_u else "open"
    return generated, (
        f"{spline.type}, {controls} control points, {shape}, "
        f"resolution_u={resolution} -> {segments} x {resolution}"
        f"{' + 1' if tail else ''} = {generated}"
    )


def describe_profile_source(curve: bpy.types.Curve) -> list[str]:
    """Every candidate source of ACROSS-curve density, for this bevel_mode."""
    mode = curve.bevel_mode
    lines = [f"  bevel_mode            : {mode}"]

    if mode == 'ROUND':
        predicted = 4 * (curve.bevel_resolution + 1)
        return lines + [
            f"  bevel_depth           : {curve.bevel_depth}",
            f"  bevel_resolution      : {curve.bevel_resolution}  "
            f"(segments per QUARTER circle)",
            f"  -> predicted profile  : {predicted} points (always a multiple of 4)",
            "  KNOB: Curve.bevel_resolution on this curve. 0 gives a 4-point diamond.",
        ]

    if mode == 'OBJECT':
        bevel_object = curve.bevel_object
        if bevel_object is None:
            return lines + ["  bevel_object          : None (no bevel geometry)"]
        lines.append(f"  bevel_object          : {bevel_object.name!r} "
                     f"({bevel_object.type})")
        if bevel_object.type != 'CURVE':
            return lines + ["  -> bevel object is not a CURVE; cannot predict width"]

        bevel_curve: bpy.types.Curve = bevel_object.data
        total = 0
        for index, spline in enumerate(bevel_curve.splines):
            count, why = spline_point_count(spline)
            total += count
            lines.append(f"      spline[{index}] : {why}")
        return lines + [
            f"  -> predicted profile  : {total} points",
            f"  KNOB: NOT on this curve. Edit {bevel_object.name!r} -- lower its",
            "        spline resolution_u, or remove control points.",
        ]

    if mode == 'PROFILE':
        profile = curve.bevel_profile
        if profile is None:
            return lines + ["  bevel_profile         : None"]

        control_points = len(profile.points)
        sampled = len(profile.segments)
        lines += [
            f"  preset                : {profile.preset}",
            f"  points (control)      : {control_points}",
            f"  segments (sampled)    : {sampled}   <-- readonly result",
            f"  use_sample_straight_edges : {profile.use_sample_straight_edges}"
            f"   (sample edges with vector handles)",
            f"  use_sample_even_lengths   : {profile.use_sample_even_lengths}",
            f"  bevel_resolution      : {curve.bevel_resolution}"
            f"   (listed to test whether it correlates; for ROUND it would imply "
            f"{4 * (curve.bevel_resolution + 1)})",
            "",
            "  Control point locations:",
        ]
        for index, point in enumerate(profile.points):
            lines.append(f"      [{index:2}]  ({point.location[0]:8.4f}, "
                         f"{point.location[1]:8.4f})")
        lines += [
            "",
            "  KNOB candidates, in the order worth trying:",
            "    1. CurveProfile.initialize(totsegments) -- sets the sampled count",
            "       directly. MUTATES the profile, so it is not called here.",
            "    2. Remove control points; the sampled curve follows them.",
            "    3. use_sample_straight_edges=True, which samples vector-handle",
            "       edges without subdividing them.",
            "  Compare 'segments (sampled)' against the measured width below: if",
            "  they match, the profile sampling is the source and knob 1 applies.",
        ]
        return lines

    return lines + [f"  (unhandled bevel_mode {mode!r})"]


def measure_generated_width(obj: bpy.types.Object) -> list[str]:
    """Actual profile width, read off the evaluated mesh.

    Same index-delta trick as analyze_mesh_topology: in a row-major grid the
    dominant non-1 delta is the row stride, which is the profile width.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        verts, edges, faces = len(mesh.vertices), len(mesh.edges), len(mesh.polygons)
        if edges == 0:
            return [
                f"  evaluated mesh        : {verts} verts, no edges",
                "  -> no surface generated; the curve has no bevel or extrude",
            ]

        deltas = Counter(abs(e.vertices[0] - e.vertices[1]) for e in mesh.edges)
        lines = [f"  evaluated mesh        : {verts} verts, {edges} edges, {faces} faces"]
        others = [d for d, _ in deltas.most_common() if d != 1]
        if not others:
            return lines + ["  -> only delta-1 edges; not a grid"]

        width = others[0]
        explained = deltas.get(1, 0) + deltas.get(width, 0)
        share = 100.0 * explained / edges
        return lines + [
            f"  measured width        : {width}",
            f"  grid fit              : deltas {{1, {width}}} cover "
            f"{explained}/{edges} edges ({share:.1f}%)",
            f"  top deltas            : {deltas.most_common(6)}",
        ]
    finally:
        evaluated.to_mesh_clear()


def main() -> list[str]:
    obj = bpy.context.active_object
    if obj is None:
        return ["  ABORTED: no active object."]
    if obj.type != 'CURVE':
        return [f"  ABORTED: active object {obj.name!r} is a {obj.type}, not a CURVE."]

    curve: bpy.types.Curve = obj.data
    lines = [
        f"  object                : {obj.name!r}  (data {curve.name!r})",
        f"  modifiers             : {[m.name for m in obj.modifiers] or 'none'}",
        "",
        "  --- ALONG the curve ---",
        f"  resolution_u          : {curve.resolution_u}  (object-level default)",
        f"  render_resolution_u   : {curve.render_resolution_u}",
        f"  extrude               : {curve.extrude}",
        f"  use_fill_caps         : {curve.use_fill_caps}",
    ]
    for index, spline in enumerate(curve.splines):
        _, why = spline_point_count(spline)
        lines.append(f"      spline[{index}] : {why}")

    lines += ["", "  --- ACROSS the curve (the profile) ---"]
    lines += describe_profile_source(curve)
    lines += ["", "  --- measured ---"]
    lines += measure_generated_width(obj)
    return lines


def emit(report: str) -> None:
    print(report)
    text = bpy.data.texts.get(REPORT_TEXT_NAME) or bpy.data.texts.new(REPORT_TEXT_NAME)
    text.from_string(report)


if __name__ == "__main__":
    try:
        _body = main()
    except Exception:
        import traceback
        _body = ["  ABORTED", "", traceback.format_exc()]

    emit("\n".join([
        RULE,
        f"analyze_curve_bevel  {datetime.now():%Y-%m-%d %H:%M:%S}",
        RULE,
        *_body,
        RULE,
    ]))
