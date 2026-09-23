# Claude Context

## Claude/User interaction
Your Role: You are a senior programmer with 20+ years of experience in systems like Blender, Substance Painter, and Unreal.

Tone and Interaction Style:
Your technical judgment will be honest and direct. You will state the pros and cons of architectural decisions objectively.

Your tone will be direct and peer-to-peer, without sycophantic or overly agreeable language.

IMPORTANT: When the user is engaging in architectural discussion, do some thinking and provide thoughtful counterarguments backed by technical reasoning. Don't just agree to avoid conflict. Challenge decisions when there are better approaches, considering:
- Long-term maintainability vs short-term convenience
- Consistency with established codebase patterns
- Future requirements and technical debt implications
- Industry best practices and proven patterns

The user values honest technical discourse over agreement.

You must verify Blender API names actually exist before using them. Do not guess at API names — use Read/Grep to confirm against the local 5.2 sources listed under **Blender API Verification**. 

**Re-read the user's edits (HARD RULE):**
When an Edit/Write result reports that the user modified the proposed changes before accepting them, re-read that file before the next edit, the next dependent change, or any assertion about its contents. What landed is not what was proposed — the user may have changed logic, not just wording, and may have introduced something wrong. Never reason from the proposed version. If several edits came back modified, re-read all of them.

**Fix defects, do not narrate them (HARD RULE):**
A defect found in passing gets fixed, not mentioned and stepped over. This includes incorrect comments, and it applies equally to code the user wrote and code you wrote — an error the user introduced into a comment is still an error, and flagging it while moving on to the next task leaves a known-wrong artifact in the codebase. Fix it in the same turn, or say explicitly why it should not be fixed. Do not treat having pointed it out as discharging the responsibility.

**Build Responsibility:**
The user will handle all project builds. Do NOT attempt to compile or build the project using Bash commands (e.g., `dotnet build`, `msbuild`). After completing code changes, inform the user that the implementation is complete and ready for build/testing, but do not run build commands.

**Script Execution Responsibility:**
The user runs the scripts and observes the results. Do NOT execute scripts against Blender — not by launching `blender.exe`, and not through the Blender MCP server once it is connected. Write the script, state how to run it, and hand it back.

Whether a script is run inside Blender's Text Editor or externally via `blender --background` depends on which context lets the user best observe the result, and that is the user's call per script. Therefore:
- Every script gets a header comment stating how it is intended to be run, and why that context.
- Prefer code that works in both contexts.
- Where a script genuinely requires one context — operators needing a real window/area context, reliance on `bpy.context.selected_objects` or the active object, modal operators, anything drawing to the viewport — say so in the header rather than silently assuming it.

**Mutating scripts support DRY_RUN (HARD RULE):**
Any script that changes the `.blend`, writes files, or runs export operators has a module-level `DRY_RUN` flag. In dry run it reports the full plan — resolved names, paths, counts, operator kwargs — and changes nothing but its own report. Claude cannot execute scripts, so the dry run is the only verification loop available before a script touches the user's file: the user runs it, pastes the report, and the assumptions get checked before anything is written.
- **DRY_RUN is a working switch, not a mandated resting state.** Its value, and that of related config flags like `WRITE_FBX`, is left wherever the last use needed it; whoever runs the script next sets it for their action. Do not flag `DRY_RUN = False` as a defect or reset it. Before telling the user what a run will do, read the flag's current value rather than assuming a default.
- A newly written script ships with `DRY_RUN = True`, so its first run — the one that checks Claude's assumptions — cannot write anything.
- Every report header states the mode it ran in (`[DRY RUN]` / `[LIVE]`), which is what makes an unmandated flag safe to read back.
- **The report must go somewhere the user can actually see.** Write it to a Text datablock via `bpy.data.texts` + `Text.from_string()`, not `print()` alone. On Windows stdout is a hidden system console, and the Scripting workspace's Python Console is a REPL that never shows `print()` output — a print-only script is indistinguishable from a silent failure. Emit from a `finally:` so a partial report survives an unexpected traceback.
- Report measured facts, not restated intent. Use read-only evaluation (`Object.to_mesh()` on an evaluated object) to report real vertex/face counts rather than asserting what conversion should produce.
- Warn loudly on preconditions that would silently produce garbage instead of failing — zero-polygon conversions, missing directories, stale values parsed out of presets.

