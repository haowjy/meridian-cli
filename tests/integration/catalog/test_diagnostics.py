"""Integration test for diagnostics + agent inventory prompt.

Verifies that library warnings emitted during catalog scanning do not leak to
stderr when capture_library_diagnostics() wraps the call. Requires real
filesystem I/O (writes a legacy agent profile to disk).

# qa-validated: test-suite-redesign
"""

import logging
from pathlib import Path

from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.launch.prompt import build_agent_inventory_prompt


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _write_legacy_profile(project_root: Path) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "legacy.md").write_text(
        "\n".join(
            [
                "---",
                "name: Legacy",
                "models:",
                "  gpt55:",
                "    effort: low",
                "---",
                "",
                "Profile body.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_build_launch_context_does_not_leak_library_warnings_to_stderr(
    tmp_path: Path,
) -> None:
    """Structural guard: library warnings during launch must not reach stderr."""

    _write_legacy_profile(tmp_path)
    handler = _CapturingHandler()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.WARNING)
    try:
        build_agent_inventory_prompt(project_root=tmp_path)

        assert any(
            record.name.startswith("meridian.lib")
            and record.levelno == logging.WARNING
            and "uses legacy models" in record.getMessage()
            for record in handler.records
        )

        handler.records.clear()
        with capture_library_diagnostics() as diag:
            build_agent_inventory_prompt(project_root=tmp_path)

        assert any(
            record.name.startswith("meridian.lib")
            and record.levelno == logging.WARNING
            and "uses legacy models" in record.getMessage()
            for record in diag.records
        )
        assert not [
            record
            for record in handler.records
            if record.name.startswith("meridian.lib") and record.levelno == logging.WARNING
        ]
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(original_level)
