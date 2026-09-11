"""Require prebuilt JS in release wheels; never fetch dependencies in a build hook."""
import os
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebAssetsHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version == "editable":
            return
        root = Path(self.root) / "src/lc_factory/_web"
        # Native backend scaffolds also build Python wheels from source. A
        # TUI-only install must not acquire a Node/npm build prerequisite.
        if not root.exists() and os.environ.get("LC_FACTORY_REQUIRE_WEB_ASSETS") != "1":
            return
        for file in ("server/main.mjs", "dist/index.html"):
            if not (root / file).is_file():
                raise RuntimeError("Build web assets before packaging: cd web && npm run build:launcher")
        build_data["force_include"][str(root)] = "lc_factory/_web"
