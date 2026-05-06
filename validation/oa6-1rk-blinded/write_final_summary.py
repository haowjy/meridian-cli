import json

summary = {
    "session_id": session_id,
    "dicom_path": dicom_path,
    "workflow_id": workflow["workflow_id"],
    "derived_notebook_path": ".jupyter-workbench/sessions/oa6-blinded/notebooks/derived_1.ipynb",
    "stage_confidence": {
        "segmentation": segmentation_report["confidence"],
        "landmarks_orientation": landmarks_report["confidence"],
        "roi": roi_report["confidence"],
        "measurement": measurement_report["confidence"],
    },
    "flags": {
        "segmentation": segmentation_report.get("flags", []),
        "roi": ["fallback anchors/visual judgment boundaries recorded"] if roi_report["confidence"] == "medium" else [],
    },
    "artifact_paths": {
        "intake_metadata": str(session_dir / "intake" / "volume_metadata.json"),
        "segmentation_report": str(session_dir / "segmentation" / "stage_report.json"),
        "segmentation_trace": str(session_dir / "segmentation" / "segmentation_trace.json"),
        "landmark_report": str(session_dir / "landmarks" / "stage_report.json"),
        "roi_report": str(session_dir / "roi" / "stage_report.json"),
        "measurement_results": str(session_dir / "measurements" / "results.json"),
        "measurement_summary": str(session_dir / "measurements" / "summary.md"),
        "qc_overlays": str(session_dir / "measurements" / "qc_overlays.json"),
        "overrides": str(session_dir / "measurements" / "overrides.json"),
        "agent_width_evidence": str(session_dir / "measurements" / "distal_femoral_width_agent_selection.json"),
    },
    "promotion_suggestions": [],
    "promotion_note": "No prior workflow run history was available in this fixture path; no override streak promotion evaluated beyond current run.",
}

(session_dir / "final_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

lines = [
    "# OA6-1RK Blinded MicroCT Analysis",
    "",
    f"Session: `{session_id}`",
    f"Workflow: `{workflow['workflow_id']}`",
    f"Derived notebook: `{summary['derived_notebook_path']}`",
    "",
    "## Stage Confidence",
    "",
]
for stage, confidence in summary["stage_confidence"].items():
    lines.append(f"- {stage}: {confidence}")
lines.extend(["", "## Key Artifacts", ""])
for name, path in summary["artifact_paths"].items():
    lines.append(f"- {name}: `{path}`")
lines.extend(
    [
        "",
        "## Flags",
        "",
        "- Segmentation used custom connected-component recovery at threshold 5000 after watershed seed failure; see segmentation_trace.json.",
        "- ROI confidence medium because workflow ROI definitions include operator-judgment/fallback anchoring.",
        "- No screenshots were produced because visualization was absent in this workbench session.",
        "",
    ]
)
(session_dir / "final_summary.md").write_text("\n".join(lines))

print(json.dumps({"final_summary": str(session_dir / "final_summary.md"), "derived_notebook": summary["derived_notebook_path"]}, sort_keys=True))
