"""
simplify_mesh.py

Script front-end for mesh_simplify_core. Dissolves the redundant profile columns
out of a curve-converted mesh, keeping only the columns where the profile
actually turns.

All logic lives in mesh_simplify_core. This file owns configuration and the text
report.

HOW TO RUN
    Blender Text Editor -> Run Script, with the converted MESH as the active
    object. Works in Object or Edit mode.

WHY THAT CONTEXT
    Reads bpy.context.active_object.

SAFETY
    DRY_RUN is True by default: it reports the recovered grid, the per-column
    turn angles, and exactly which columns would go -- and changes nothing but
    its own report. Read it, confirm the keep set matches intent, then set
    DRY_RUN = False.

    This edits the mesh IN PLACE and there is no duplicate. Unlike the export,
    the source here is already a generated artifact -- regenerate it from the
    curve if a run goes wrong, and keep the curve as the real source of truth.

WHERE THE OUTPUT GOES
    System console and a Text datablock named by REPORT_TEXT_NAME. See
    spline_to_unreal_mesh.py for why the datablock is the channel that works.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import bpy

if TYPE_CHECKING:
    from mesh_simplify_core import SimplifyPlan, SimplifyResult

# --------------------------------------------------------------------- config

DRY_RUN = True

# Profile turn below this many degrees counts as collinear, so the column is
# redundant. Real corners in these ramps turn through tens of degrees, so this
# is deliberately tight.
ANGLE_THRESHOLD_DEG = 1.0

# Explicit 0-based columns to keep, overriding angle detection. Empty means
# detect. At width 11 the detector already finds (0, 2, 8, 10).
KEEP_COLUMNS_OVERRIDE: tuple[int, ...] = ()

# Refuse if the grid model explains less than this share of edges.
MIN_GRID_CONFIDENCE = 0.90

REPORT_TEXT_NAME = "mesh_simplify_report.txt"

CORE_DIR_NAME = "lib"
CORE_DIR_FALLBACK = r"D:\dev\Blender\Scripts\BlenderScripts\Source\lib"
CORE_MODULE = "mesh_simplify_core"

RULE = "=" * 71


# ------------------------------------------------------------------ bootstrap

def _load_core() -> ModuleType:
    """Import mesh_simplify_core from the sibling lib folder.

    Blender's Text Editor does not put a script's directory on sys.path, so a
    plain import fails. The reload matters just as much: without it, edits to the
    core module stay invisible until Blender is restarted.
    """
    import importlib
    import sys

    candidates: list[Path] = []
    try:
        here = Path(__file__).resolve().parent
    except NameError:
        pass
    else:
        candidates.append(here.parent / CORE_DIR_NAME)  # Source/cli -> Source/lib
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


# ------------------------------------------------------------------ reporting

def format_header(state: str) -> str:
    return "\n".join([
        RULE,
        f"simplify_mesh  [{state}]  {datetime.now():%Y-%m-%d %H:%M:%S}",
        RULE,
    ])


def format_report(plan: SimplifyPlan, dry_run: bool) -> str:
    grid = plan.grid
    settings = plan.settings
    detected = "override" if settings.keep_columns_override else "angle detection"

    lines = [
        format_header("DRY RUN" if dry_run else "LIVE"),
        f"  object                : {plan.source.name!r} "
        f"(data {plan.mesh.name!r})",
        "",
        "  --- recovered grid ---",
        f"  width x rows x shells : {grid.width} x {grid.rows} x {grid.shells}",
        f"  verts / edges         : {grid.verts} / {grid.edges}",
        f"  shell stride          : {grid.shell_stride} "
        f"(divisible by width: {grid.shell_stride % grid.width == 0})",
        f"  grid confidence       : {grid.confidence:.1%}",
        "",
        f"  --- columns, by {detected} ---",
        f"  angle threshold       : {settings.angle_threshold_deg} degrees",
    ]

    for column, angle in enumerate(plan.column_angles_deg):
        kept = column in plan.keep_columns
        if column in (0, grid.width - 1):
            why = "boundary, always kept"
        elif kept:
            why = f"turns {angle:6.2f} deg -> corner"
        else:
            why = f"turns {angle:6.2f} deg -> collinear, redundant"
        lines.append(f"      col {column:2}  {'KEEP    ' if kept else 'DISSOLVE'}  {why}")

    lines += [
        "",
        f"  keep                  : {list(plan.keep_columns)}",
        f"  dissolve              : {list(plan.dissolve_columns)}",
        f"  edges to dissolve     : {plan.edges_to_dissolve} "
        f"({plan.edges_to_dissolve - plan.rim_edges_to_dissolve} lengthwise + "
        f"{plan.rim_edges_to_dissolve} end-cap rim)",
        f"  verts {grid.verts} -> {plan.predicted_verts} (predicted)",
        f"      {grid.shells} shells x {grid.rows} rows x "
        f"{len(plan.keep_columns)} kept columns",
    ]

    if plan.warnings:
        lines.append("")
        for warning in plan.warnings:
            prefix = "BLOCKED" if warning.blocking else "WARNING"
            lines.append(f"  {prefix}: {warning.message}")

    lines.append("")
    if dry_run:
        lines += ["  DRY RUN -- nothing changed except this report.", RULE]
    return "\n".join(lines)


def format_result(result: SimplifyResult) -> str:
    return "\n".join([
        f"  edges dissolved       : {result.edges_dissolved}",
        f"  verts                 : {result.verts_before} -> {result.verts_after}",
        f"  faces                 : {result.faces_before} -> {result.faces_after}",
        RULE,
    ])


def emit_report(report: str, text_name: str = REPORT_TEXT_NAME) -> bpy.types.Text:
    print(report)
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    text.from_string(report)
    return text


def _run(core: ModuleType, sections: list[str]) -> None:
    """Append each report section as soon as it exists, so a failure during
    execution still leaves the plan in the report. See spline_to_unreal_mesh."""
    settings = core.SimplifySettings(
        angle_threshold_deg=ANGLE_THRESHOLD_DEG,
        keep_columns_override=KEEP_COLUMNS_OVERRIDE,
        min_grid_confidence=MIN_GRID_CONFIDENCE,
    )

    plan = core.build_simplify_plan(bpy.context, settings)
    sections.append(format_report(plan, DRY_RUN))
    if DRY_RUN:
        return

    result = core.execute_simplify_plan(bpy.context, plan)
    sections.append(format_result(result))


if __name__ == "__main__":
    _sections: list[str] = []
    try:
        _run(_load_core(), _sections)
    except Exception:
        import traceback
        _sections += [format_header("ABORTED"), "", traceback.format_exc()]
    finally:
        emit_report("\n".join(_sections))
