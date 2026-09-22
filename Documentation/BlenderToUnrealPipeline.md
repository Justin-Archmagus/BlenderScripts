# Blender → Unreal Pipeline Notes

Reference material for scripts in this folder. Facts here were verified against
the live `pinball.blend` and the local Blender 5.2 API reference on 2026-09-20.
Anything not verified is labelled as such.

Target file: `D:\dev\blender_models\pinball.blend` → `D:\dev\Epic\PinballUniverse`

---

## Scene conventions (observed, not documented elsewhere)

- **`Export` collection** sits at scene root and is empty. Generated meshes go here.
- **Curve/mesh pairing:** `MiningRamp` / `MiningRamp_BézierCurve`,
  `RefineryRamp` / `RefineryRamp_BézierCurve`. A source curve named `X_BézierCurve`
  corresponds to mesh `X`. Not every curve follows this — the Mining collection's
  curve was renamed to `InnerRearRamp` on 2026-09-22, with no `_BézierCurve` suffix.
  Plain descriptive names like that are the better pattern: ASCII, no dots, and
  `derive_export_name()` passes them through untouched.
- Ramps are built as **curve + Solidify**, not as hand-modelled meshes. The curve
  is the authoring source and must survive conversion.
- Five curve objects exist as of 2026-09-22: `MiningRamp_BézierCurve`,
  `BézierCurve`, `RefineryRamp_BézierCurve`, `InnerRearRamp`, `Template_BezierCurve`.

## Script output channels (Windows)

A Blender script's `print()` goes to stdout, which on Windows is a **system console
window hidden by default** (Window → Toggle System Console). The **Python Console**
in the Scripting workspace is an interactive REPL and never shows `print()` output,
which makes it a convincing decoy — it looks like the place output should appear.
The Info editor only logs operator calls, so a script run shows exactly one line,
`bpy.ops.text.run_script()`, regardless of what the script did.

A script whose only output channel is stdout therefore looks like a silent failure.
Write reports to a **Text datablock** (`bpy.data.texts` + `Text.from_string()`) so
they are readable in the Text Editor the user is already looking at. Console output
is still worth keeping as a second channel, not the only one.

## Naming hazards

- **Non-ASCII.** `Bézier` contains U+00E9. Unreal sanitizes or rejects non-ASCII in
  asset names, so export filenames must never be derived raw from object names.
- **Dots.** Blender's `.001` duplicate suffix collides with Unreal's asset path
  separator. `BézierCurve.001` is the worst case: accented *and* dotted.
- **Object vs data collision.** `BézierCurve` is simultaneously an object in
  `Manufactory` and the *data* name of `MiningRamp_BézierCurve`. `bpy.data.objects[...]`
  and `bpy.data.curves[...]` with the same string return unrelated datablocks.
  Always be explicit about which collection you are indexing.

## Curve → mesh transform ordering

Ramp curves carry non-uniform scale (`BézierCurve.001` is `5.61 × 2.79 × 2.79`),
non-identity rotation, and a Solidify modifier. The order is **not** interchangeable:

1. Convert to mesh — bakes curve geometry and Solidify into real vertices while
   the object transform stays separate.
2. `transform_apply(location=False, rotation=True, scale=True)` on the resulting mesh.
3. `origin_set(type='ORIGIN_GEOMETRY', center='MEDIAN')`.
4. Zero `location`.

Applying the transform **before** conversion rescales the curve's control points and
changes how bevel/extrude depth is interpreted. Solidify thickness is computed in
local space, so non-uniform scale also produces visibly uneven wall thickness.

### `origin_set` footgun

The default is `type='GEOMETRY_ORIGIN'`, which moves *geometry to the origin* — the
opposite of `'ORIGIN_GEOMETRY'` ("Origin to Geometry"). The two names differ only by
word order. Always pass `type` explicitly.

## FBX export preset

Blender's operator preset **"Unreal - mesh"**, saved by the user and validated manually:

```
<user scripts>/presets/operator/export_scene.fbx/Unreal_-_mesh.py
```

Locate it with `bpy.utils.preset_paths("operator/export_scene.fbx")` rather than
hardcoding — copies exist under 3.6, 4.3 and 5.2.

Settings that matter:

| Property | Value | Note |
|---|---|---|
| `axis_forward` / `axis_up` | `-Z` / `Y` | Standard Unreal convention |
| `bake_space_transform` | `True` | Bakes axis conversion into vertex data |
| `use_selection` | `True` | Exports the selection only |
| `use_mesh_modifiers` | `True` | Applies modifiers at export |
| `use_mesh_modifiers_render` | `True` | Uses **render** visibility, not viewport |
| `global_scale` / `apply_unit_scale` | `1.0` / `True` | Scene unit scale feeds straight through |

