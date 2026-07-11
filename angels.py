'''
Angels - the Host: a multi-agent system of guardian Angels.

Builds on the Angel class to assemble many Angels into one coordinated
system. Following the Linda principle the members never reference each
other: every skill only declares *dependencies* on the shared Eden board
(which Kanban cards it consumes or produces, and where it stands in a
workflow), and the tuple space wires the system together at runtime.

A useful pattern applied here is the separation of a skill's *identity*
from its *serialization*:

    Skill (identity)                        SkillBundle (serialization)
     |-- metadata                            skill-bundle/v1 JSON envelope
     |-- entrypoint (SKILL.md)               with SHA-256 per file,
     |-- resources                           produced by Skill.to_bundle()
     |-- helper scripts                      and verified again by
     `-- dependencies                        Skill.from_bundle()
          |-- Kanban card assignments
          `-- workflow assignments

@author: vankomme
'''

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple
import json

from angel import (
    Angel, AngelSkill, BundleMetadata, SkillBundle, parse_frontmatter
)

CONSUME = "consume"   # the skill take()s cards of this kind from the board
PRODUCE = "produce"   # the skill write()s cards of this kind onto the board

DEPENDENCIES_FILE = "dependencies.json"
DEPENDENCIES_SCHEMA = "skill-dependencies/v1"


class SkillDependency(BaseModel):
    """A dependency of a skill on the Eden board: a Kanban card assignment
    (which cards it consumes or produces) plus its workflow assignment
    (where that exchange sits in a named workflow)."""
    kind: str = Field(..., description="Kanban card kind this dependency binds to, e.g. 'task'")
    action: str = Field(..., pattern=f"^({CONSUME}|{PRODUCE})$",
                        description="'consume' = take matching cards, 'produce' = write them")
    fields: Dict[str, Any] = Field(default_factory=dict,
                                   description="Template fields to match (consume) or stamp (produce)")
    workflow: Optional[str] = Field(None, description="Name of the workflow this exchange belongs to")
    step: Optional[int] = Field(None, ge=1, description="Position of this exchange in the workflow")


class Skill(BaseModel):
    """The *identity* of a skill: what it is and what it needs, independent
    of how it is shipped. Serialization is delegated to skill-bundle/v1
    (``to_bundle`` / ``from_bundle``); execution shape is delegated to the
    Angel/AngelSkill pair (``to_angel`` / ``from_angel``)."""
    metadata: BundleMetadata = Field(..., description="Name, version, description and tags of the skill")
    entrypoint: str = Field(..., description="Contents of SKILL.md (YAML frontmatter + instructions)")
    resources: Dict[str, str] = Field(
        default_factory=dict,
        description="Documentation, templates and other resources, mapping relative path to contents"
    )
    scripts: Dict[str, str] = Field(
        default_factory=dict,
        description="Helper scripts, mapping relative path (inside scripts/) to contents"
    )
    dependencies: List[SkillDependency] = Field(
        default_factory=list,
        description="Kanban card and workflow assignments this skill relies on"
    )

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def instructions(self) -> str:
        """The SKILL.md body without the frontmatter - what Hermes-Agent executes."""
        _, body = parse_frontmatter(self.entrypoint)
        return body

    def consumes(self) -> List[SkillDependency]:
        return [d for d in self.dependencies if d.action == CONSUME]

    def produces(self) -> List[SkillDependency]:
        return [d for d in self.dependencies if d.action == PRODUCE]

    # ------------------------------------------------------------------ #
    # identity <-> execution (Angel)
    # ------------------------------------------------------------------ #

    @classmethod
    def from_angel(cls, angel: Angel,
                   dependencies: Optional[List[SkillDependency]] = None) -> "Skill":
        """Extract the identity of an Angel's skill, optionally attaching
        its board dependencies."""
        resources: Dict[str, str] = {}
        for prefix, mapping in (("references", angel.skill.references),
                                ("assets", angel.skill.assets)):
            for rel_path, content in mapping.items():
                resources[f"{prefix}/{rel_path}"] = content
        for rel_path, content in angel.skill.additional_files.items():
            if rel_path != DEPENDENCIES_FILE:
                resources.setdefault(rel_path, content)
        return cls(
            metadata=BundleMetadata(name=angel.name, version=angel.version,
                                    description=angel.description, tags=angel.tags),
            entrypoint=angel.skill.skill_md,
            resources=resources,
            scripts=dict(angel.skill.scripts),
            dependencies=dependencies or [],
        )

    def to_angel(self) -> Angel:
        """Materialize the skill as a guardian Angel ready to run on
        Hermes-Agent. The persona comes from the SKILL.md frontmatter."""
        meta, _ = parse_frontmatter(self.entrypoint)
        references: Dict[str, str] = {}
        assets: Dict[str, str] = {}
        additional: Dict[str, str] = {}
        buckets = {"references": references, "assets": assets}
        for path, content in self.resources.items():
            top, _, rest = path.partition("/")
            if rest and top in buckets:
                buckets[top][rest] = content
            else:
                additional[path] = content
        skill = AngelSkill(
            name=self.name,
            skill_md=self.entrypoint,
            scripts=dict(self.scripts),
            references=references,
            assets=assets,
            additional_files=additional,
        )
        return Angel(
            name=self.name,
            persona=meta.get("persona"),
            description=self.metadata.description or meta.get("description", ""),
            version=self.metadata.version,
            tags=list(self.metadata.tags),
            skill=skill,
        )

    # ------------------------------------------------------------------ #
    # identity <-> serialization (skill-bundle/v1)
    # ------------------------------------------------------------------ #

    def to_bundle(self) -> SkillBundle:
        """Serialize the identity into a skill-bundle/v1 envelope. The
        dependencies travel as a dedicated ``dependencies.json`` file so the
        bundle stays a plain, integrity-checked list of files."""
        files: Dict[str, str] = {"SKILL.md": self.entrypoint}
        for rel_path, content in self.scripts.items():
            files[f"scripts/{rel_path}"] = content
        for path, content in self.resources.items():
            files.setdefault(path, content)
        if self.dependencies:
            files[DEPENDENCIES_FILE] = json.dumps({
                "schema": DEPENDENCIES_SCHEMA,
                "dependencies": [d.model_dump() for d in self.dependencies],
            }, indent=2)
        return SkillBundle.build(metadata=self.metadata, files=files, entrypoint="SKILL.md")

    @classmethod
    def from_bundle(cls, bundle) -> "Skill":
        """Rebuild the identity from a skill-bundle/v1 envelope (model, dict
        or JSON string). The bundle is fully verified first (schema id,
        entrypoint, safe unique paths, SHA-256 of every file)."""
        if isinstance(bundle, str):
            bundle = json.loads(bundle)
        if isinstance(bundle, dict):
            bundle = SkillBundle.model_validate(bundle)

        entrypoint = ""
        scripts: Dict[str, str] = {}
        resources: Dict[str, str] = {}
        dependencies: List[SkillDependency] = []
        for f in bundle.files:
            if f.path == bundle.entrypoint:
                entrypoint = f.content
            elif f.path == DEPENDENCIES_FILE:
                payload = json.loads(f.content)
                if payload.get("schema") != DEPENDENCIES_SCHEMA:
                    raise ValueError(
                        f"unsupported dependencies schema '{payload.get('schema')}', "
                        f"expected '{DEPENDENCIES_SCHEMA}'"
                    )
                dependencies = [SkillDependency.model_validate(d)
                                for d in payload.get("dependencies", [])]
            elif f.path.startswith("scripts/"):
                scripts[f.path[len("scripts/"):]] = f.content
            else:
                resources[f.path] = f.content
        return cls(metadata=bundle.metadata, entrypoint=entrypoint,
                   resources=resources, scripts=scripts, dependencies=dependencies)


