"""The shared HTTP client is `httpx2`; `httpx` is gone from the project (convention 9)."""

import ast
import re
import tomllib

import httpx2

from conftest import ROOT
from dispatcher import http


def _package_name(requirement):
    return re.split(r"[\s<>=!~;\[(]", requirement.strip(), maxsplit=1)[0].lower()


def _imported_top_level_modules(path):
    modules = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add((node.module or "").split(".")[0])
    return modules


def _python_files():
    return [
        path
        for folder in ("src", "tests")
        for path in (ROOT / folder).rglob("*.py")
        if ".venv" not in path.parts
    ]


def test_httpx2_is_a_runtime_dependency_and_httpx_is_not_declared():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = {_package_name(r) for r in project["project"]["dependencies"]}
    declared = runtime | {
        _package_name(r)
        for group in project.get("dependency-groups", {}).values()
        for r in group
    }
    assert "httpx2" in runtime
    assert "httpx" not in declared


def test_httpx_is_not_in_the_lock_file():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"] for package in lock["package"]}
    assert "httpx2" in locked
    assert "httpx" not in locked


def test_no_source_or_test_file_imports_httpx():
    importers = [
        str(path.relative_to(ROOT))
        for path in _python_files()
        if "httpx" in _imported_top_level_modules(path)
    ]
    assert importers == []


def test_the_shared_client_is_an_httpx2_client():
    with http.client() as shared:
        assert isinstance(shared, httpx2.Client)


def test_the_shared_client_uses_the_injected_transport_and_the_timeout(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    monkeypatch.setattr(http, "transport", httpx2.MockTransport(handler))
    with http.client() as shared:
        assert shared.get("https://auth.example.com/ping").json() == {"ok": True}
        assert shared.timeout.read == http.TIMEOUT_SECONDS
    assert [r.url.host for r in seen] == ["auth.example.com"]


def test_only_the_http_module_builds_an_httpx2_client():
    builders = []
    for path in (ROOT / "src" / "dispatcher").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"Client", "AsyncClient"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx2"
            ):
                builders.append(path.name)
    assert builders == ["http.py"]
