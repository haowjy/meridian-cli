"""Unit tests for multi-path `meridian qi graph`."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.cli import qi_cmd


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_collect_qi_graph_results_dedupes_shared_boundary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    alpha = root / "alpha"
    _write(alpha / "AGENTS.md", "# Alpha\n")
    (alpha / "nested").mkdir()

    results = qi_cmd.collect_qi_graph_results(
        [alpha / "nested" / "a.txt", alpha / "nested" / "b.txt"],
        root,
    )

    assert len(results) == 1
    assert results[0].boundary_path == "alpha"


def test_format_qi_graph_text_single_result_has_no_header(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    sub = root / "sub"
    root.mkdir()
    sub.mkdir()
    _write(sub / "AGENTS.md", "# Agents\n")

    results = qi_cmd.collect_qi_graph_results([sub], root)
    text = qi_cmd.format_qi_graph_text(results)

    assert text.startswith("# sub/AGENTS.md")
    assert "\n\n# sub\n" not in text


def test_format_qi_graph_text_multiple_results_include_headers(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    alpha = root / "alpha"
    beta = root / "beta"
    _write(alpha / "AGENTS.md", "# Alpha\n")
    _write(beta / "AGENTS.md", "# Beta\n")

    results = qi_cmd.collect_qi_graph_results([alpha, beta], root)
    text = qi_cmd.format_qi_graph_text(results)

    assert text.startswith("# alpha\n")
    assert "# beta\n" in text
    assert "# alpha/AGENTS.md" in text
    assert "# beta/AGENTS.md" in text


def test_cmd_qi_graph_json_output_is_list(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    alpha = root / "alpha"
    beta = root / "beta"
    _write(alpha / "AGENTS.md", "# Alpha\n")
    _write(beta / "AGENTS.md", "# Beta\n")

    results = qi_cmd.collect_qi_graph_results([alpha, beta], root)
    payload = [result.model_dump() for result in results]

    assert len(payload) == 2
    assert {entry["boundary_path"] for entry in payload} == {"alpha", "beta"}
    decoded = json.loads(json.dumps(payload, indent=2))
    assert isinstance(decoded, list)
