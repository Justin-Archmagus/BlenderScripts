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
                       the result is always a multiple of 4 (unverified here)
            OBJECT  -> the BEVEL OBJECT's own splines, not a property of this
                       curve at all
            PROFILE -> Curve.bevel_resolution again, NOT the CurveProfile's own
                       sampled segment count. Measured 2026-09-22 on this
                       project's 5-point STEPS profile: width = 2 x
                       bevel_resolution + 3, while profile.segments read 3.

    Every mode yields a predicted width, and the report checks it against the
    width measured off the evaluated mesh. The measurement wins; a mismatch
    means the prediction formula is wrong for that curve, and says so.
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


def describe_profile_source(curve: bpy.types.Curve) -> tuple[list[str], int | None]:
    """Report lines for the ACROSS-curve density source, plus the width it
    predicts -- or None when this mode gives nothing to predict from."""
    mode = curve.bevel_mode
    lines = [f"  bevel_mode            : {mode}"]

    if mode == 'ROUND':
        predicted = 4 * (curve.bevel_resolution + 1)
        return lines + [
            f"  bevel_depth           : {curve.bevel_depth}",
            f"  bevel_resolution      : {curve.bevel_resolution}  "
            f"(segments per QUARTER circle)",
            f"  -> predicted width    : 4 x ({curve.bevel_resolution} + 1) = "
            f"{predicted}  (formula not yet measured on this project)",
            "  KNOB: Curve.bevel_resolution on this curve. 0 gives a 4-point diamond.",
        ], predicted

    if mode == 'OBJECT':
        bevel_object = curve.bevel_object
        if bevel_object is None:
            return lines + ["  bevel_object          : None (no bevel geometry)"], None
        lines.append(f"  bevel_object          : {bevel_object.name!r} "
                     f"({bevel_object.type})")
        bevel_curve = bevel_object.data
        if type(bevel_curve) is not bpy.types.Curve:
            return lines + ["  -> bevel object is not a CURVE; cannot predict width"], None

        total = 0
        for index, spline in enumerate(bevel_curve.splines):
            count, why = spline_point_count(spline)
            total += count
            lines.append(f"      spline[{index}] : {why}")
        return lines + [
            f"  -> predicted width    : {total}",
            f"  KNOB: NOT on this curve. Edit {bevel_object.name!r} -- lower its",
            "        spline resolution_u, or remove control points.",
        ], total

    if mode == 'PROFILE':
        resolution = curve.bevel_resolution
        predicted = 2 * resolution + 3
        lines += [
            f"  bevel_resolution      : {resolution}",
            f"  -> predicted width    : 2 x {resolution} + 3 = {predicted}",
            "     Empirical, from this project's 5-point STEPS profile. Always odd,",
            "     so an even width is unreachable through this knob.",
            "  KNOB: Curve.bevel_resolution on this curve.",
        ]

        profile = curve.bevel_profile
        if profile is None:
            return lines + ["  bevel_profile         : None"], predicted

        # Shape context only. On the measured profile, segments read 3 while the
        # width was 7, so the sampled count is not what sets the width.
        lines += [
            "",
            "  Profile shape (context; did not set the width when measured):",
            f"  preset                : {profile.preset}",
            f"  points (control)      : {len(profile.points)}",
            f"  segments (sampled)    : {len(profile.segments)}",
            f"  use_sample_straight_edges : {profile.use_sample_straight_edges}",
            f"  use_sample_even_lengths   : {profile.use_sample_even_lengths}",
            "  Control point locations:",
        ]
        for index, point in enumerate(profile.points):
            lines.append(f"      [{index:2}]  ({point.location[0]:8.4f}, "
                         f"{point.location[1]:8.4f})")
        return lines, predicted

    return lines + [f"  (unhandled bevel_mode {mode!r})"], None


def measure_generated_width(obj: bpy.types.Object) -> tuple[list[str], int | None]:
    """Actual profile width, read off the evaluated mesh, plus the report lines.

    Same index-delta trick as mesh_simplify_core.recover_grid(): in a row-major
    grid the dominant non-1 delta is the row stride, which is the profile width.
    With Solidify, the next delta is the shell stride -- rim edges joining each
    vertex to its twin on the other shell. Those count toward the fit; leaving
    them out made every solidified mesh read ~92% when it was a perfect grid.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        # None for object types with no geometry. A curve always yields a mesh,
        # even an empty one, so this is a guard rather than an expected path.
        if mesh is None:
            return ["  evaluated mesh        : none -- object produced no geometry"], None
        verts, edges, faces = len(mesh.vertices), len(mesh.edges), len(mesh.polygons)
        if edges == 0:
            return [
                f"  evaluated mesh        : {verts} verts, no edges",
                "  -> no surface generated; the curve has no bevel or extrude",
            ], None
        # The stubs' bpy_prop_array defines no __getitem__, though indexing works
        # at runtime. Suppressed on this line only, so the stub gap stays visible.
        deltas = Counter(abs(e.vertices[0] - e.vertices[1]) for e in mesh.edges)  # pyright: ignore[reportIndexIssue]
        lines = [f"  evaluated mesh        : {verts} verts, {edges} edges, {faces} faces"]
        others = [d for d, _ in deltas.most_common() if d != 1]
        if not others:
            return lines + ["  -> only delta-1 edges; not a grid"], None

        width = others[0]
        families = [1, width]
        shells_line = "  shells                : 1"
        if len(others) > 1:
            stride = others[1]
            # Counted only if it splits the verts into whole shells and is a
            # multiple of the width, so a stray delta cannot pad the fit.
            shells = verts // stride
            if shells > 1 and shells * stride == verts and stride % width == 0:
                families.append(stride)
                shells_line = f"  shells                : {shells}  (rim stride {stride})"
            else:
                shells_line = (
                    f"  shells                : 1  (next delta {stride} is not a "
                    f"shell stride; not counted)"
                )

        explained = sum(deltas.get(d, 0) for d in families)
        share = 100.0 * explained / edges
        return lines + [
            f"  measured width        : {width}",
            shells_line,
            f"  grid fit              : deltas {families} cover "
            f"{explained}/{edges} edges ({share:.1f}%)",
            f"  top deltas            : {deltas.most_common(6)}",
        ], width
    finally:
        evaluated.to_mesh_clear()


def main() -> list[str]:
    obj = bpy.context.active_object
    if obj is None:
        return ["  ABORTED: no active object."]
    curve = obj.data
    if type(curve) is not bpy.types.Curve:
        return [f"  ABORTED: active object {obj.name!r} is a {obj.type}, not a CURVE."]

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

    profile_lines, predicted = describe_profile_source(curve)
    measured_lines, measured = measure_generated_width(obj)
    lines += ["", "  --- ACROSS the curve (the profile) ---", *profile_lines]
    lines += ["", "  --- measured ---", *measured_lines]

    if predicted is not None and measured is not None:
        lines.append("")
        if predicted == measured:
            lines.append(f"  -> MATCH: predicted width {predicted} = measured")
        else:
            lines.append(
                f"  -> MISMATCH: predicted width {predicted}, measured {measured}. "
                f"The formula above is wrong for this curve; trust the measurement."
            )
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
