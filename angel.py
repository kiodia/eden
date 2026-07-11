'''
Angel - a user persona's guardian Angel living in the Garden of Eden.

An Angel operates on top of Hermes-Agent under the Linda coordination
principle: it never talks to other Angels directly, it coordinates through
the Eden tuple space (write / read / take / notify on Kanban cards).

The Angel's behaviour is defined by a skill following the agentskills.io
standard, exactly as Hermes-Agent implements it: a directory with a required
``SKILL.md`` (YAML frontmatter + instructions) plus optional ``scripts/``,
``references/`` and ``assets/`` directories.

For robust transport between agents (over the Eden space, HTTP, or disk) a
skill can be packed into a ``skill-bundle/v1`` envelope: a self-describing
JSON document with metadata, an explicit entrypoint, and a SHA-256 checksum
per file, verified on load.

@author: vankomme
'''

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
import hashlib
import json

SKILL_BUNDLE_SCHEMA = "skill-bundle/v1"


def sha256_of(content: str) -> str:
    """Hex SHA-256 of a file's textual content (UTF-8)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class AngelSkill(BaseModel):
    """A skill following the agentskills.io standard (https://agentskills.io),
    as implemented by Hermes-Agent.

    On disk a skill is a directory ``my-skill/`` with a required ``SKILL.md``
    (metadata + instructions) plus optional ``scripts/``, ``references/`` and
    ``assets/`` directories. Each optional directory is stored as a mapping of
    relative file path to its textual contents.
    """
    name: str = Field(..., description="The skill directory name, e.g. 'guardian-angel'")
    skill_md: str = Field(
        ...,
        description="Contents of the required SKILL.md file (YAML frontmatter + instructions)"
    )
    scripts: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional executable code, mapping relative file path to contents"
    )
    references: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional documentation, mapping relative file path to contents"
    )
    assets: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional templates/resources, mapping relative file path to contents"
    )
    additional_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Any additional files or directories, mapping relative file path to contents"
    )


# ---------------------------------------------------------------------- #
# skill-bundle/v1: integrity-checked transport envelope for a skill
# ---------------------------------------------------------------------- #

class BundleFile(BaseModel):
    """One file inside a skill bundle, carried with its SHA-256 checksum."""
    path: str = Field(..., description="Relative path inside the skill directory, e.g. 'scripts/check.sh'")
    sha256: str = Field(..., description="Hex SHA-256 of the UTF-8 encoded content")
    content: str = Field(..., description="The file's textual content")

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        """Normalize to forward slashes and reject anything that could
        escape the skill directory when written to disk."""
        path = value.replace("\\", "/")
        parts = path.split("/")
        if not path or path.startswith("/") or ":" in path or ".." in parts or "" in parts:
            raise ValueError(f"unsafe file path in bundle: '{value}'")
        return path


class BundleMetadata(BaseModel):
    """Descriptive metadata of a skill bundle."""
    name: str = Field(..., description="Skill name, e.g. 'deploy-k8s'")
    version: str = Field(default="1.0.0", description="Semantic version of the skill")
    description: str = Field(default="", description="One-line summary of the skill")
    tags: List[str] = Field(default_factory=list, description="Free-form tags, e.g. ['kubernetes', 'devops']")


class SkillBundle(BaseModel):
    """A ``skill-bundle/v1`` envelope:

    ```json
    {
      "schema": "skill-bundle/v1",
      "metadata": {"name": "deploy-k8s", "version": "1.2.0",
                   "description": "...", "tags": ["kubernetes", "devops"]},
      "entrypoint": "SKILL.md",
      "files": [{"path": "SKILL.md", "sha256": "...", "content": "..."}]
    }
    ```

    Validation is strict at parse time: the schema identifier must match,
    the entrypoint must be one of the files, paths must be unique and safe,
    and every file's SHA-256 must match its content - a tampered or corrupted
    bundle is rejected before an Angel is ever built from it.

    Serialize with ``bundle.dump()`` / ``bundle.dump_json()`` so the field
    is emitted under its wire name ``schema``.
    """
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default=SKILL_BUNDLE_SCHEMA, alias="schema",
                         description="Bundle format identifier, must be 'skill-bundle/v1'")
    metadata: BundleMetadata = Field(..., description="Descriptive metadata of the skill")
    entrypoint: str = Field(default="SKILL.md",
                            description="Path of the file Hermes-Agent starts from (must be in files)")
    files: List[BundleFile] = Field(..., description="All files of the skill, each with its checksum")

    @model_validator(mode="after")
    def validate_bundle(self) -> "SkillBundle":
        if self.schema_ != SKILL_BUNDLE_SCHEMA:
            raise ValueError(f"unsupported bundle schema '{self.schema_}', expected '{SKILL_BUNDLE_SCHEMA}'")
        paths = [f.path for f in self.files]
        duplicates = {p for p in paths if paths.count(p) > 1}
        if duplicates:
            raise ValueError(f"duplicate file paths in bundle: {sorted(duplicates)}")
        if self.entrypoint not in paths:
            raise ValueError(f"entrypoint '{self.entrypoint}' is not among the bundle files")
        for f in self.files:
            actual = sha256_of(f.content)
            if actual != f.sha256:
                raise ValueError(
                    f"sha256 mismatch for '{f.path}': expected {f.sha256}, got {actual} - bundle corrupted or tampered"
                )
        return self

    @classmethod
    def build(cls, metadata: BundleMetadata, files: Dict[str, str],
              entrypoint: str = "SKILL.md") -> "SkillBundle":
        """Create a bundle from plain {path: content} files, computing the
        SHA-256 checksums."""
        return cls(
            metadata=metadata,
            entrypoint=entrypoint,
            files=[BundleFile(path=path, sha256=sha256_of(content), content=content)
                   for path, content in sorted(files.items())],
        )

    def dump(self) -> dict:
        """Wire-format dict (with the 'schema' key)."""
        return self.model_dump(by_alias=True)

    def dump_json(self, **kwargs) -> str:
        """Wire-format JSON string (with the 'schema' key)."""
        return self.model_dump_json(by_alias=True, **kwargs)


def parse_frontmatter(skill_md: str) -> Tuple[Dict[str, str], str]:
    """Split a SKILL.md into its YAML frontmatter (flat key: value pairs)
    and the instruction body. Returns ({}, full text) when there is none."""
    meta: Dict[str, str] = {}
    body = skill_md
    if skill_md.lstrip().startswith("---"):
        parts = skill_md.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def parse_tags(value: Optional[str]) -> List[str]:
    """Parse a frontmatter tags value like 'kubernetes, devops' or
    '[kubernetes, devops]' into a list of tags."""
    if not value:
        return []
    return [t.strip().strip("'\"") for t in value.strip("[]").split(",") if t.strip()]


class Angel(BaseModel):
    """A guardian Angel: the agent God places in the Garden to serve one
    user persona. Its instructions live in an agentskills.io SKILL.md."""
    name: str = Field(..., description="Unique name of the Angel")
    persona: Optional[str] = Field(
        None,
        description="The user persona this Angel guards (from the SKILL.md frontmatter)"
    )
    description: str = Field(
        default="",
        description="One-line summary of the Angel (from the SKILL.md frontmatter)"
    )
    version: str = Field(
        default="1.0.0",
        description="Semantic version of the Angel's skill"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Free-form tags describing the Angel's skill"
    )
    skill: AngelSkill = Field(
        ...,
        description="The agentskills.io skill (SKILL.md and optional resources) that drives this Angel on Hermes-Agent"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "guardian-angel",
                "persona": "researcher",
                "description": "Watches the Eden board and claims task cards for its persona",
                "version": "1.0.0",
                "tags": ["eden", "kanban"],
                "skill": {
                    "name": "guardian-angel",
                    "skill_md": "---\nname: guardian-angel\ndescription: Watches the Eden board\npersona: researcher\n---\n\n# Guardian Angel\n\nTake task cards from the Eden space and work them."
                }
            }
        }

    @property
    def instructions(self) -> str:
        """The SKILL.md body without the frontmatter - what Hermes-Agent executes."""
        _, body = parse_frontmatter(self.skill.skill_md)
        return body

    @classmethod
    def from_skill_dir(cls, path) -> "Angel":
        """Load an Angel from an on-disk agentskills.io skill directory
        (e.g. ``assets/guardian-angel/``). SKILL.md is required; scripts/,
        references/ and assets/ are picked up when present."""
        skill_dir = Path(path)
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(skill_md)

        def load_dir(sub: str) -> Dict[str, str]:
            d = skill_dir / sub
            if not d.is_dir():
                return {}
            return {
                f.relative_to(d).as_posix(): f.read_text(encoding="utf-8")
                for f in sorted(d.rglob("*")) if f.is_file()
            }

        skill = AngelSkill(
            name=meta.get("name", skill_dir.name),
            skill_md=skill_md,
            scripts=load_dir("scripts"),
            references=load_dir("references"),
            assets=load_dir("assets"),
        )
        return cls(
            name=meta.get("name", skill_dir.name),
            persona=meta.get("persona"),
            description=meta.get("description", ""),
            version=meta.get("version", "1.0.0"),
            tags=parse_tags(meta.get("tags")),
            skill=skill,
        )

    # ------------------------------------------------------------------ #
    # skill-bundle/v1 transport
    # ------------------------------------------------------------------ #

    def to_bundle(self) -> SkillBundle:
        """Pack the Angel's skill into a ``skill-bundle/v1`` envelope with a
        SHA-256 checksum per file, ready to be shipped between agents."""
        files: Dict[str, str] = {"SKILL.md": self.skill.skill_md}
        for sub, mapping in (("scripts", self.skill.scripts),
                             ("references", self.skill.references),
                             ("assets", self.skill.assets)):
            for rel_path, content in mapping.items():
                files[f"{sub}/{rel_path}"] = content
        for rel_path, content in self.skill.additional_files.items():
            files.setdefault(rel_path, content)
        return SkillBundle.build(
            metadata=BundleMetadata(
                name=self.name,
                version=self.version,
                description=self.description,
                tags=self.tags,
            ),
            files=files,
            entrypoint="SKILL.md",
        )

    @classmethod
    def from_bundle(cls, bundle: Union["SkillBundle", dict, str]) -> "Angel":
        """Unpack an Angel from a ``skill-bundle/v1`` envelope (model, dict
        or JSON string). The bundle is fully validated first - schema
        identifier, entrypoint, safe unique paths, SHA-256 of every file -
        so a corrupted or tampered bundle never becomes an Angel."""
        if isinstance(bundle, str):
            bundle = json.loads(bundle)
        if isinstance(bundle, dict):
            bundle = SkillBundle.model_validate(bundle)

        entry = next(f for f in bundle.files if f.path == bundle.entrypoint)
        meta, _ = parse_frontmatter(entry.content)

        scripts: Dict[str, str] = {}
        references: Dict[str, str] = {}
        assets: Dict[str, str] = {}
        additional: Dict[str, str] = {}
        buckets = {"scripts": scripts, "references": references, "assets": assets}
        for f in bundle.files:
            if f.path == bundle.entrypoint:
                continue
            top, _, rest = f.path.partition("/")
            if rest and top in buckets:
                buckets[top][rest] = f.content
            else:
                additional[f.path] = f.content

        skill = AngelSkill(
            name=bundle.metadata.name,
            skill_md=entry.content,
            scripts=scripts,
            references=references,
            assets=assets,
            additional_files=additional,
        )
        return cls(
            name=bundle.metadata.name,
            persona=meta.get("persona"),
            description=bundle.metadata.description or meta.get("description", ""),
            version=bundle.metadata.version,
            tags=bundle.metadata.tags,
            skill=skill,
        )

    def to_card_fields(self) -> dict:
        """Serialize the Angel into the fields of a Kanban card, so it can be
        written into the Eden space (kind='angel') and discovered by templates
        like {"kind": "angel", "fields": {"persona": "researcher"}}.
        The skill travels as an integrity-checked skill-bundle/v1."""
        return {
            "angel": self.name,
            "persona": self.persona,
            "description": self.description,
            "version": self.version,
            "tags": self.tags,
            "skill_bundle": self.to_bundle().dump(),
        }

    @classmethod
    def from_card_fields(cls, fields: dict) -> "Angel":
        """Rebuild an Angel from Kanban card fields written by
        ``to_card_fields`` (the embedded bundle is verified)."""
        return cls.from_bundle(fields["skill_bundle"])