### Two traps in this preset

- **`object_types` includes `'OTHER'`**, which covers curves — the FBX exporter
  converts them to mesh on export. Combined with `use_selection=True`, a source
  curve left selected lands in the FBX as a **second silent mesh**. Deselect
  everything and select only the generated mesh immediately before export.
- **Stale `filepath`.** The 3.6 and 4.3 copies hardcode
  `D:\dev\Blender\EvolutionaryRoguelike\tractormech.fbx`. The 5.2 copy does not,
  but any parser must drop `filepath` rather than honour it.

### Reusing a preset from a script

Presets assign onto `bpy.context.active_operator`, which only exists after an
interactive operator run — there is no clean "invoke preset by name" from script.
Parse the `op.<name> = <literal>` lines with `ast.literal_eval` and pass them as
kwargs. This keeps the preset file as the single source of truth: edit it in the
FBX dialog and the scripts follow.

## Verified API signatures (Blender 5.2)

```python
bpy.ops.object.convert(*, target='MESH', keep_original=False, merge_customdata=True, ...)
bpy.ops.object.origin_set(*, type='GEOMETRY_ORIGIN', center='MEDIAN')
bpy.ops.object.transform_apply(*, location=True, rotation=True, scale=True, properties=True,
                               corrective_flip_normals=True, isolate_users=False)
bpy.types.Object.to_mesh(*, preserve_all_data_layers=False, depsgraph=None)
bpy.types.Object.to_mesh_clear()
bpy.utils.preset_paths(subdir)
bpy.types.UnitSettings.scale_length / .system
```

`to_mesh()` on an evaluated object gives post-modifier vertex and polygon counts
**without mutating anything** — the basis for meaningful dry-run reporting.

`convert(keep_original=True)` is a built-in non-destructive path, but duplicating
manually via `obj.copy()` + `obj.data.copy()` gives an explicit handle on the new
object instead of inferring what the operator left selected.

## Collections: view layer vs `bpy.data`

`bpy.data.collections` spans the **whole file**, including collections not linked
to the current scene. Linking an object into one of those succeeds, but the object
never enters `view_layer.objects` — so the follow-up
`view_layer.objects.active = obj` fails and the export dies partway through, after
the duplicate already exists.

Validate the export target against the view layer, not `bpy.data`:

```python
view_layer.layer_collection          # root; walk .children recursively
child.collection.name                # the Collection behind each LayerCollection
```

Two details worth keeping:

- **Exclude the root.** A scene's master collection is not a member of
  `bpy.data.collections`, so `bpy.data.collections[name]` cannot find it. Offering
  it as a target produces a `KeyError` at execute time.
- **`PointerProperty(type=Collection, poll=...)`** gives the native datablock
  selector with search, survives renames, and filters the dropdown. But Blender
  applies `poll` *only when assigning from the UI* — the docs are explicit that an
  invalid value can still be set directly — so the core must re-validate rather
  than trusting the widget. An ID pointer also cannot carry a default, so it
  starts empty.

## Write safety

`DRY_RUN` alone is a poor guard for filesystem risk — it is all-or-nothing, so the
only way to test conversion is to also arm the file write. The core splits these
into independent rungs:

| Setting | Effect |
|---|---|
| `DRY_RUN = True` | Report only; nothing happens |
| `DRY_RUN = False`, `write_fbx = False` | Converts the mesh, touches no files |
| `write_fbx = True` | Writes, but `execute_plan()` refuses an existing target |
| `allow_overwrite = True` | Replaces an existing file |

`build_plan()` records `target_exists` and warns in the report; `execute_plan()`
raises `PlanError` rather than clobbering. So a live run cannot destroy an existing
FBX by accident even with `DRY_RUN` off.

## Open questions

- **Export directory is `D:\dev\blender_models`** (set by the user 2026-09-22) —
  alongside `pinball.blend`, not inside the Unreal content tree. So FBX export is
  currently a staging step with a manual import into Unreal afterwards. Worth
  revisiting if the goal becomes a one-step pipeline.
- **Scene unit scale unread.** `scale_length` feeds directly into FBX output size
  via `apply_unit_scale=True`. The dry run prints it; record the value here once seen.
- **No materials** on the ramp curves, so meshes import into Unreal with zero
  material slots. Unclear whether that is intended.
- **Curve surface geometry unconfirmed.** If a curve has no bevel or extrude,
  conversion yields an edge-only mesh and Unreal imports nothing usable. The dry
  run warns when the evaluated polygon count is zero.
