"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

import yaml

from nanobot.agent.remote_skills import RemoteSkillSync, cache_dir as remote_cache_dir

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Old skill names, read as the new ones. Stored conversations keep the blocks a
# model wrote under the old name, and a model reading them back writes it
# again; config written before the rename keeps naming it. Both are aliased on
# read rather than rewritten, the way this stack treats every renamed name.
SKILL_ALIASES = {
    "tasks": "chores",   # renamed 2026-09-11; see skills/chores/SKILL.md
    "home-lights": "lights",  # renamed 2026-09-12; served by the smart-lights plugin
}

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        # Skills served by the service they describe, kept on disk by a
        # background refresher — see agent/remote_skills.py. Starting it here
        # rather than from an entry point is deliberate: nanobot runs as a CLI,
        # an API server, a cron runner and a subagent, and this is the one place
        # all four pass through.
        self.remote_skills = remote_cache_dir(workspace)
        RemoteSkillSync.ensure_started(workspace, self.builtin_skills)
        # An entry naming a skill by its old name still switches it off: the
        # house instance lists `tasks` to keep chores away from a container
        # with no member credentials, and a rename must not undo that quietly.
        disabled = set(disabled_skills or ())
        self.disabled_skills = disabled | {SKILL_ALIASES[n] for n in disabled if n in SKILL_ALIASES}

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        # Precedence: workspace, then remote, then builtin.
        #
        # Remote beats builtin because the service is the authority on its own
        # API and ships in lockstep with it — that is the whole reason a service
        # serves a skill. The workspace still beats remote: a file somebody put
        # in this instance's own config is a deliberate local override, and an
        # override a remote service can silently defeat is not one.
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        seen = {entry["name"] for entry in skills}
        remote = self._skill_entries_from_dir(self.remote_skills, "remote", skip_names=seen)
        skills.extend(remote)
        seen |= {entry["name"] for entry in remote}
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=seen)
            )

        if self.disabled_skills:
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        roots = [self.workspace_skills, self.remote_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(
        self,
        exclude: set[str] | None = None,
        only: set[str] | None = None,
    ) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed. The ``name — desc `path` ``
        lines are also what the skill-invocation interceptor parses to map a
        ``{"skill": ...}`` block back to its SKILL.md path, so every skill that
        can be invoked (including always-loaded ones) needs a line here.

        Args:
            exclude: Set of skill names to omit from the summary.
            only: If given, include *only* these skills (applied after exclude).

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        unavailable: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            if only is not None and skill_name not in only:
                continue
            meta = self._get_skill_meta(skill_name)
            if not self._check_requirements(meta):
                # A skill that cannot run here -- its binary is not installed,
                # its key is not set -- is named, not described: the model
                # should know to say "not here", and the full description of
                # something it cannot invoke is prompt spent on every turn.
                missing = self._get_missing_requirements(meta)
                unavailable.append(f"{skill_name} ({missing})" if missing else skill_name)
                continue
            desc = self._get_skill_description(skill_name)
            lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
        if unavailable:
            lines.append(f"- Not available on this machine: {', '.join(unavailable)}.")
        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: object) -> dict:
        """Extract nanobot/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("nanobot", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_nanobot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata
