import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_copies_pyproject_readme_before_install():
    pyproject = tomllib.loads((ROOT / "platform/pyproject.toml").read_text())
    readme = pyproject["project"]["readme"]
    dockerfile = (ROOT / "deploy/Dockerfile").read_text()

    assert f"COPY platform/{readme} platform/{readme}" in dockerfile
    assert dockerfile.index(f"COPY platform/{readme} platform/{readme}") < dockerfile.index(
        'RUN pip install --no-cache-dir -e "./platform[postgres]"'
    )


def test_compose_api_has_http_healthcheck():
    compose = (ROOT / "deploy/docker-compose.yml").read_text()

    assert "  api:\n" in compose
    api_section = compose.split("  api:\n", 1)[1].split("  worker:\n", 1)[0]
    assert "healthcheck:" in api_section
    assert "http://127.0.0.1:8000/health" in api_section
    assert "urllib.request" in api_section
    assert "start_period: 5s" in api_section


def test_compose_file_is_valid_for_docker_compose():
    docker_compose = shutil.which("docker-compose")
    if docker_compose is None:
        pytest.skip("docker-compose is not installed")

    result = subprocess.run(
        [docker_compose, "-f", str(ROOT / "deploy/docker-compose.yml"), "config"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"context: {ROOT}" in result.stdout
    assert "dockerfile: deploy/Dockerfile" in result.stdout
    assert 'published: "8000"' in result.stdout
    assert "target: 8000" in result.stdout
    assert "http://127.0.0.1:8000/health" in result.stdout


def test_compose_api_healthcheck_is_bounded_and_status_strict():
    compose = (ROOT / "deploy/docker-compose.yml").read_text()
    api_section = compose.split("  api:\n", 1)[1].split("  worker:\n", 1)[0]

    assert "urlopen('http://127.0.0.1:8000/health', timeout=2)" in api_section
    assert "['status'] == 'ok'" in api_section
    assert "timeout: 3s" in api_section
    assert "retries: 20" in api_section


def test_dockerignore_keeps_build_context_minimal():
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

    assert dockerignore[0] == "**"
    assert "!deploy/Dockerfile" in dockerignore
    assert "!platform/pyproject.toml" in dockerignore
    assert "!platform/README.md" in dockerignore
    assert "!platform/ticloud/**" in dockerignore
    assert "!.venv" not in dockerignore
    assert "!platform/tests/**" not in dockerignore
