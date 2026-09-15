"""Application-owned skill discovery, shared by graph and client catalogues.

No policy preserves the personal CLI's upstream discovery. A project can opt in
through .deepagents/skills.toml; embedders can supply the same data explicitly.
This controls discovery, not the agent's underlying filesystem permissions.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import tomllib

from lc_factory.upstream import FilesystemBackend, PluginSkillsMiddleware, built_in_skills_dir, find_git_root, import_skill_policy_modules, import_skill_command_modules


@dataclass(frozen=True)
class SkillPolicy:
    mode: str = "personal"
    sources: tuple[str, ...] | None = None
    include_builtin: bool = False


def skill_policy_root(cwd=None, *, project_context=None, credentials=None):
    """Match client/server project discovery without inheriting another cwd."""
    if project_context is not None:
        return Path(project_context.project_root or project_context.user_cwd).resolve()
    configured = getattr(credentials, "project_root", None)
    if cwd is None and configured is not None:
        return Path(configured).resolve()
    base = Path(cwd or Path.cwd()).resolve()
    if configured is not None and base.is_relative_to(Path(configured).resolve()):
        return Path(configured).resolve()
    return find_git_root(base) or base


def resolve_skill_policy(root, value=None):
    """Validate policy before discovery; invalid explicit policy never inherits."""
    if value is None and root is not None:
        root = Path(root).resolve()
        path = root / ".deepagents" / "skills.toml"
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError(f"Skill policy escapes project: {path}")
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            if set(data) != {"skills"}:
                raise ValueError(f"{path} must contain only [skills]")
            value = data["skills"]
        except FileNotFoundError:
            if path.is_symlink():
                raise ValueError(f"Broken skill policy symlink: {path}") from None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Cannot read skill policy {path}: {type(exc).__name__}") from exc
    if value is None:
        return SkillPolicy()
    if isinstance(value, SkillPolicy):
        value = dict(mode=value.mode, sources=value.sources, include_builtin=value.include_builtin)
    if not isinstance(value, Mapping) or set(value) - {"mode", "sources", "include_builtin"}:
        raise ValueError("skill_policy accepts only mode, sources and include_builtin")
    mode = value.get("mode", "project")
    if mode not in ("personal", "project"):
        raise ValueError("skill_policy.mode must be personal or project")
    builtin = value.get("include_builtin", False)
    if not isinstance(builtin, bool):
        raise ValueError("skill_policy.include_builtin must be a boolean")
    paths = value.get("sources")
    if paths is not None and (not isinstance(paths, (list, tuple)) or any(
        not isinstance(p, str) or not p.strip() for p in paths
    )):
        raise ValueError("skill_policy.sources must be a list of nonempty paths")
    if mode == "personal" and (paths is not None or builtin):
        raise ValueError("Explicit skill sources/include_builtin require project mode")
    if mode == "project" and root is None:
        raise ValueError("Project skill policy requires an explicit workspace root")
    return SkillPolicy(mode, None if paths is None else tuple(paths), builtin)


def project_skill_sources(root, policy):
    """Return only selected roots; symlinks cannot introduce personal sources."""
    root = Path(root).resolve()
    paths = policy.sources if policy.sources is not None else (
        ".deepagents/skills", ".agents/skills", ".claude/skills",
    )
    result = []
    if policy.include_builtin:
        result.append((str(built_in_skills_dir()), "Built-in"))
    for source in paths:
        if Path(source).is_absolute():
            raise ValueError(f"Skill source must be relative to project: {source!r}")
        path = (root / source).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Skill source escapes project: {source!r}")
        result.append((str(path), "Project"))
    return result


class SelectedSkillBackend(FilesystemBackend):
    """Contain discovery reads, including a SKILL.md symlink within a source."""

    def __init__(self, sources):
        super().__init__(virtual_mode=False)
        self.allowed_roots = tuple(Path(source[0]).resolve() for source in sources)

    def _resolve_path(self, key):
        path = super()._resolve_path(key).resolve()
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise PermissionError("Skill file is outside selected sources")
        return path


class ProjectSkillsMiddleware(PluginSkillsMiddleware):
    """Rebuild the catalogue so resumed personal state cannot bypass selection."""

    def __init__(self, *, sources, backend=None):
        # Always restrict discovery before files are opened. Filtering metadata
        # afterwards would still read symlinked personal SKILL.md content.
        super().__init__(backend=SelectedSkillBackend(sources), sources=sources)

    @property
    def name(self):
        return "SkillsMiddleware"

    def _selected(self, update):
        roots = [Path(source).resolve() for source in self.sources]
        update["skills_metadata"] = [skill for skill in update["skills_metadata"]
            if any(Path(skill["path"]).resolve().is_relative_to(root) for root in roots)]
        # Clear old errors from a resumed graph along with its old catalogue.
        update.setdefault("skills_load_errors", [])
        return update

    def before_agent(self, state, runtime, config):
        return self._selected(super().before_agent(
            {k: v for k, v in state.items() if k != "skills_metadata"}, runtime, config))

    async def abefore_agent(self, state, runtime, config):
        return self._selected(await super().abefore_agent(
            {k: v for k, v in state.items() if k != "skills_metadata"}, runtime, config))


def discover_project_skills(root, policy):
    """Use the same middleware loader for client listing and model discovery."""
    sources = project_skill_sources(root, policy)
    middleware = ProjectSkillsMiddleware(sources=sources)
    update = middleware.before_agent({}, None, {})
    skills = [dict(skill, source=next(
        label.lower() for source, label in reversed(sources)
        if Path(skill["path"]).resolve().is_relative_to(Path(source).resolve())
    )) for skill in update["skills_metadata"]]
    return skills, [Path(source[0]).resolve() for source in sources]


@contextmanager
def client_skill_policy():
    """Adapt upstream listing/invocation without modifying installed packages."""
    agent, app, loader, invocation, credentials, _ = import_skill_policy_modules()
    commands, command_config, output = import_skill_command_modules()

    original_sources = agent.get_skill_sources
    original_list = loader.list_skills
    original_discover = invocation.discover_skills_and_roots
    original_app_discover = app.DeepAgentsApp._discover_skills_and_roots
    original_content = loader.load_skill_content
    original_invoke = app.DeepAgentsApp._invoke_skill
    original_command_list = commands._list
    invocation_root = ContextVar("factory_skill_invocation_root", default=None)

    def root_for(cwd=None):
        return invocation_root.get() or skill_policy_root(cwd, credentials=credentials)

    def sources(assistant_id=agent.DEFAULT_AGENT_NAME, project_context=None):
        root = skill_policy_root(project_context=project_context, credentials=credentials)
        policy = resolve_skill_policy(root)
        if policy.mode == "personal":
            return original_sources(assistant_id, project_context)
        return project_skill_sources(root, policy)

    def listing(**kwargs):
        root = root_for()
        policy = resolve_skill_policy(root)
        return original_list(**kwargs) if policy.mode == "personal" else discover_project_skills(root, policy)[0]

    def discovery(assistant_id, **kwargs):
        root = root_for(kwargs.get("path_base"))
        policy = resolve_skill_policy(root)
        return (original_discover(assistant_id, **kwargs) if policy.mode == "personal"
                else discover_project_skills(root, policy))

    def command_list(agent, *, project=False, output_format="text"):
        root = root_for()
        policy = resolve_skill_policy(root)
        if policy.mode == "personal":
            return original_command_list(agent, project=project, output_format=output_format)
        # Upstream returns before its loader when the standard directories are
        # absent. Custom/empty application sources must bypass that precheck.
        rows, _ = discover_project_skills(root, policy)
        if project:
            rows = [row for row in rows if row["source"] == "project"]
        if output_format == "json":
            output.write_json("skills list", rows)
            return
        console = command_config.console
        if not rows:
            console.print("No project skills selected." if project else "No skills selected.", markup=False)
            return
        console.print("Project Skills:" if project else "Available Skills:", markup=False)
        for row in rows:
            console.print(f"  {row['name']} ({row['source']})", markup=False)
            console.print(f"    {row['description']}", markup=False)
            console.print(f"    {Path(row['path']).parent}/", markup=False)

    def app_discovery(self):
        root = root_for(self._cwd)
        policy = resolve_skill_policy(root)
        if policy.mode == "personal":
            return original_app_discover(self)
        self._discovered_plugin_ids = set()
        return discover_project_skills(root, policy)

    def content(skill_path, *, allowed_roots=()):
        root = root_for()
        policy = resolve_skill_policy(root)
        if policy.mode == "project":
            selected = project_skill_sources(root, policy)
            path = Path(skill_path).resolve()
            if not any(path.is_relative_to(Path(source).resolve()) for source, _ in selected):
                # ValueError is deliberate: upstream PermissionError offers a
                # trust override, which cannot widen an application's policy.
                raise ValueError("Skill is outside the project's selected skill sources")
        return original_content(skill_path, allowed_roots=allowed_roots)

    async def invoke(self, skill_name, args="", *, command=None):
        root = root_for(self._cwd)
        token = invocation_root.set(root)
        try:
            try:
                policy = resolve_skill_policy(root)
                if policy.mode == "project":
                    # Upstream otherwise accepts cached personal metadata and
                    # treats an empty containment-root list as unrestricted.
                    rows, roots = await asyncio.to_thread(app_discovery, self)
                    self._discovered_skills = rows
                    self._skill_allowed_roots = roots
            except ValueError as exc:
                await self._mount_message(app.AppMessage(f"Cannot load skill policy: {exc}"))
                return
            await original_invoke(self, skill_name, args, command=command)
        finally:
            invocation_root.reset(token)

    replacements = [(agent, "get_skill_sources", sources), (loader, "list_skills", listing),
                    (commands, "_list", command_list),
                    (invocation, "discover_skills_and_roots", discovery),
                    (app.DeepAgentsApp, "_discover_skills_and_roots", app_discovery),
                    (loader, "load_skill_content", content),
                    (app.DeepAgentsApp, "_invoke_skill", invoke)]
    originals = [(obj, key, getattr(obj, key)) for obj, key, _ in replacements]
    try:
        for obj, key, value in replacements:
            setattr(obj, key, value)
        from lc_factory.skill_activity_ui import client_skill_activity
        with client_skill_activity():
            yield
    finally:
        for obj, key, value in reversed(originals):
            setattr(obj, key, value)
