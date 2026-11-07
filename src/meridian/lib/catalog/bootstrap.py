from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from meridian.lib.launch.composition import PromptDocument


class BootstrapTier(StrEnum):
    SKILL = "skill"
    PACKAGE = "package"


@dataclass(frozen=True)
class BootstrapRecord:
    logical_name: str
    tier: BootstrapTier
    path: Path

    def to_prompt_document(self) -> PromptDocument:
        content = self.path.read_text(encoding="utf-8").strip()
        if self.tier == BootstrapTier.SKILL:
            attributed = f"# Bootstrap: {self.logical_name}\n\n{content}"
        else:
            attributed = f"# Bootstrap: {self.logical_name} (package)\n\n{content}"
        return PromptDocument(
            kind="bootstrap",
            logical_name=self.logical_name,
            path=self.path.resolve().as_posix(),
            content=attributed,
        )


class BootstrapRegistry:
    """Discover bootstrap docs from installed .mars content."""

    def __init__(self, mars_root: Path) -> None:
        self._mars_root = mars_root

    @property
    def mars_root(self) -> Path:
        return self._mars_root

    def discover_skill_bootstrap_docs(self) -> tuple[BootstrapRecord, ...]:
        skills_dir = self._mars_root / "skills"
        if not skills_dir.is_dir():
            return ()
        records = [
            BootstrapRecord(
                logical_name=skill_dir.name,
                tier=BootstrapTier.SKILL,
                path=bootstrap_path,
            )
            for skill_dir in skills_dir.iterdir()
            if skill_dir.is_dir()
            for bootstrap_path in (skill_dir / "resources" / "BOOTSTRAP.md",)
            if bootstrap_path.is_file()
        ]
        return tuple(sorted(records, key=lambda record: record.logical_name))

    def discover_package_bootstrap_docs(self) -> tuple[BootstrapRecord, ...]:
        bootstrap_dir = self._mars_root / "bootstrap"
        if not bootstrap_dir.is_dir():
            return ()
        records = [
            BootstrapRecord(
                logical_name=doc_dir.name,
                tier=BootstrapTier.PACKAGE,
                path=bootstrap_path,
            )
            for doc_dir in bootstrap_dir.iterdir()
            if doc_dir.is_dir()
            for bootstrap_path in (doc_dir / "BOOTSTRAP.md",)
            if bootstrap_path.is_file()
        ]
        return tuple(sorted(records, key=lambda record: record.logical_name))

    def load_all(self) -> tuple[PromptDocument, ...]:
        return tuple(
            record.to_prompt_document()
            for record in (
                *self.discover_skill_bootstrap_docs(),
                *self.discover_package_bootstrap_docs(),
            )
        )


__all__ = ["BootstrapRecord", "BootstrapRegistry", "BootstrapTier"]
