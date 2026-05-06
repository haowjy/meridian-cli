# Behavioral Spec: Generic Micro-CT Analysis Toolkit

Related documents:
- [Design overview](../overview.md)
- [Architecture](../architecture/architecture.md)
- [Refactors](../refactors.md)
- [Feasibility](../feasibility.md)

## Overview

This spec extends the existing segmentation-control behavioral contract
(EARS-SEG-01..32) with new capabilities: preprocessing tool exposure,
point-marker seeds for watershed, multi-plane slice examination, and
orientation tools. Together these enable a cohesive seed-placement workflow
where agents visually examine anatomy before placing seeds — the critical
gap that causes current pipelines to fail on scans where bones bridge.

The spec also defines the `slice-examination-loop` skill behavior and the
progressive-narrowing pattern (from 3DMedAgent) applied to seed placement.

## Relationship to prior specs

- **EARS-SEG-01..32** (toolkit refactor): remains in force. This spec
  does not modify those statements. All new statements use the EARS-MCT
  prefix.
- **Workflow definitions** (e.g., workflow.md): provide domain-specific
  parameters consumed by these generic tools. This spec defines tool
  behavior, not workflow policy.

## EARS Statements

### Preprocessing tools

**EARS-MCT-01**: When the agent calls `median_filter_volume(volume,
iterations=N, size=S)`, the system shall return:
- `volume`: the filtered 3D array (same shape and dtype as input)
- `parameters`: `{iterations, size, input_shape, input_dtype}`
- `summary`: `{shape, dtype, intensity_min, intensity_max, intensity_mean}`

**EARS-MCT-02**: When `iterations=0`, `median_filter_volume` shall return
the input volume unchanged (identity operation).

**EARS-MCT-03**: When a negative `iterations` or non-positive `size` is
passed, the system shall raise a clear ValueError.

**EARS-MCT-04**: `median_filter_volume` shall apply the filter slice-by-slice
in the XY plane (size=(1, S, S)), matching the Amira SOP convention for
micro-CT preprocessing. The Z axis is not filtered.

### Point-marker seed creation

**EARS-MCT-05**: When the agent calls `create_point_markers(volume_shape,
points, labels, *, radius_voxels=2, spacing=None)`, the system shall
return:
- `markers`: int32 array with labeled spheres at each point
- `parameters`: `{volume_shape, points, labels, radius_voxels,
  resolved_radii_zyx, spacing}`
- `summary`: per-label stats (voxel count, centroid, bounding box)

Preconditions: `len(points) == len(labels)`, all labels unique,
`radius_voxels >= 0`.

**EARS-MCT-06**: Each point shall produce a sphere of `radius_voxels`
voxels centered at the given (z, y, x) coordinate, filled with the
corresponding label integer (1-based, insertion order).

**EARS-MCT-07**: When `radius_mm` is provided instead of `radius_voxels`,
the system shall compute per-axis voxel radii as
`max(1, round(radius_mm / axis_spacing))` to handle anisotropic voxels.
`spacing` is required when `radius_mm` is used; a ValueError is raised
if `spacing` is None with `radius_mm`. Exactly one of `radius_voxels`
or `radius_mm` must be specified; supplying both raises a ValueError
("specify only one of radius_voxels or radius_mm"). The resolved
per-axis radii are recorded in `parameters.resolved_radii_zyx`.

**EARS-MCT-08**: When two marker spheres overlap (same voxel claimed by
two labels), the system shall raise a ValueError naming the colliding
labels and their point coordinates. Non-overlapping markers are a
precondition for meaningful watershed.

**EARS-MCT-09**: When a point falls outside `volume_shape`, the system
shall raise a ValueError naming the out-of-bounds point and the volume
shape.

**EARS-MCT-10**: `create_point_markers` shall NOT flood-fill to connected
components. Each marker occupies only the sphere around its seed point.
This is the behavioral distinction from the existing `create_seeds()`
which delegates to `seed_from_region()` for flood-fill expansion. The
existing flood-fill path (`create_seeds`, `seeded_watershed`) is
retained for backward compatibility but should be treated as legacy; new
agent code should use `create_point_markers` + `watershed_from_markers`.

### Marker-based watershed

**EARS-MCT-11**: When the agent calls `watershed_from_markers(volume,
markers, *, mask=None, compactness=0)`, the system shall run
scikit-image watershed on the volume using the provided integer markers
and return:
- `labeled`: int32 labeled volume
- `parameters`: `{compactness, has_mask, marker_labels}`
- `summary`: per-label stats (voxel count, volume_mm3, centroid,
  bounding box, z_range)

