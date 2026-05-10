"""SKILL.md parsing, scanning, and filesystem-backed skill catalog."""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.domain import IndexReport, SkillContent, SkillManifest

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Skill document model
# ---------------------------------------------------------------------------


class SkillDocument(BaseModel):
    """Parsed representation of one SKILL.md file."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    path: Path
    skill_type: str = "reference"
    content: str
    body: str
    frontmatter: dict[str, object]


class SkillRecord(BaseModel):
    """Indexed root skill and its lazily discovered variant documents."""

    model_config = ConfigDict(frozen=True)

    base: SkillDocument
    variant_index: dict[tuple[str, str | None], Path] | None = None

    @property
    def root_dir(self) -> Path:
        return self.base.path.parent

    def with_variant_index(self) -> "SkillRecord":
        if self.variant_index is not None:
            return self
        return self.model_copy(update={"variant_index": discover_skill_variants(self.root_dir)})


# ---------------------------------------------------------------------------
# Markdown frontmatter parsing
# ---------------------------------------------------------------------------


def split_markdown_frontmatter(markdown: str) -> tuple[dict[str, object], str]:
    """Split markdown into YAML frontmatter and body."""
    import frontmatter  # type: ignore[import-untyped]
    import yaml

    try:
        post = frontmatter.loads(markdown)
    except yaml.YAMLError:
        logger.warning("Malformed YAML frontmatter, treating as plain markdown")
        return {}, markdown
    return dict(post.metadata), post.content


def parse_skill_file(path: Path) -> SkillDocument:
    """Parse one SKILL.md file."""

    content = path.read_text(encoding="utf-8")
    frontmatter, body = split_markdown_frontmatter(content)

    name_value = frontmatter.get("name")
    description_value = frontmatter.get("description")
    skill_type = str(frontmatter.get("type", "reference")).strip()
    name = str(name_value).strip() if name_value is not None else path.parent.name
    description = str(description_value).strip() if description_value is not None else ""

    return SkillDocument(
        name=name or path.parent.name,
        description=description,
        path=path.resolve(),
        skill_type=skill_type or "reference",
        content=content,
        body=body,
        frontmatter=frontmatter,
    )


def discover_skill_files(skills_dir: Path) -> list[Path]:
    """Discover top-level skill documents under `.mars/skills/`.

    Skill variants live below each skill root and must not be indexed as
    standalone skills. Discovery therefore treats each immediate child
    directory as one possible skill root and only returns its root
    ``SKILL.md``.
    """

    if not skills_dir.is_dir():
        return []
    return sorted(
        skill_file
        for skill_file in (
            child / "SKILL.md" for child in skills_dir.iterdir() if child.is_dir()
        )
        if skill_file.is_file()
    )


def discover_skill_variants(skill_root: Path) -> dict[tuple[str, str | None], Path]:
    """Discover variant SKILL.md files for one skill root.

    Keys are ``(harness, model)`` for model variants and ``(harness, None)``
    for harness variants. Directory names are preserved exactly so runtime
    matching remains exact-only.
    """

    variants_dir = skill_root / "variants"
    if not variants_dir.is_dir():
        return {}

    variants: dict[tuple[str, str | None], Path] = {}
    for harness_dir in sorted(
        (path for path in variants_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    ):
        harness = harness_dir.name
        harness_skill = harness_dir / "SKILL.md"
        if harness_skill.is_file():
            variants[(harness, None)] = harness_skill
        for model_dir in sorted(
            (path for path in harness_dir.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        ):
            model_skill = model_dir / "SKILL.md"
            if model_skill.is_file():
                variants[(harness, model_dir.name)] = model_skill
    return variants


def _skill_search_dirs(project_root: Path) -> list[Path]:
    return [project_root / ".mars" / "skills"]


def replace_skill_body(base: SkillDocument, body: str) -> str:
    """Return base document content with its body replaced, preserving frontmatter bytes."""

    if base.body and base.content.endswith(base.body):
        return f"{base.content[: -len(base.body)]}{body}"
    if base.content.startswith("---\n"):
        frontmatter_end = base.content.find("\n---", 3)
        if frontmatter_end != -1:
            body_start = frontmatter_end + len("\n---")
            while body_start < len(base.content) and base.content[body_start] in "\r\n":
                body_start += 1
            return f"{base.content[:body_start]}{body}"
    if not base.body:
        return f"{base.content}{body}"
    logger.warning(
        "Could not isolate frontmatter prefix for skill '%s';"
        " using variant body without base prefix",
        base.name,
    )
    return body


def files_have_equal_text(first: Path, second: Path) -> bool:
    try:
        return first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    except OSError:
        return False


def scan_skills(
    project_root: Path | None = None,
    skills_dirs: list[Path] | None = None,
) -> list[SkillDocument]:
    """Scan configured skill directories and parse all discovered skills."""

    root = resolve_project_root_resolution(project_root).project_root
    directories = skills_dirs if skills_dirs is not None else _skill_search_dirs(root)
    documents: list[SkillDocument] = []
    selected_by_name: dict[str, SkillDocument] = {}

    for directory in directories:
        for path in discover_skill_files(directory):
            document = parse_skill_file(path)
            existing = selected_by_name.get(document.name)
            if existing is not None:
                if files_have_equal_text(existing.path, document.path):
                    continue
                logger.warning(
                    "Skill '%s' found in multiple paths with conflicting content: %s, %s. "
                    "Using %s; conflicting duplicate ignored.",
                    document.name,
                    existing.path,
                    document.path,
                    existing.path,
                )
                continue
            selected_by_name[document.name] = document
            documents.append(document)
    return documents


# ---------------------------------------------------------------------------
# Skill registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Skill catalog with discovery from configured skill directories."""

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        readonly: bool = False,
    ) -> None:
        self._project_root = resolve_project_root_resolution(project_root).project_root
        self._skills_dirs = tuple(_skill_search_dirs(self._project_root))
        self._readonly = readonly
        self._filesystem_documents: tuple[SkillDocument, ...] | None = None
        self._filesystem_records: tuple[SkillRecord, ...] | None = None

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def skills_dirs(self) -> tuple[Path, ...]:
        return self._skills_dirs

    @property
    def readonly(self) -> bool:
        return self._readonly

    def _scan_documents(self, *, refresh: bool = False) -> tuple[SkillDocument, ...]:
        if self._filesystem_records is None or refresh:
            documents = tuple(
                scan_skills(self._project_root, skills_dirs=list(self._skills_dirs))
            )
            self._filesystem_documents = documents
            self._filesystem_records = tuple(SkillRecord(base=document) for document in documents)
        elif self._filesystem_documents is None:
            self._filesystem_documents = tuple(record.base for record in self._filesystem_records)
        return self._filesystem_documents

    def _scan_records(self, *, refresh: bool = False) -> tuple[SkillRecord, ...]:
        if self._filesystem_records is None or refresh:
            self._scan_documents(refresh=refresh)
        assert self._filesystem_records is not None
        return self._filesystem_records

    def _replace_record(self, updated: SkillRecord) -> None:
        records = self._scan_records()
        self._filesystem_records = tuple(
            updated if record.base.name == updated.base.name else record for record in records
        )
        self._filesystem_documents = tuple(record.base for record in self._filesystem_records)

    def reindex(self, skills_dir: Path | None = None) -> IndexReport:
        """Refresh in-memory index from configured skill search directories."""

        scan_dirs: list[Path]
        if skills_dir is not None:
            requested = skills_dir.resolve()
            if requested not in self._skills_dirs:
                expected = ", ".join(path.as_posix() for path in self._skills_dirs)
                expected_text = expected if expected else "<none>"
                raise ValueError(
                    "Skill discovery is restricted to repo-local installed skill paths; "
                    f"expected one of '{expected_text}', got '{skills_dir}'."
                )
            scan_dirs = [requested]
        else:
            scan_dirs = list(self._skills_dirs)

        documents = tuple(scan_skills(self._project_root, skills_dirs=scan_dirs))
        self._filesystem_documents = documents
        self._filesystem_records = tuple(SkillRecord(base=document) for document in documents)
        return IndexReport(indexed_count=len(documents))

    def list_skills(self) -> list[SkillManifest]:
        """List all discovered skills."""

        return sorted(
            [
                SkillManifest(
                    name=document.name,
                    description=document.description,
                    path=str(document.path),
                )
                for document in self._scan_documents()
            ],
            key=lambda item: item.name,
        )

    def _select_document(
        self,
        record: SkillRecord,
        *,
        harness_id: str | None,
        selected_model_token: str | None,
        canonical_model_id: str | None,
    ) -> SkillDocument:
        harness = (harness_id or "").strip()
        if not harness:
            return record.base

        indexed = record.with_variant_index()
        if indexed is not record:
            self._replace_record(indexed)
        variant_index = indexed.variant_index or {}
        candidates = [
            (harness, (selected_model_token or "").strip()),
            (harness, (canonical_model_id or "").strip()),
            (harness, None),
        ]
        for candidate_harness, candidate_model in candidates:
            key = (candidate_harness, candidate_model or None)
            path = variant_index.get(key)
            if path is not None:
                variant = parse_skill_file(path)
                content = replace_skill_body(record.base, variant.body)
                return SkillDocument(
                    name=record.base.name,
                    description=record.base.description,
                    path=variant.path,
                    skill_type=record.base.skill_type,
                    content=content,
                    body=variant.body,
                    frontmatter=record.base.frontmatter,
                )
        return record.base

    def load(
        self,
        names: list[str],
        *,
        harness_id: str | None = None,
        selected_model_token: str | None = None,
        canonical_model_id: str | None = None,
    ) -> list[SkillContent]:
        """Load full SKILL.md content for specific skill names in requested order."""

        normalized_names = [name.strip() for name in names if name.strip()]
        if not normalized_names:
            return []

        records_by_name = {record.base.name: record for record in self._scan_records()}
        missing = [name for name in normalized_names if name not in records_by_name]
        if missing:
            raise KeyError(f"Unknown skills: {', '.join(missing)}")

        loaded: list[SkillContent] = []
        for name in normalized_names:
            document = self._select_document(
                records_by_name[name],
                harness_id=harness_id,
                selected_model_token=selected_model_token,
                canonical_model_id=canonical_model_id,
            )
            loaded.append(
                SkillContent(
                    name=document.name,
                    description=document.description,
                    content=document.content,
                    path=str(document.path),
                    skill_type=document.skill_type,
                )
            )
        return loaded

    def show(self, name: str) -> SkillContent:
        """Load one skill content payload by name."""

        loaded = self.load([name])
        return loaded[0]
