'''
Angel - a user persona's guardian Angel living in the Garden of Eden.

An Angel operates on top of Hermes-Agent under the Linda coordination
principle: it never talks to other Angels directly, it coordinates through
the Eden tuple space (write / read / take / notify on Kanban cards).

The Angel's behaviour is defined by a skill following the agentskills.io
standard, exactly as Hermes-Agent implements it: a directory with a required
``SKILL.md`` (YAML frontmatter + instructions) plus optional ``scripts/``,
``references/`` and ``assets/`` directories.

@author: vankomme
'''

from pydantic import BaseModel, Field
from pathlib import Path
from typing import Dict, Optional, Tuple


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
            skill=skill,
        )

    def to_card_fields(self) -> dict:
        """Serialize the Angel into the fields of a Kanban card, so it can be
        written into the Eden space (kind='angel') and discovered by templates
        like {"kind": "angel", "fields": {"persona": "researcher"}}."""
        return {
            "angel": self.name,
            "persona": self.persona,
            "description": self.description,
            "skill": self.skill.model_dump(),
        }