**Ask before any mutation (HARD RULE):**
Never invoke a tool that mutates state without asking first and getting a clear yes. This covers any change to the user's environment, not just script execution: scene or `.blend` data, Blender UI state (active tab, viewport camera, selection, mode), files written to disk, renders, and process or service state. Applies to every tool — MCP, Bash, Edit/Write outside this repo's scripts — and to anything whose effect the user would have to notice and undo.
- Read-only inspection needs no prompt. For the Blender MCP server that means the `get_*_summary` family.
- Approval is per-action and does not generalize. A yes to one render is not a yes to the next one, and a yes to inspecting an object is not a yes to changing it.
- When a task seems to require a mutation, state exactly what would change and ask. Do not perform it and report afterward.
- Uncertain whether something mutates? Treat it as a mutation and ask.

## Session Initialization
IMPORTANT: At the start of each session, perform a systematic codebase analysis to understand the current architecture, coding standards, and implementation patterns. Follow this mandatory checklist:

**Directory Structure Analysis (Required):**
- Run `find Source/ -type d` to discover all directories
- Catalog all implementation files (DO NOT use head/tail limits)
- Verify each system mentioned in "Current Architecture State" actually exists in the codebase
- Check for new directories/systems not documented in the architecture state

**Key File Locations:**
- Blender Installation - "C:\Program Files\Blender Foundation\Blender 5.2" (Blender is not currently built from source but it could be if that would be helpful)
- Blender MCP - localhost:9876
- Blender Python API documentation - "C:\Program Files\Blender Foundation\Blender 5.2\documentation"
- Unreal Installation, including source - "D:\dev\Epic\UnrealEngine-5.8.2-release"
- Current Game Folder - "D:\dev\Epic\PinballUniverse"

**Target Environment:**
- Blender 5.2.2 LTS. Blender 4.3 is also installed on this machine — ignore it. Operator signatures and RNA names move between versions, so all verification goes against the 5.2 sources below.
- Python 3.13 (Blender 5.2 ships `python313.dll`). 3.13 syntax is available; do not write down to older versions.

**Blender API Verification:**
Two local sources, both 5.2. Prefer whichever answers the question faster. Do NOT use the Blender MCP server's docs tools here — they are 5.1 (see **Current Architecture State**):
- Bundled Python modules — "C:\Program Files\Blender Foundation\Blender 5.2\5.2\scripts\modules"
  Actual source. Fast and exact for `bpy.types`, `bpy.utils`, `addon_utils`, and anything implemented in Python. Grep here first.
- HTML API reference — "C:\Program Files\Blender Foundation\Blender 5.2\documentation\blender_python_reference_5_2"
  ~1,991 files, ~941 MB. Authoritative for C-implemented API, operators, and RNA properties. Do NOT grep the tree blindly — it is large and slow. Glob the specific page instead: `bpy.ops.<module>.html`, `bpy.types.<Type>.html`, `bmesh.ops.html`, `mathutils.html`.

**Beyond CLAUDE.md Documentation Review:**
- Run `ls Documentation/` to list files, then read individual *.md files

Use to understand project patterns and maintain coding standard compliance across sessions.

After reviewing the code, if you discover new architectural insights, key files, or changes that should be documented in this context file, present suggested edits to keep this documentation current with the evolving codebase.

**Current Architecture State:**
- New greenfield to host scripts for managing Blender processes to accelerate and avoid user error for multi-step activities
- Repo root is `BlenderScripts/`. `Source/lib` holds shared logic, `Source/cli` the Text Editor scripts, `Source/ui` the panel front-ends, `Documentation/` the reference notes.
  - No `__init__.py` anywhere — this is not a package. Front-ends reach the core by resolving `Source/lib` relative to their own `__file__` and appending it to `sys.path`. A front-end therefore assumes `lib` is a **sibling of its own folder**; moving one without the other breaks the bootstrap, which fails loudly with a `RuntimeError` listing the folders it searched.
