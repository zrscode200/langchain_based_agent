"""Declare both packages while retaining OG's graph and lifecycle adapters."""
from importlib.metadata import version
import json
from pathlib import Path

from lc_factory import launch as og_launch


def runtime_dependency():
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file():
        return f"lc-factory-enterprise @ {root.as_uri()}"
    return f"lc-factory-enterprise=={version('lc-factory-enterprise')}"


def scaffold_workspace(work_dir):
    og_launch.scaffold_workspace(work_dir, extra_runtime_dependencies=(runtime_dependency(),))
    config_path = Path(work_dir) / "langgraph.json"
    config = json.loads(config_path.read_text())
    config["graphs"]["agent"] = "lc_factory_enterprise.server_graph:make_graph"
    config["http"]["app"] = "lc_factory_enterprise.offload_api:app"
    config_path.write_text(json.dumps(config, indent=2))
