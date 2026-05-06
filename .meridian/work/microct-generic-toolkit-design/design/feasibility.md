# Feasibility Assessment

Related documents:
- [Design overview](overview.md)
- [Architecture](architecture/architecture.md)
- [Behavioral spec](spec/behavioral-spec.md)
- [Refactors](refactors.md)

## Summary

All proposed extensions are technically feasible with the existing
dependency stack (scipy, scikit-image, numpy, matplotlib). No new
external dependencies are needed. The core risk is not technical
feasibility but integration complexity — specifically, the
`slice-examination-loop` skill prompt needs to reliably drive a
visual-examination loop using existing multimodal capabilities.

## Validated by probes

### Point-marker seeds work (p4188, Probe 2)

The smoke-tester created a synthetic bridged volume and demonstrated:
- Current flood-fill seeds: **FAIL** — only 1 label survives, the earlier
  seed is completely overwritten
- Point-marker spheres (radius=2): **PASS** — both labels survive,
  watershed cleanly separates the bridge at the midpoint

The fix is in marker creation only. The existing `watershed_segment()`
function works correctly when given proper markers.

### Multi-plane rendering is straightforward (p4188, Probe 3)

Array slicing for non-axial planes is trivial:
- sagittal: `volume[:, y, :]`
- coronal: `volume[:, :, x]`

Mask overlays work identically to axial. The only complexity is 3D point
projection for edge overlays, which requires filtering points to the
current plane — a few lines of coordinate math.

### Recipe executor cannot carry volumes (p4188, Probe 4)

Confirmed: `run_mask_recipe()` only tracks masks in `step_masks`. Adding
preprocessing as a recipe op would require either:
- Separate volume storage alongside mask storage
- A new recipe model that distinguishes volume outputs from mask outputs

Neither is worth the complexity. The simpler approach: agents call
`median_filter_volume` as a standalone step before entering segmentation
recipes. This matches the Amira SOP flow (filter → threshold → segment)
without overloading the recipe model.

### Median filter already exists (p4185)

`processing/preprocess.py` has a working `median_filter()` with correct
slice-by-slice XY behavior. Exposing it as a tool requires only a thin
wrapper — no new algorithm development.

### Orientation primitives exist (p4185)

`processing/orientation.py` has `pca_orient()`, `center_volume()`, and
`apply_rotation()` — all working and tested. The tool wrapper adds
parameter/summary formatting and coordinate-frame metadata.

## Technical risks

### Risk 1: Slice-examination-loop visual judgment quality

**Level**: MEDIUM

**Concern**: The `slice-examination-loop` skill needs to examine rendered
slices and make anatomical judgments about seed placement. This requires
multimodal image understanding — identifying bone boundaries, joint spaces,
and shaft regions from 2D slice images.

**Mitigation**: The measurer already does this successfully for ML width
measurements via its T1S-Loop (render_axial → read PNG → reason about
anatomy). The examination loop uses the same pattern. The key difference
is that seed placement is more forgiving — a seed anywhere in the interior
of a bone works, whereas measurement requires precise boundary
identification.

**Evidence**: The measurer's T1S-Loop has been validated on OA6-1RK data
and produces results within 10% of published values.

### Risk 2: Memory pressure from concurrent volumes

**Level**: MEDIUM

**Concern**: The examination loop may have multiple large arrays
resident simultaneously:
- Original volume (~128 MB for typical scan)
- Filtered volume (~128 MB)
- Bone mask (~32 MB)
- Markers (~128 MB int32)
- Watershed output (~128 MB)

Total: ~544 MB peak

**Mitigation**: The compute-efficiency resource gate must be enforced
before median_filter and watershed. The agent must `del` rejected
intermediates. The recipe's `keep_intermediates=False` pattern applies
to watershed retry loops too.

### Risk 3: Marker sphere overlap validation

**Level**: LOW

**Concern**: Two seeds placed close together may produce overlapping
marker spheres, which would confuse watershed.

**Mitigation**: EARS-MCT-08 requires overlap validation. The
`create_sphere_markers` function checks for label collisions and raises
a clear error. The agent must space seeds at least 2*radius+1 voxels
apart.

### Risk 4: Coordinate frame confusion

**Level**: LOW

**Concern**: If orientation is applied before seed placement, the agent
must track which coordinate frame seed positions are in.

**Mitigation**: EARS-MCT-23 requires `orient_volume` to return transform
metadata. EARS-MCT-25 requires seed evidence to record whether
orientation was applied. The skill specifies that the coordinate frame
must be stated explicitly.

## Library version constraints

| Library | Required | Current in pyproject.toml | Notes |
| --- | --- | --- | --- |
| scipy | ≥1.10 | existing | `ndimage.median_filter`, `ndimage.label` |
| scikit-image | ≥0.21 | existing | `segmentation.watershed`, `measure.marching_cubes` |
| numpy | ≥1.24 | existing | array operations |
| matplotlib | ≥3.7 | existing | slice rendering |

No new libraries needed.

## Effort estimate

| Component | Scope | Effort |
| --- | --- | --- |
| `tools/preprocess_tools.py` | 1 function + tests | Small |
| `tools/orientation_tools.py` | 2-3 functions + tests | Small |
| `create_sphere_markers` in processing | 1 function + tests | Small |
| `create_point_markers` + `watershed_from_markers` in tools | 2 functions + tests | Small |
| `render_slice` in slice_renderer | 1 function + tests | Small |
| `inspect_plane` in slice_inspector | 1 function + tests | Small |
| `slice-examination-loop` skill | ~150 lines | Medium |
| Segmenter prompt update (R5) | ~30 lines changed | Small |
| Compute-efficiency budget updates (R4) | ~20 lines added | Small |
| Acceptance test: bridged-bone scenario | 1 test | Small |

Total: 5-7 implementation phases, each independently testable.

## Conclusion

The design is feasible. The hardest part is not code — it is prompt
engineering for the `slice-examination-loop` skill. The tool-layer work
is straightforward wrapping of existing processing primitives. The
critical validation target is the bridged-bone acceptance scenario on
real OA6-1RK data, where the current pipeline fails and the new
point-marker seeds should succeed.