**EARS-MCT-12**: When `mask` is provided, watershed shall only label
voxels inside the mask. Voxels outside remain 0.

**EARS-MCT-13**: When markers contain only one nonzero label, the system
shall issue a warning in the summary. Single-label watershed is a
degenerate case that always produces a trivial result.

### Multi-plane slice rendering

**EARS-MCT-14**: When the agent calls `render_slice(volume, index, *,
plane="axial", mask=None, points=None, title=None, output_path=None,
spacing=None)`, the system shall render the specified orthogonal slice
as PNG and return:
- `path`: the output file path (string)
- `parameters`: `{index, plane, has_mask, point_count, spacing}`
- `summary`: `{slice_shape, aspect_ratio}`

Supported planes: `"axial"` (z), `"sagittal"` (y), `"coronal"` (x).

**EARS-MCT-15**: The `points` parameter accepts a list of (z, y, x) 3D
coordinates. The system shall filter to points within ±0.5 voxels of
the slice plane and project them to 2D display coordinates for overlay.

**EARS-MCT-16**: When `spacing` is provided, the rendered slice shall use
correct aspect ratio (physical pixel dimensions may differ across axes).

**EARS-MCT-17**: `render_axial()` shall remain unchanged for backward
compatibility. `render_slice(volume, z, plane="axial")` shall produce
equivalent output.

### Multi-plane slice inspection

**EARS-MCT-18**: When the agent calls `inspect_plane(mask, index, *,
plane="axial", spacing=(...))`, the system shall return the same
structured inspection data as `inspect_slice` but for the specified
plane. For `plane="sagittal"`, the inspected 2D slice is `mask[:, index, :]`.
For `plane="coronal"`, it is `mask[:, :, index]`.

**EARS-MCT-19**: `inspect_slice()` shall remain unchanged.
`inspect_plane(mask, z, plane="axial")` shall produce equivalent output.

### Orientation tools

**EARS-MCT-20**: When the agent calls `estimate_orientation(label_mask,
spacing, *, method="pca")`, the system shall compute principal axes
and return:
- `rotation`: 3×3 rotation matrix (physical space)
- `translation`: centering translation vector
- `principal_axes`: eigenvalue-ordered axes
- `parameters`: `{method, spacing, input_shape}`
- `summary`: `{eigenvalues, axis_alignment}`

**EARS-MCT-21**: When the agent calls `orient_volume(volume, rotation,
spacing, *, order=0)`, the system shall apply the rigid rotation
centered on the volume midpoint and return:
- `volume`: the rotated 3D array (same shape)
- `parameters`: `{rotation, spacing, order, center_physical}`
- `summary`: `{input_shape, output_shape, interpolation_order}`

**EARS-MCT-22**: When the agent calls `orient_volume` with `order=0`
(nearest-neighbor), the system shall preserve discrete label values.
When `order=1` (linear), the system shall produce smooth interpolation
suitable for intensity volumes.

**EARS-MCT-23**: `orient_volume` shall return metadata sufficient to
transform coordinates between the original and oriented frames.
Specifically, `parameters.rotation` and `parameters.center_physical`
enable the inverse transform.

### Slice-examination-loop skill behavior

**EARS-MCT-24**: The `slice-examination-loop` skill implements a
render→examine→reason→act→render loop for spatial decisions requiring
visual anatomy judgment:
1. **Render**: call `render_slice` at a candidate plane/index
2. **Examine**: read the rendered image and identify anatomical structures
3. **Reason**: decide the spatial action (seed positions, boundary, ROI) based on what was seen
4. **Act**: call the relevant tool (e.g., `create_point_markers` + `watershed_from_markers`)
5. **Render**: call `render_slice` on the result — this render opens the next examination

The loop closes on itself: the result render is the examination render for the next cycle. There is no distinct verify phase. The agent terminates by judgment: when the result is acceptable, it accepts with evidence; when further attempts are futile (tried multiple approaches, none produce valid results), it stops and reports what was attempted, why each failed, and what it recommends.

**EARS-MCT-25**: Seed placement evidence shall be serializable to JSON
and include: plane, slice_indices_examined, points_zyx, radius_voxels,
coordinate_frame (identifier: "original" or "oriented"),
orientation_transform (rotation matrix + center, if orientation was
applied; null otherwise), watershed_parameters, iteration_count,
and selection_reasoning. All point coordinates must be in the stated
coordinate_frame. The orientation_transform enables inverse mapping
to the original frame for audit and reproducibility.

