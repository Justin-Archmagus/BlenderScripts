"""
spline_to_unreal_mesh.py

Script front-end for spline_export_core. Converts the active curve into a mesh,
centres it on the world origin, parks it in the Export collection, and optionally
writes an FBX using the "Unreal - mesh" operator preset.

All logic lives in spline_export_core. This file owns configuration and the text
report -- which is presentation, and deliberately does not live in the core, since
the panel front-end reports through the UI instead.

HOW TO RUN
    Blender Text Editor only (Scripting workspace -> Run Script), with the source
    curve selected and active in the viewport.

WHY THAT CONTEXT
    Reads bpy.context.active_object, and convert/transform_apply/origin_set all
    act on the current selection. Those need a real window context with a live
    selection, so `blender --background` will not work.

SAFETY LADDER
    Each rung is independent, so risk can be added one step at a time:

      DRY_RUN = True                     nothing happens but a report
      DRY_RUN = False, WRITE_FBX = False conversion only, zero filesystem writes
      WRITE_FBX = True                   writes, but refuses an existing target
      ALLOW_OVERWRITE = True             replaces an existing file

    The source curve is never modified at any rung; work happens on a duplicate.

WHERE THE OUTPUT GOES
    Both the system console and a Text datablock named by REPORT_TEXT_NAME, which
    shows up in the Text Editor's datablock dropdown.

    The Python Console in the Scripting workspace is an interactive REPL and never
    shows print() output, and on Windows stdout goes to a system console hidden
    until Window -> Toggle System Console. The datablock exists so the report is
    readable without either.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import bpy

if TYPE_CHECKING:
    # Resolves for a type checker run in this folder. At runtime the core is
    # imported dynamically by _load_core(), so this must never execute.
    from spline_export_core import ExportPlan, ExportResult

# --------------------------------------------------------------------- config

DRY_RUN = False
# Independent of DRY_RUN. False converts the mesh but touches no files at all,
# which is the safe way to validate conversion before risking a write.
WRITE_FBX = False
# Only ever set this True deliberately, for one run, when replacing a known file.
ALLOW_OVERWRITE = False


EXPORT_COLLECTION = "Export"
EXPORT_DIR = r"D:\dev\blender_models"
# Blank derives the name from the source curve.
EXPORT_NAME_OVERRIDE = ""

# 'MEDIAN' matches Blender's own "Origin to Geometry" default. 'BOUNDS' uses the
# bounding-box centre, which reads as more centred for long asymmetric ramps.
ORIGIN_CENTER = 'MEDIAN'

REPORT_TEXT_NAME = "spline_export_report.txt"

# The core lives in Source/lib, a sibling of this file's folder (Source/cli).
CORE_DIR_NAME = "lib"

# Last resort, used only when __file__ is not a real path -- which happens for a
# text datablock created inside Blender rather than opened from disk. This is the
# one machine-specific line in the file; everything else resolves relatively so
# the repo works wherever it is checked out.
CORE_DIR_FALLBACK = r"D:\dev\Blender\Scripts\BlenderScripts\Source\lib"

CORE_MODULE = "spline_export_core"

# ------------------------------------------------------------------ bootstrap

def _load_core() -> ModuleType:
    """Import spline_export_core from the sibling lib folder.

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


# ----------------------------------------------------------------- reporting

def format_header(state: str) -> str:
    """Rule, title, mode and timestamp.

    Every report path goes through this, including the abort path. The datablock
    is overwritten in place, so without a timestamp a stale report is
    indistinguishable from a fresh one -- which matters most after an edit, when
    the question is whether you are looking at the run you just triggered.
    """
    return "\n".join([
        "=======================================================================",
        f"spline_to_unreal_mesh  [{state}]  {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=======================================================================",
    ])


