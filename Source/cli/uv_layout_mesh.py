"""
uv_layout_mesh.py

Script front-end for mesh_uv_core. Lays a converted, solidified ramp out as
three straight UV islands -- inner shell, outer shell (V reversed), and the rim
band -- each scaled to fill FILL_HEIGHT of the UV height, and marks the seams
between them.

All logic lives in mesh_uv_core. This file owns configuration and the text
report.

HOW TO RUN
    Blender Text Editor -> Run Script, with the converted MESH as the active
    object. Works in Object or Edit mode. Run it after Simplify, on the simpler
    mesh; it also works on an unsimplified one.

WHY THAT CONTEXT
    Reads bpy.context.active_object.

SAFETY
    DRY_RUN is True by default: it reports the recovered grid, which shell was
    judged inner, each island's size, UV rectangle and mirrored-face count, and
    the seam counts -- and changes nothing but its own report. Mirrored faces
    should read 0 on every island.

    This edits the mesh IN PLACE, replacing its active UV layer's coordinates
    and every seam. Regenerate from the curve if a run goes wrong.

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
    from mesh_uv_core import UVPlan, UVResult

# --------------------------------------------------------------------- config

DRY_RUN = False

# UV-space gap left and right of, and between, islands.
MARGIN = 0.01

# Share of the UV height each island fills, centred vertically. Each island is
# scaled uniformly, so density differs between islands, not within one.
FILL_HEIGHT = 0.95

# Refuse if the grid model explains less than this share of edges.
MIN_GRID_CONFIDENCE = 0.90

REPORT_TEXT_NAME = "mesh_uv_report.txt"

CORE_DIR_NAME = "lib"
CORE_DIR_FALLBACK = r"D:\dev\Blender\Scripts\BlenderScripts\Source\lib"
CORE_MODULE = "mesh_uv_core"

RULE = "=" * 71


# ------------------------------------------------------------------ bootstrap

def _load_core() -> ModuleType:
    """Import mesh_uv_core from the sibling lib folder.

    Blender's Text Editor does not put a script's directory on sys.path, so a
    plain import fails. The reload matters just as much: without it, edits to the
    core module stay invisible until Blender is restarted. The core reloads
    mesh_simplify_core itself.
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
        f"uv_layout_mesh  [{state}]  {datetime.now():%Y-%m-%d %H:%M:%S}",
        RULE,
    ])


def format_report(plan: UVPlan, dry_run: bool) -> str:
    grid = plan.grid
    lengths = ", ".join(
        f"shell {s} = {length:.4f}" for s, length in enumerate(plan.profile_lengths)
    )

    lines = [
        format_header("DRY RUN" if dry_run else "LIVE"),
        f"  object                : {plan.source.name!r} "
        f"(data {plan.mesh.name!r})",
        "",
        "  --- recovered grid ---",
        f"  width x rows x shells : {grid.width} x {grid.rows} x {grid.shells}",
        f"  verts / edges         : {grid.verts} / {grid.edges}",
        f"  shell stride          : {grid.shell_stride}",
        f"  grid confidence       : {grid.confidence:.1%}",
        "",
        "  --- islands ---",
        f"  profile lengths       : {lengths}",
        f"  inner shell           : {plan.inner_shell} (shorter profile)",
        f"  fill height           : {plan.settings.fill_height:.0%} per island",
        f"  width fit             : {plan.width_fit:.4f}"
        + ("" if plan.width_fit >= 1.0 else "  (U compressed to fit side by side)"),
    ]

    for island in plan.islands:
        (x0, y0), (x1, y1) = island.uv_min, island.uv_max
        lines += [
            f"  {island.name:<6}  faces {island.faces:<5} "
            f"size {island.width:9.4f} x {island.height:9.4f}   "
            f"1 UV = {1.0 / island.scale:.3f} units",
            f"          UV ({x0:.4f}, {y0:.4f}) -> ({x1:.4f}, {y1:.4f})   "
            f"U reversed: {'yes' if island.u_reversed else 'no '}   "
            f"mirrored faces: {island.mirrored_faces}",
        ]

    lines += [
        "",
        "  --- seams and layer ---",
        f"  seams to mark         : {plan.seams_to_mark} "
        f"(replacing {plan.seams_existing} existing)",
        f"  UV layer              : "
        f"{plan.uv_layer if plan.uv_layer is not None else 'none -- UVMap will be created'}",
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


def format_result(result: UVResult) -> str:
    created = " (created)" if result.uv_layer_created else ""
    return "\n".join([
        f"  UV layer written      : {result.uv_layer}{created}",
        f"  faces written         : {result.faces_written}",
        f"  seams                 : {result.seams_marked} marked, "
        f"{result.seams_cleared} cleared",
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
    settings = core.UVSettings(
        min_grid_confidence=MIN_GRID_CONFIDENCE,
        margin=MARGIN,
        fill_height=FILL_HEIGHT,
    )

    plan = core.build_uv_plan(bpy.context, settings)
    sections.append(format_report(plan, DRY_RUN))
    if DRY_RUN:
        return

    result = core.execute_uv_plan(bpy.context, plan)
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