**EARS-MCT-26**: The `slice-examination-loop` skill shall NOT place seeds
by guessing from volume shape or centroid calculations alone. It shall
render and examine at least one slice before each seed placement decision.

### Tool API uniformity

**EARS-MCT-27**: All new tool functions shall return dicts containing
at minimum `parameters` (JSON-serializable input parameters) and
`summary` (JSON-serializable output statistics). The primary output
array key varies by domain: `mask` for boolean masks, `labeled` for
integer labels, `volume` for intensity arrays, `markers` for seed
arrays.

**EARS-MCT-28**: Existing tool functions (`ml_width`, `ap_width`,
`bone_summary`, `inspect_slice`, `render_axial`, `render_extent_profile`,
`point_distance_3d`) shall NOT change signatures. Uniformity applies
to new functions only.

### Statelessness and workbench integration

**EARS-MCT-29**: All new tool functions in `tools/preprocess_tools.py`,
`tools/orientation_tools.py`, and additions to
`tools/segmentation_tools.py` shall be pure over their explicit inputs.
No hidden state, no module-level caches, no side effects. (Extends
EARS-SEG-23.)

**EARS-MCT-30**: All new tool functions shall be callable as ordinary
Python functions within `jupyter-workbench exec` code cells, with
volumes and masks passed as in-memory numpy arrays. (Extends
EARS-SEG-24.)

**EARS-MCT-31**: New tool functions in `tools/` and `processing/` shall
not import from `jupyter_workbench.adapters.*`,
`jupyter_workbench.core.*`, `jupyter_client`, or `nbformat`. (Extends
EARS-SEG-32.)

### Dependency boundaries

**EARS-MCT-32**: `tools/preprocess_tools.py` shall depend only on
`processing/preprocess.py` and numpy. No scikit-image or scipy imports
at the tools layer.

**EARS-MCT-33**: `tools/orientation_tools.py` shall depend only on
`processing/orientation.py` and numpy. No scipy imports at the tools
layer.

**EARS-MCT-34**: Point-marker creation in `processing/` shall use only
numpy for sphere generation. Watershed shall use
`skimage.segmentation.watershed` via the existing
`processing/segmentation.py` boundary.

## Acceptance scenarios

### Scenario 1: Bridged-bone separation with point markers

Given a volume where femur and tibia form one connected component
(bridged by osteophytes):

1. `median_filter_volume(volume, iterations=3, size=3)` → filtered volume
2. `threshold_mask(filtered, lower_bound=220)` → bone mask with 1 component
3. Agent renders midsagittal slice via `render_slice(filtered, y_mid,
   plane="sagittal", mask=bone_mask)`
4. Agent examines rendered image, identifies femur center and tibia center coordinates
5. `create_point_markers(shape, [femur_pt, tibia_pt], ["femur", "tibia"],
   radius_voxels=2)` → markers with 2 distinct spheres
6. `watershed_from_markers(filtered, markers, mask=bone_mask)` → labeled
   volume with 2 labels
7. Agent renders labeled result, examines split — both bones correctly separated

This scenario shall produce labeled output where each label's centroid
is in the expected anatomical region and the split boundary falls near
the joint space.

### Scenario 2: Multi-plane seed examination

The agent shall be able to examine anatomy from multiple viewing angles
before placing seeds:

1. `render_slice(volume, z, plane="axial")` → axial view
2. `render_slice(volume, y, plane="sagittal")` → sagittal view showing
   joint space and bone shaft orientation
3. `render_slice(volume, x, plane="coronal")` → coronal view

All three views shall be rendered as PNG files with correct aspect
ratios when spacing is provided.

### Scenario 3: Preprocessing before segmentation

The Amira SOP applies median filter BEFORE threshold:

1. `median_filter_volume(volume, iterations=3, size=3)` → filtered
2. `threshold_mask(filtered, lower_bound=220)` → cleaner mask with
   fewer noise bridges

The filtered volume shall reduce small-scale noise connections that
would otherwise create false bridges between bones.

## Edge cases

- Point marker at volume boundary: sphere clipped to volume bounds
- Two markers with radius_voxels=2 placed 3 voxels apart: overlap
  detected, ValueError raised
- Zero-radius marker (radius_voxels=0): single voxel marker (valid)
- Watershed with empty mask: returns all-zero labeled volume
- Sagittal/coronal rendering with non-square slices: correct aspect ratio
  from spacing
- Orientation applied to empty volume: returns empty volume, no error
- PCA on a flat (2D) mask: third eigenvalue near zero, warning in summary
- `render_slice` with out-of-range index: raises IndexError