def format_report(plan: ExportPlan, dry_run: bool) -> str:
    """Render a plan as the fixed-width text block this script writes out."""
    src = plan.source
    settings = plan.settings
    lines = [
        format_header("DRY RUN" if dry_run else "LIVE"),
        f"  source curve     : {src.name!r}  (data {src.data.name!r})",
        f"  collections      : {[c.name for c in src.users_collection]}",
        f"  location         : {tuple(round(v, 4) for v in src.location)}",
        f"  rotation (rad)   : {tuple(round(v, 4) for v in src.rotation_euler)}",
        f"  scale            : {tuple(round(v, 4) for v in src.scale)}",
        f"  modifiers        : {[m.name for m in src.modifiers] or 'none'}",
        f"  materials        : {[m.name for m in src.data.materials if m] or 'none'}",
        f"  evaluated result : {plan.verts} verts, {plan.polys} polys",
        f"  scene unit scale : {plan.unit_scale}  (system {plan.unit_system})",
        "",
        f"  -> mesh object   : {plan.mesh_name!r} into {settings.export_collection!r}",
        f"  -> origin        : ORIGIN_GEOMETRY / {settings.origin_center}, then zeroed",
    ]

    if settings.write_fbx:
        kwargs = plan.preset_kwargs
        lines += [
            f"  -> fbx path      : {plan.out_path}",
            f"  -> preset        : {plan.preset_path}",
            f"  -> preset keys   : {len(kwargs)} "
            f"(use_selection={kwargs.get('use_selection')}, "
            f"bake_space_transform={kwargs.get('bake_space_transform')}, "
            f"axis_forward={kwargs.get('axis_forward')!r}, "
            f"axis_up={kwargs.get('axis_up')!r})",
        ]
    else:
        lines.append("  -> fbx           : SKIPPED (WRITE_FBX is False)")

    if plan.warnings:
        lines.append("")
        for warning in plan.warnings:
            prefix = "BLOCKED" if warning.blocking else "WARNING"
            lines.append(f"  {prefix}: {warning.message}")

    if plan.stale_preset_path:
        lines.append(
            f"  note: ignored hardcoded filepath in preset -> {plan.stale_preset_path}")
    if plan.unparsed_preset_lines:
        lines.append(
            f"  note: {len(plan.unparsed_preset_lines)} preset line(s) unparsed: "
            f"{plan.unparsed_preset_lines}")

    lines.append("")
    if dry_run:
        lines.append("  DRY RUN -- nothing changed except this report.")
        lines.append("=======================================================================")
    return "\n".join(lines)


def format_result(plan: ExportPlan, result: ExportResult) -> str:
    return "\n".join([
        f"  converted  : {result.mesh_object_name!r} "
        f"({result.verts} verts, {result.polys} polys)",
        f"  exported   : {result.fbx_path or 'skipped (WRITE_FBX is False)'}",
        f"  source     : {plan.source.name!r} untouched",
        "=======================================================================",
    ])


def emit_report(report: str, text_name: str = REPORT_TEXT_NAME) -> bpy.types.Text:
    """Send a report to the console and to a Text datablock.

    The datablock is the channel that actually works here: stdout is hidden on
    Windows, and the Scripting workspace's Python Console is a REPL that never
    shows print() output at all.
    """
    print(report)
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    text.from_string(report)
    return text


def _run(core: ModuleType) -> str:
    settings = core.ExportSettings(
        export_dir=EXPORT_DIR,
        export_collection=EXPORT_COLLECTION,
        name_override=EXPORT_NAME_OVERRIDE,
        origin_center=ORIGIN_CENTER,
        write_fbx=WRITE_FBX,
        allow_overwrite=ALLOW_OVERWRITE,
    )

    plan = core.build_plan(bpy.context, settings)
    report = format_report(plan, DRY_RUN)

    if DRY_RUN:
        return report

    result = core.execute_plan(bpy.context, plan)
    return f"{report}\n{format_result(plan, result)}"


if __name__ == "__main__":
    _core = None
    try:
        _core = _load_core()
        _report = _run(_core)
    except Exception:
        # Full traceback, not just the message -- otherwise an unexpected error
        # lands only in the hidden console and looks like a silent failure again.
        import traceback
        _report = f"{format_header('ABORTED')}\n\n{traceback.format_exc()}"

    emit_report(_report)
