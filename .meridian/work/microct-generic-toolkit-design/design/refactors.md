# Refactors

Related documents:
- [Design overview](overview.md)
- [Architecture](architecture/architecture.md)
- [Behavioral spec](spec/behavioral-spec.md)
- [Feasibility](feasibility.md)

Preparatory structural work that should be sequenced before or during
toolkit extension. Each refactor is behavior-preserving; combined they
create the seams the new tools and skill need.

## R1: Extract `create_sphere_markers` into `processing/segmentation.py`

**Current state**: No processing-layer function for creating sphere
markers. The only marker creation is `seed_from_region()` which
flood-fills.

**Target**: Add `create_sphere_markers(volume_shape, points, *,
radius_voxels=2)` that creates labeled spheres using numpy. Pure
function, no scipy dependency.

**Why first**: The new `create_point_markers` tool wrapper depends on
this processing primitive.

**Risk**: LOW — additive only.

## R2: Extract shared plane-geometry helpers

**Current state**: `inspect_slice` and `render_axial` only work on axial
slices. The array indexing is hardcoded to `mask[z]` and `volume[z]`.

**Target**: Create `tools/_plane_helpers.py` (internal, not public API)
owning the shared concept of orthogonal plane operations:

```python
SUPPORTED_PLANES = ("axial", "sagittal", "coronal")

def validate_plane(plane: str) -> str: ...
def extract_2d_slice(volume, index, plane): ...
def project_3d_to_2d(points_zyx, index, plane, tolerance=0.5): ...
def display_axes(plane, spacing=None) -> tuple[str, str, float]: ...
def index_bounds(volume_shape, plane) -> int: ...
```

**Why early**: Both `render_slice` and `inspect_plane` need plane
validation, 2D extraction, 3D→2D coordinate projection, and
aspect-ratio metadata. Without a shared home, these semantics would be
duplicated across `slice_renderer.py` and `slice_inspector.py`, creating
coordinated-edit fan-out for every future plane-related change.

**Risk**: LOW — internal module, no public API change. Both `render_slice`
and `inspect_plane` import from it; external callers never see it.

## R3: Unify tool return-shape conventions (documentation only)

**Current state**: Tool modules use different return shapes (explorer
p4185 findings):
- `segmentation_tools`: `{mask/labeled, parameters, summary}`
- `measurement_tools`: flat metric dicts, bare floats
- `slice_inspector`: flat stats dicts
- `slice_renderer`: string paths or metadata dicts

**Target**: Document the canonical envelope patterns. New tools follow
the `{output, parameters, summary}` pattern. Existing tools are NOT
modified (EARS-MCT-28 backward compatibility).

**Why**: Prevents new tools from inventing yet another return shape.

**Risk**: ZERO — documentation only.

## R4: Add compute-efficiency budget entries for new operations

**Current state**: The compute-efficiency skill has budget entries for
threshold, component, recipe, median_filter, and watershed operations.

**Target**: Add budget entries for:
- `median_filter_volume` (same as existing median_filter entry: ~12N bytes, HIGH RISK)
- `create_point_markers` (~4N bytes, LOW)
- `watershed_from_markers` (same as existing watershed entry)
- `orient_volume` (~2*N*V bytes, MEDIUM)

**Why**: The segmenter needs resource gate estimates before running
expensive operations inside the `slice-examination-loop` skill.

**Risk**: LOW — additive to skill document.

## R5: Load `slice-examination-loop` skill in the segmenter prompt

**Current state**: The segmenter prompt (agents/microct-segmenter.md)
instructs the agent to derive seed coordinates from volume geometry and
call `seeded_watershed()`.

**Target**: Add two changes to the segmenter prompt:
1. Load the `slice-examination-loop` skill.
2. Add a decision branch: when `component_summary` shows fewer components
   than the workflow's expected structure count, run the
   `slice-examination-loop` skill in-context instead of attempting
   geometry-based seeding.

**Why**: This is the integration point between the existing segmenter
pipeline and the `slice-examination-loop` skill. The segmenter runs the
skill directly — no spawn — and receives labeled output and seed evidence.

**Risk**: MEDIUM — prompt change that alters agent decision-making. Must
preserve the existing path for cases where bones separate into distinct
components (no bridging).

## R6: Define shared coordinate-frame contract

**Current state**: Orientation changes the coordinate frame but there is
no shared convention for how tools, evidence artifacts, and agent
prompts represent which frame they are in.

**Target**: Define a lightweight frame contract in `processing/types.py`
or a new `processing/frames.py`:

```python
@dataclass(frozen=True)
class CoordinateFrame:
    """Identifies which coordinate frame a set of points is in."""
    frame_id: str  # "original" or "oriented"
    rotation: np.ndarray | None  # 3x3, None for original
    center_physical: tuple[float, float, float] | None

    def to_json(self) -> dict: ...

    @staticmethod
    def original() -> "CoordinateFrame": ...
```

**Why**: Orientation, rendering, markers, and seed evidence all touch
coordinate-frame metadata. Without a single shared type, each module
serializes frame information differently, creating hidden coupling.

**Risk**: LOW — additive data type.

## R7: Deprecate flood-fill seed path in documentation

**Current state**: `create_seeds()` and `seeded_watershed()` use
flood-fill semantics via `seed_from_region()`. The new
`create_point_markers()` and `watershed_from_markers()` use point-marker
semantics. Both are first-class public API.

**Target**: Mark the flood-fill path as legacy in:
- Tool docstrings: add `.. deprecated:: Use create_point_markers for new code`
- Agent prompts: direct new work to point-marker path
- README/design docs: document the distinction and recommendation

**Why**: Two near-neighbor APIs with materially different behavior
(flood-fill vs spheres) creates wrong-call risk. Making the semantic
distinction visible prevents confusion.

**Risk**: LOW — documentation only, no behavior change.

## Sequencing

```
R1 (sphere markers) ──┐
R2 (plane helpers) ───┤
R3 (return docs) ─────┤──→ all tools can build on these
R4 (budget entries) ──┤
R6 (frame contract) ──┘
                       │
                       ▼
       new tool functions (create_point_markers,
       render_slice, inspect_plane, etc.)
                       │
                       ▼
R5 (skill load) ──────→ slice-examination-loop integrated into segmenter
                       │
                       ▼
R7 (deprecation) ─────→ clean up legacy path documentation
```

R1, R2, R3, R4, R6 are independent and can run in parallel.
R5 depends on the tool functions AND the `slice-examination-loop` skill
being written — not just on prompt text existing. The skill needs
stable plane-aware tools (R2), resource gating (R4), and frame
conventions (R6) before it can operate correctly.
R7 depends on R5 being complete and the point-marker path proven in
validation.
