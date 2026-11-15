import ast
import subprocess
import sys
import tomllib

from conftest import ROOT, SETTING_NAMES


def run_cli(*args, env_extra=None):
    import os

    env = {k: v for k, v in os.environ.items() if k not in SETTING_NAMES}
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "dispatcher", *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )


def test_help_prints_usage_and_exits_zero_with_no_settings():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_cli_runs_without_a_database_setting():
    # A setting is required only by the operation that uses it.
    result = run_cli("--help")
    assert result.returncode == 0
    assert "TURSO_DATABASE_URL" not in result.stderr


def test_ruff_is_configured_for_python_314_with_up_and_dtz():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]
    assert config["target-version"] == "py314"
    lint = config.get("lint", {})
    selected = set(lint.get("select", [])) | set(lint.get("extend-select", []))
    assert {"UP", "DTZ"} <= selected


def test_pytest_and_ruff_are_dev_only_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = " ".join(project["project"]["dependencies"]).lower()
    dev = " ".join(project["dependency-groups"]["dev"]).lower()
    assert "pytest" not in runtime and "ruff" not in runtime
    assert "pytest" in dev and "ruff" in dev


def test_uv_lock_is_committed():
    tracked = subprocess.run(
        ["git", "ls-files", "uv.lock"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == "uv.lock"


def test_runtime_dependencies_include_the_prd_stack():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = " ".join(project["project"]["dependencies"]).lower()
    assert "fastapi" in runtime
    assert "turso-serverless" in runtime


def test_ruff_format_and_check_pass():
    for command in (["format", "--check"], ["check"]):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *command],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_only_the_database_module_imports_the_driver():
    importers = []
    for path in (ROOT / "src" / "dispatcher").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(name.split(".")[0] == "turso_serverless" for name in names):
                importers.append(path.name)
    assert set(importers) == {"db.py"}


def test_settings_are_read_in_one_module_only():
    readers = []
    for path in (ROOT / "src" / "dispatcher").rglob("*.py"):
        source = path.read_text()
        if "os.environ" in source or "getenv" in source or "dotenv" in source:
            readers.append(path.name)
    assert readers == ["settings.py"]


def test_fly_config_has_the_prd_http_service_block():
    fly = tomllib.loads((ROOT / "fly.toml").read_text())
    service = fly["http_service"]
    assert service["auto_stop_machines"] == "suspend"
    assert service["auto_start_machines"] is True
    assert service["min_machines_running"] == 0


def test_fly_internal_port_is_the_port_the_image_serves_on():
    fly = tomllib.loads((ROOT / "fly.toml").read_text())
    port = str(fly["http_service"]["internal_port"])
    dockerfile = (ROOT / "Dockerfile").read_text()
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD")]
    assert len(cmd) == 1
    assert f'"{port}"' in cmd[0] or f"--port {port}" in cmd[0]


def test_dockerfile_uses_python_314_and_locked_non_dev_install():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.splitlines()[0].startswith("FROM python:3.14")
    assert "--locked" in dockerfile
    assert "--no-dev" in dockerfile
    assert "dispatcher.app" in dockerfile


def test_readme_covers_install_test_image_and_single_machine_deploy():
    readme = (ROOT / "README.md").read_text()
    assert "uv sync" in readme
    assert "pytest" in readme
    assert "docker build" in readme
    assert "fly deploy" in readme
    assert "--ha=false" in readme
    assert "fly secrets" in readme
