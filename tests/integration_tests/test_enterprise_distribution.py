"""Installed wheels, real enterprise gateway adapter and OG server; loopback only."""
import json
import os
import subprocess
import sys
import sysconfig
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from langchain_core.messages import convert_to_messages
from lc_factory._testing_models import SettledIntegrationChatModel
from lc_factory_enterprise.merck_models import MerckChatModel

pytestmark = pytest.mark.integration


def test_installed_enterprise_uses_og_server_and_local_gateway(tmp_path):
    """Own wheels override editable OG; dependencies reuse the reviewed venv."""
    repo = Path(__file__).resolve().parents[2]
    wheels = [repo / "dist" / "og-enterprise" / name for name in (
        "lc_factory-0.2.0-py3-none-any.whl",
        "lc_factory_enterprise-0.2.0-py3-none-any.whl",
    )]
    assert all(path.is_file() for path in wheels), "Build both current wheels before this check"
    venv = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True, timeout=60)
    python = venv / "bin" / "python"
    result = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                             "--no-cache-dir", *(str(p) for p in wheels)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    # Reuse only the already-reviewed third-party package tree. The new venv's
    # own wheels take precedence; no source PYTHONPATH survives into its server.
    site = subprocess.check_output([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
    (Path(site) / "reviewed_dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("DEEPAGENTS_", "LC_FACTORY_", "LANGSMITH_", "LANGCHAIN_"))
                   and k != "PYTHONPATH"}
    home = tmp_path / "profile-home"
    profile = home / ".deepagents"
    profile.mkdir(parents=True)
    environment.update(HOME=str(home), DEEPAGENTS_CODE_NO_UPDATE_CHECK="1",
                       COMPANY_LLM_API_KEY="offline-test-key", LC_FACTORY_SETTLED_DISPATCH="1",
                       LC_FACTORY_CAPABILITIES="reload,background,history")
    probe = subprocess.run([str(python), "-c", "import lc_factory, lc_factory_enterprise; print(lc_factory.__file__); print(lc_factory_enterprise.__file__)"],
                           env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    assert all(str(venv) in line for line in probe.stdout.splitlines()[-2:]), probe.stdout
    cli = venv / "bin" / "ddt-agent"
    version = subprocess.run([str(cli), "--version"], env=environment, capture_output=True, text=True, timeout=30)
    assert version.returncode == 0 and "DDT-agent 0.2.0" in version.stdout

    requests = []
    fake = SettledIntegrationChatModel(model="fixture")
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("X-Merck-APIKey"), payload))
            result = fake._generate(convert_to_messages(payload["messages"]))
            data = json.dumps({"choices": [{"message": MerckChatModel._convert_message(result.generations[0].message)}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    (profile / "config.toml").write_text(f'''
[models.providers.merck]
class_path = "lc_factory.merck_models:MerckChatModel"
api_key_env = "COMPANY_LLM_API_KEY"
base_url = "http://127.0.0.1:{gateway.server_port}/gateway"
models = ["fixture"]
[models.providers.merck.params]
api_model = "local-route"
[retries.merck]
param = "max_retries"
max_retries = 1
''')
    workspace = tmp_path / "outer" / "consumer"
    work = workspace / "work"
    work.mkdir(parents=True)
    (workspace / ".deepagents").mkdir()
    (workspace / ".deepagents" / "subagents.toml").write_text(
        '[subagents.general-purpose]\nmode = "isolated"\n')
    target = work / "delegated.txt"
    try:
        result = subprocess.run([str(cli), "--timeout", "120", "-M", "merck:fixture", "-q", "--no-stream",
                                 "-n", f"DCA_TEST_DELEGATE_WRITE={target}"],
                                env=environment, cwd=work, capture_output=True, text=True, timeout=150)
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=5)
    assert result.returncode == 0, result.stderr[-4000:]
    assert target.exists(), result.stdout[-2000:]
    with sqlite3.connect(profile / ".state" / "sessions.db") as connection:
        bindings = connection.execute("select cwd, project_root from dcode_thread_workspaces").fetchall()
    assert (str(work), str(workspace)) in bindings
    assert requests and all(key == "offline-test-key" for _, key, _ in requests)
    assert all(path.startswith("/gateway/local-route/chat/completions") for path, _, _ in requests)
    assert any(any(tool["function"]["name"] == "task_settled" for tool in payload.get("tools", []))
               for _, _, payload in requests)