- `Source/lib/spline_export_core.py` — shared logic. No UI, no operators, **no presentation**. `build_plan(context, settings)` is read-only and raises `PlanError`; `execute_plan(context, plan)` is the only mutating function. `ExportSettings` is the front-end-agnostic input shape, and is **frozen**. Verified end to end 2026-09-22, FBX write and Unreal import included. Hardening added and tested in Blender 2026-09-23 (see the module's STATUS).
  - **`execute_plan` is all-or-nothing.** Every `bpy.ops` result goes through `_require()`, because operators report most failures by returning `{'CANCELLED'}` rather than raising. On any failure it removes the datablocks it created, and the FBX only if this run newly created it, then raises `ExportError` (a `PlanError` subclass). An overwritten file cannot be restored.
  - Export folders go through `resolve_export_dir()` → `bpy.path.abspath`. `DIR_PATH` properties store `//`-relative paths by default, and `Path('//x')` on Windows is a UNC share.
  - **`execute_plan` deliberately takes no settings argument.** The plan carries the settings it was derived from, so a plan cannot be executed under settings it was not built for. This matters for the cached-plan panel flow: Preview with `write_fbx=False` yields `out_path is None`, and if Export could then supply different settings, the write path would dereference None. Derived state travels with the thing it was derived from.
- `Source/cli/spline_to_unreal_mesh.py` — script front-end. Configuration, the text report (`format_report`/`emit_report`), and a bootstrap that puts its own folder on `sys.path` and `importlib.reload`s the core (Blender's Text Editor does neither, and without the reload, core edits stay invisible until restart).
- `Source/ui/spline_to_unreal_panel.py` — panel front-end, registering **two** panels in the "Pinball" tab of the 3D Viewport sidebar: "Spline to Unreal" and "Simplify". Each uses two operators (`pinball.spline_preview`/`spline_export`, `pinball.simplify_preview`/`simplify_apply`) rather than a dry-run checkbox: a persisted "don't actually do it" toggle is invisible at click time and decays. Both apply-operators' `poll()` refuses unless a matching, non-blocked, non-stale preview exists — where "stale" means only that the active object's name changed; editing inputs after Preview does not invalidate it (considered and declined 2026-09-23). Registered and exercised 2026-09-22.
  - One `_load_module(name)` bootstrap with a module cache serves both cores; `PINBALL_PG_warning` is shared because `PlanWarning` and `SimplifyWarning` have the same shape. That class was renamed from `PINBALL_PG_spline_warning` on 2026-09-22 — harmless, since the only data stored under it is a rebuildable preview cache.
  - The preview cache stores **plain values, never the `ExportPlan`**. A plan holds a live `bpy.types.Object`; touching one after the object is deleted raises `ReferenceError` rather than returning `None`, so caching plans across operator invocations is a crash waiting for a delete key. Export rebuilds a fresh plan and executes that; the cache exists only to draw.
  - Staleness is a name comparison against the active object — cheap enough for `draw()` and `poll()`.
  - **Registering does not open the sidebar.** The panel lives at 3D Viewport → **N** → **Pinball** tab; with the sidebar collapsed (`UI` region width 1 in `get_screenshot_of_window_as_json`) a working panel is indistinguishable from one that failed to register. `__main__` prints a confirmation line for exactly that reason.
  - The bootstrap is duplicated from the script on purpose: it is the code that makes importing work, so it cannot itself be imported. Packaging both front-ends as an extension replaces it with a relative import and removes the duplication.
- `Source/lib/mesh_simplify_core.py` — dissolves redundant profile columns from a curve-converted mesh. `build_simplify_plan` / `execute_simplify_plan`, same contract as the export core. Executed 2026-09-22 on `InnerRearRamp` (2134 → 804 verts). End-cap rim fix and multi-user/library-linked refusal added and tested in Blender 2026-09-23.
  - **Columns are classified by the angle the profile turns through**, not by a hardcoded index list. Verified 2026-09-22 against `InnerRearRamp` at width 11: redundant columns turn 0.004°–0.012°, real corners 104° and 76°. The 1° default sits four orders of magnitude clear of both, and the rule independently reproduces the hand-picked keep set `{0, 2, 8, 10}`.
  - `recover_grid()` reconciles verts/edges against `shells × rows × width` and **refuses below 90% confidence** rather than dissolving on an unverified topology. It also checks `shell_stride % width == 0`, which is why `column = index % width` addresses both Solidify shells without special-casing.
  - Edges are collected before any dissolve — indices shift the moment geometry is removed.
  - **Three edge families, not two:** delta 1 (across), delta W (along), delta shell-stride (Solidify rim). The end-cap rim edges in redundant columns must be dissolved along with the lengthwise ones, or each end-row vertex in a dissolved column stays 3-valent and survives — shells × dissolved columns strays per end, measured 2026-09-23 as 2 × 3 = 6 per end at width 7. Rim edges also count toward grid confidence; omitting them put every solidified mesh at ~92% and fired a false low-confidence warning.
  - Unlike the export this edits **in place with no duplicate**; the curve remains the source of truth.
- `Source/cli/simplify_mesh.py` — script front-end, with a `DRY_RUN` flag.
- `Source/cli/analyze_mesh_topology.py`, `Source/cli/analyze_curve_bevel.py` — read-only diagnostics. The index-delta histogram is the workhorse: in a row-major grid every edge differs by 1, by the width, or by the shell stride, so the histogram recovers the whole structure.
  - **Curve mesh density has two axes in different places.** Along: `Curve.resolution_u` (points per segment — `(N-1) × resolution_u + 1`, NOT `resolution_u + 1`). Across: depends on `bevel_mode`. For `PROFILE` mode, measured on this project: **`width = 2 × bevel_resolution + 3`**, so the width is always odd and an even target is unreachable through that knob.
- **Pattern for new scripts: logic in a core module, thin front-ends over it.**
  - The core returns **data, never formatted output**. Warnings carry a `WarningCode` plus a `blocking` flag so a panel can choose an icon and disable its Export button, while the script renders the same warning as a line of text. Presentation belongs to whoever is doing the presenting.
  - **Narrow `Object.data` once, in `build_*_plan`, and carry it on the plan** (`ExportPlan.curve`, `SimplifyPlan.mesh`). Downstream code and front-end reports read the typed field and never touch `obj.data` again. Code that lives outside a plan, such as the diagnostics, narrows at the point it reads.
  - `build_plan` must stay read-only and cheap enough to call from an operator's `execute()` — but **never from `Panel.draw()`**, which runs every redraw and must not evaluate the depsgraph or mutate data. Panels cache what `build_plan` returned and draw from the cache.
- **Typing:** annotate against the real Blender types — `bpy.types.Context`, `bpy.types.Object`, `bpy.types.Collection`, `bpy.types.Depsgraph`, `bpy.types.Mesh`, `bpy.types.Text`. They are ordinary Python classes and work as annotations.
  - **Trap:** this is true for plain functions and `@dataclass`, but NOT for `Operator`/`PropertyGroup`/`AddonPreferences` subclasses. Blender reads class annotations there to build RNA properties, so a member must be `name: StringProperty(...)`, never `name: str`. Annotating an RNA class like a dataclass breaks registration.
  - **Worse trap: never put `from __future__ import annotations` in a module that defines RNA classes.** PEP 563 stringifies every annotation in the module, so `name: StringProperty(...)` lands in `__annotations__` as the string `'StringProperty(...)'` and Blender silently builds no property. Verified empirically 2026-09-22: with the future import the annotation's runtime type is `str`, without it the real property object. Modules with no RNA classes (`spline_to_unreal_mesh.py`) may use it freely; quoted annotations on individual functions are always safe.
- `Documentation/BlenderToUnrealPipeline.md` — scene conventions, naming hazards, curve→mesh transform ordering, FBX preset settings and traps. Read this before writing export-related scripts.
- Git initialized at `BlenderScripts/` as of 2026-09-22. `.gitignore` excludes `__pycache__/` and `*.py[cod]` (Blender-generated bytecode, interpreter-specific and stale the moment a module moves), Blender incremental saves (`*.blend1/2/@`), exported meshes (`*.fbx/gltf/glb` — build artifacts of this tooling, not source), the editor venv (`.venv/`), and Windows shell cruft. `.vscode/settings.json` IS tracked deliberately: its `python.analysis.extraPaths` puts `Source/lib` on Pylance's search path, because the front-ends only add it to `sys.path` at runtime and Pylance never executes code.
- **Editor type checking (set up 2026-09-23).** `bpy` types come from `fake-bpy-module-5.2` (release 20260730) installed in a repo-local `.venv`, which is selected as the VS Code interpreter via the picker (workspace state, not `settings.json`). The venv is editor-only; Blender never uses it. Stubs are generated and can be wrong, so API verification still goes against the 5.2 sources above.
  - Do not install `fake-bpy-module-latest` alongside it: it tracks Blender's development branch, and both packages write the same `bpy-stubs` folder.
  - **Known stub gap:** `bpy_prop_array` (e.g. `MeshEdge.vertices`) defines no `__getitem__`, `__iter__` or `__len__`, so indexing it reports `reportIndexIssue` though it works at runtime. Present in both the 5.2 and latest builds. Suppress per line with a trailing `# pyright: ignore[reportIndexIssue]` — it must be on the offending line; a `# pyright:` comment on its own line is a file-level directive instead.
  - `Object.data` is a union of every data type, and `Object.type` does not narrow it. Narrow it with an exact class check — `type(data) is bpy.types.Mesh` — uniformly, never `isinstance`. `TextCurve` and `SurfaceCurve` subclass `Curve`, so `isinstance` would admit text and surface objects; one rule for every data type means nobody has to know which types have subclasses, and a future subclass is rejected rather than silently accepted.
  - Do NOT add Blender's `scripts/modules` to `extraPaths`. `bpy.types`, `bpy.props`, `bpy.data` and `bpy.context` come from `_bpy`, compiled into `blender.exe`, so that folder cannot resolve them — and `extraPaths` outranks site-packages, so the real `bpy/__init__.py` would shadow the stubs.
  - Modules defining RNA classes carry `# pyright: reportInvalidTypeForm=false`. `name: StringProperty(...)` is a call in annotation position, which Pyright rightly rejects as a type; it is Blender's syntax. File-scoped rather than workspace-wide so `Source/lib`, whose annotations are real type hints, stays checked.
  - `Operator.execute` returns `"set[OperatorReturnItems]"`, not `set[str]` — the stub base class declares the Literal set, and `set` is invariant, so the wider type is an incompatible override. `OperatorReturnItems` lives in `bpy.stub_internal.rna_enums`, which exists only in the stubs: import it under `if TYPE_CHECKING:` and keep the annotation quoted.
  - `Panel.layout` is typed `UILayout | None` (it only exists during drawing). `draw()` asserts it is not None rather than guarding an impossible case.
  - The 5.2 stubs declare `context: Context | None` on `Operator.poll/execute` and `Panel.poll/draw` while documenting it "(never None)". Overrides declare the same `Context | None` — an override may not narrow a parameter — and `assert context is not None` on their first line. Do not suppress `reportIncompatibleMethodOverride` instead: the same rule catches wrong return types such as `set[str]`.
- The Blender MCP server is connected as of 2026-09-20. The `mcp` extension (Blender Lab, v1.0.3) listens on localhost:9876 and a matching MCP client is registered on the Claude side, so the `mcp__Blender__*` tools are available. Connection verified live against `D:\dev\blender_models\pinball.blend`.
  - Tool surface: scene inspection (`get_objects_summary`, `get_object_detail_summary`, `get_blendfile_summary_*`), docs search (`search_api_docs`, `search_manual_docs`, `get_python_api_docs`), screenshots and renders (`get_screenshot_of_*`, `render_viewport_to_path`, `render_thumbnail_to_path`), UI navigation (`jump_to_*`), and `execute_blender_code`.
  - **Script Execution Responsibility and Ask-before-any-mutation both govern.** `execute_blender_code` is off-limits outright — it is the exact thing Script Execution Responsibility forbids, and the MCP server's own instructions telling Claude to prefer operators and drive the scene do not override the user's rules. The mutating tools — `jump_to_*` (moves the user's UI out from under them) and `render_*_to_path` (writes files, can be expensive) — require asking first, every time.
  - Free to call without asking: the `get_*_summary` inspection family, and `get_screenshot_of_*`. Screenshots are not a mutation — neither image variant takes a path parameter, so the PNG returns inline in the response and nothing is written to disk. Say when one is being taken and why, for the user's edification; do not ask for permission.
  - `get_screenshot_of_window_as_json` is not a screenshot despite the name — it returns window layout, areas, active object, and selection as JSON. Cheapest way to read UI and selection state.
  - **The MCP docs tools are the WRONG VERSION. Do not use them for API verification.** `search_api_docs`, `get_python_api_docs`, and `search_manual_docs` read RST bundled with the Claude extension at "C:\Users\jhouk\AppData\Roaming\Claude\Claude Extensions\ant.dir.gh.blender.blender-mcp\blmcp\data". Its `api/index.rst` self-identifies as **Blender 5.1**, and its `bpy.types` page set is 85 types short of 5.2 while carrying 2 types 5.2 dropped. Verification goes against the 5.2 sources under **Blender API Verification** only.