class Angels(BaseModel):
    """The Host of Angels: a multi-agent system built from Skill identities.

    Members are pure identities; they hold no references to one another.
    The system is wired exclusively through the Eden board: one member's
    'produce' dependency is another member's 'consume' dependency, and
    workflows name the order of those exchanges. ``validate_system`` checks
    that the wiring is sound, ``deployment_cards`` emits the write payloads
    that place every Angel onto the board."""
    name: str = Field(..., description="Name of the multi-agent system, e.g. 'newsroom-host'")
    space: str = Field(default="eden", description="Eden board the system operates on")
    skills: Dict[str, Skill] = Field(
        default_factory=dict,
        description="The member skills, keyed by skill (= Angel) name"
    )

    def enlist(self, skill: Skill) -> Angel:
        """Add a member to the Host and return its Angel. Names are unique."""
        if skill.name in self.skills:
            raise ValueError(f"an Angel named '{skill.name}' is already enlisted in '{self.name}'")
        self.skills[skill.name] = skill
        return skill.to_angel()

    @property
    def angels(self) -> Dict[str, Angel]:
        """All members materialized as Angels, keyed by name."""
        return {name: skill.to_angel() for name, skill in self.skills.items()}

    # ------------------------------------------------------------------ #
    # board wiring
    # ------------------------------------------------------------------ #

    def consumers_of(self, kind: str) -> List[str]:
        """Names of the members that take cards of this kind."""
        return [name for name, s in self.skills.items()
                if any(d.kind == kind for d in s.consumes())]

    def producers_of(self, kind: str) -> List[str]:
        """Names of the members that write cards of this kind."""
        return [name for name, s in self.skills.items()
                if any(d.kind == kind for d in s.produces())]

    def workflow(self, workflow_name: str) -> List[Tuple[int, str, SkillDependency]]:
        """The ordered exchanges of a named workflow:
        a list of (step, angel name, dependency), sorted by step."""
        steps = [
            (dep.step, name, dep)
            for name, skill in self.skills.items()
            for dep in skill.dependencies
            if dep.workflow == workflow_name and dep.step is not None
        ]
        return sorted(steps, key=lambda item: item[0])

    def workflows(self) -> List[str]:
        """All workflow names referenced by the members."""
        return sorted({d.workflow for s in self.skills.values()
                       for d in s.dependencies if d.workflow})

    def validate_system(self) -> List[str]:
        """Check that the board wiring is sound. Returns a list of issues
        (empty = the system is well-formed):
        - every workflow's steps are consecutive starting at 1;
        - every consumed kind after step 1 is produced by an earlier step;
        - every produced kind has at least one consumer somewhere
          (otherwise its cards pile up until their lease expires)."""
        issues: List[str] = []
        for wf in self.workflows():
            steps = self.workflow(wf)
            numbers = sorted({step for step, _, _ in steps})
            if numbers != list(range(1, len(numbers) + 1)):
                issues.append(f"workflow '{wf}': steps {numbers} are not consecutive from 1")
                continue
            produced_so_far: set = set()
            for step, name, dep in steps:
                if dep.action == CONSUME and step > 1 and dep.kind not in produced_so_far:
                    issues.append(
                        f"workflow '{wf}' step {step}: '{name}' consumes '{dep.kind}' "
                        f"but no earlier step produces it"
                    )
                if dep.action == PRODUCE:
                    produced_so_far.add(dep.kind)
        for name, skill in self.skills.items():
            for dep in skill.produces():
                if self.consumers_of(dep.kind):
                    continue
                if dep.workflow:
                    steps = [step for step, _, _ in self.workflow(dep.workflow)]
                    if steps and dep.step == max(steps):
                        continue  # final deliverable of the workflow, picked up by the user
                issues.append(
                    f"'{name}' produces '{dep.kind}' cards but no member consumes them"
                )
        return issues

    # ------------------------------------------------------------------ #
    # deployment
    # ------------------------------------------------------------------ #

    def deployment_cards(self, lease_seconds: Optional[int] = None) -> List[dict]:
        """The POST /api/write/ payloads that place every member onto the
        Eden board as a kind='angel' card. The card fields carry the
        integrity-checked skill bundle plus the board assignments, so any
        Hermes-Agent runner can pick an Angel up and know exactly which
        cards to watch and write."""
        payloads = []
        for name, skill in self.skills.items():
            angel = skill.to_angel()
            fields = angel.to_card_fields()
            fields["skill_bundle"] = skill.to_bundle().dump()  # includes dependencies.json
            fields["assignments"] = [d.model_dump() for d in skill.dependencies]
            fields["host"] = self.name
            payload = {"space": self.space, "kind": "angel", "fields": fields, "agent": self.name}
            if lease_seconds is not None:
                payload["lease_seconds"] = lease_seconds
            payloads.append(payload)
        return payloads
