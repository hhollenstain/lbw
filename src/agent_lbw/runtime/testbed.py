"""Throwaway environments for a tool the project could not start.

The project's own virtualenv, node_modules, and vendor directory stay as they
are. Dependencies are installed beside the checkout, then the suite or linter
runs with that tool. When the language tool is not on the machine, the same
install happens inside a short-lived container. A missing ruff binary is
installed the same way, and the container that already contains ruff has no
network. A command that could not find a whole toolchain, such as uv or npm,
runs again with that toolchain installed beside the checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agent_lbw.progress import record_event
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult
from agent_lbw.tools.shell import run_command

_PYTHON_PACKAGES = {"pytest", "nose", "tox", "nose2"}
_NODE_RUNNERS = ("vitest", "jest", "mocha")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=@+,*-]+$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PYTEST_LINE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:uv\s+run\s+(?:--\S+\s+)*)?"
    r"(?:python\d*(?:\.\d+)?\s+-m\s+)?"
    r"pytest\b(?P<args>.*)$"
)
_ENV_ASSIGN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S*)$")
_IMAGES = {
    "python": "ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
    "node": "node:22-bookworm-slim",
    "go": "golang:1.23-bookworm",
    "rust": "rust:1-bookworm-slim",
    "ruby": "ruby:3.3-bookworm",
    "php": "composer:2",
    "maven": "maven:3-eclipse-temurin-21",
    "gradle": "gradle:8-jdk21",
}
_RUFF_IMAGE = "ghcr.io/astral-sh/ruff:0.16.10"
_RUFF_SUBCOMMANDS = frozenset({"format", "check", "version", "rule", "config", "clean"})
_RUFF_SPEC = re.compile(r"^ruff(?:\[[A-Za-z0-9_,.-]+\])?(?:[><=!~]=?[A-Za-z0-9.*+!,]+)*$")
_RUFF_STEP = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:uv\s+run\s+(?:--\S+\s+)*)?"
    r"(?:pipenv\s+run\s+)?"
    r"(?:python\d*(?:\.\d+)?\s+-m\s+)?"
    r"ruff\b(?P<args>.*)$"
)
_UNSAFE_SHELL = re.compile(r"[\n\r\x00`;<>]|\$\(")


def run_in_testbed(
    workspace: Workspace,
    command: str,
    output: str,
    timeout_seconds: int,
) -> ToolResult | None:
    """Install the missing runner outside the project and run the suite once.

    Returns None when no project file says how to build that environment.
    A suite that then fails is returned as a normal test failure.
    """
    kind = _kind(workspace.root, output, command)
    if kind == "python":
        return _python_bed(workspace, command, output, timeout_seconds)
    if kind == "node":
        return _node_bed(workspace, output, timeout_seconds)
    if kind == "go":
        return _cached_tool(
            workspace,
            kind="go",
            label="Go",
            tool="go",
            setup=None,
            run=_go_command(workspace.root),
            docker_inner="go test ./...",
            timeout_seconds=timeout_seconds,
        )
    if kind == "rust":
        bed = _bed(workspace.root, "rust")
        return _cached_tool(
            workspace,
            kind="rust",
            label="Rust",
            tool="cargo",
            setup=None,
            run=f"env CARGO_TARGET_DIR={_q(bed)} cargo test",
            docker_inner="export CARGO_TARGET_DIR=/tmp/lbw-testbed && cargo test",
            timeout_seconds=timeout_seconds,
        )
    if kind == "ruby":
        bed = _bed(workspace.root, "ruby")
        setup = f"env BUNDLE_PATH={_q(bed)} BUNDLE_IGNORE_CONFIG=1 bundle install"
        run = f"env BUNDLE_PATH={_q(bed)} BUNDLE_IGNORE_CONFIG=1 bundle exec rspec"
        return _cached_tool(
            workspace,
            kind="ruby",
            label="Ruby",
            tool="bundle",
            setup=setup,
            run=run,
            docker_inner=(
                "export BUNDLE_PATH=/tmp/lbw-testbed BUNDLE_IGNORE_CONFIG=1 && "
                "bundle install && bundle exec rspec"
            ),
            timeout_seconds=timeout_seconds,
        )
    if kind == "php":
        bed = _bed(workspace.root, "php")
        binary = bed / "bin" / "phpunit"
        setup = f"env COMPOSER_VENDOR_DIR={_q(bed)} composer install --no-interaction"
        run = f"env COMPOSER_VENDOR_DIR={_q(bed)} {_q(binary)}"
        return _cached_tool(
            workspace,
            kind="php",
            label="PHP",
            tool="composer",
            setup=setup,
            run=run,
            docker_inner=(
                "export COMPOSER_VENDOR_DIR=/tmp/lbw-testbed && "
                "composer install --no-interaction && /tmp/lbw-testbed/bin/phpunit"
            ),
            timeout_seconds=timeout_seconds,
        )
    if kind == "maven":
        bed = _bed(workspace.root, "maven")
        binary = _wrapper(workspace.root, "mvnw", "mvn")
        run = f"{binary} -Dmaven.repo.local={_q(bed)} test"
        return _cached_tool(
            workspace,
            kind="maven",
            label="Maven",
            tool=None if binary.startswith("./") else "mvn",
            setup=None,
            run=run,
            docker_inner="mvn -Dmaven.repo.local=/tmp/lbw-testbed test",
            timeout_seconds=timeout_seconds,
        )
    if kind == "gradle":
        bed = _bed(workspace.root, "gradle")
        binary = _wrapper(workspace.root, "gradlew", "gradle")
        run = f"env GRADLE_USER_HOME={_q(bed)} {binary} test"
        return _cached_tool(
            workspace,
            kind="gradle",
            label="Gradle",
            tool=None if binary.startswith("./") else "gradle",
            setup=None,
            run=run,
            docker_inner="export GRADLE_USER_HOME=/tmp/lbw-testbed && gradle test",
            timeout_seconds=timeout_seconds,
        )
    return None


def ruff_unavailable(output: str) -> bool:
    """The lint command never started ruff. That is not a lint finding."""
    return _ruff_missing(output)


def run_lint_in_testbed(
    workspace: Workspace,
    command: str,
    output: str,
    timeout_seconds: int,
) -> ToolResult | None:
    """Install ruff outside the checkout when the lint command could not start it."""
    if not ruff_unavailable(output):
        return None
    return run_with_throwaway_ruff(workspace, command, timeout_seconds)


def run_with_throwaway_ruff(
    workspace: Workspace,
    command: str,
    timeout_seconds: int,
) -> ToolResult | None:
    """Run a lint command with a ruff binary that is not the project's."""
    bindir = _ensure_ruff(workspace, timeout_seconds)
    if bindir is not None:
        record_event(kind="step", message="Running lint with the throwaway ruff.")
        return run_command(workspace, _ruff_rerun(bindir, command), timeout_seconds)
    steps = _ruff_steps(command)
    if steps and _docker_available():
        return _ruff_docker(workspace, steps, timeout_seconds)
    record_event(kind="step", message="Could not build a throwaway environment for ruff.")
    return None


def _ensure_ruff(workspace: Workspace, timeout_seconds: int) -> Path | None:
    bed = _bed(workspace.root, "ruff")
    binary = bed / "bin" / "ruff"
    stamp = _stamp(_python_inputs(workspace.root))
    if _reusable(bed, stamp, binary):
        record_event(kind="step", message="Reusing the throwaway ruff environment.")
        return binary.parent
    record_event(kind="step", message="Building a throwaway environment for ruff.")
    if _ruff_local(workspace, bed, timeout_seconds):
        _write_stamp(bed, stamp)
        return binary.parent
    _discard(bed)
    return None


def _ruff_local(workspace: Workspace, bed: Path, timeout_seconds: int) -> bool:
    root = workspace.root
    spec = _q(_ruff_requirement(root))
    python = _q(bed / "bin" / "python")
    if shutil.which("uv"):
        _discard(bed)
        command = (
            f"env -u VIRTUAL_ENV UV_NO_CACHE=1 uv venv {_q(bed)} && "
            f"env -u VIRTUAL_ENV UV_NO_CACHE=1 uv pip install --python {python} "
            f"--only-binary=:all: {spec}"
        )
        if run_command(workspace, command, timeout_seconds).ok and (bed / "bin" / "ruff").is_file():
            return True
        _discard(bed)
    if shutil.which("python3"):
        _discard(bed)
        command = (
            f"env -u VIRTUAL_ENV python3 -m venv {_q(bed)} && "
            f"env -u VIRTUAL_ENV {python} -m pip install --no-input --no-cache-dir "
            f"--disable-pip-version-check --only-binary=:all: {spec}"
        )
        if run_command(workspace, command, timeout_seconds).ok and (bed / "bin" / "ruff").is_file():
            return True
        _discard(bed)
    return False


def _ruff_docker(
    workspace: Workspace,
    steps: list[list[str]],
    timeout_seconds: int,
) -> ToolResult:
    """Run ruff from a pinned image. The container has no network and no extra capabilities."""
    image = _RUFF_IMAGE
    root = workspace.root.resolve()
    user = f"{os.getuid()}:{os.getgid()}"
    record_event(kind="step", message="Running lint in a throwaway ruff container.")
    last = ToolResult(False, "ruff did not run")
    for args in steps:
        command = (
            "docker run --rm --network none --read-only --cap-drop=ALL "
            "--security-opt=no-new-privileges "
            "--tmpfs /tmp:rw,noexec,nosuid,size=64m "
            f"--user {user} "
            f"-v {_q(root)}:/workspace -w /workspace "
            f"--entrypoint ruff {image} {shlex.join(args)}"
        )
        last = run_command(workspace, command, timeout_seconds)
        if not last.ok:
            return last
    return last


def _ruff_rerun(bindir: Path, command: str) -> str:
    return f"export PATH={_q(bindir)}:$PATH && {command}"


def _ruff_missing(output: str) -> bool:
    text = output or ""
    if re.search(r"No module named ['\"]?ruff\b", text, re.IGNORECASE):
        return True
    return (
        re.search(r"\bruff\b[^\n]{0,80}(?:not found|command not found)", text, re.IGNORECASE)
        is not None
    )


def _ruff_requirement(root: Path) -> str:
    for name in ("pyproject.toml", "requirements-dev.txt", "requirements.txt"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in re.finditer(
            r"[\"'](ruff(?:\[[A-Za-z0-9_,.-]+\])?(?:[><=!~]=?[A-Za-z0-9.*+!,]+)*)[\"']",
            text,
        ):
            spec = match.group(1)
            if _RUFF_SPEC.fullmatch(spec):
                return spec
        for line in text.splitlines():
            spec = line.strip()
            if _RUFF_SPEC.fullmatch(spec):
                return spec
    return "ruff"


def _ruff_steps(command: str) -> list[list[str]] | None:
    text = (command or "").strip()
    if not text or _UNSAFE_SHELL.search(text):
        return None
    steps: list[list[str]] = []
    for part in text.split("&&"):
        piece = part.strip()
        match = _RUFF_STEP.fullmatch(piece)
        if match is None:
            return None
        args = match.group("args").split()
        if not args or args[0] not in _RUFF_SUBCOMMANDS:
            return None
        if not all(_SAFE_ARG.fullmatch(token) for token in args):
            return None
        steps.append(args)
    return steps or None


def _python_bed(
    workspace: Workspace,
    command: str,
    output: str,
    timeout_seconds: int,
) -> ToolResult | None:
    root = workspace.root
    package = _missing_package(output) or "pytest"
    bed = _bed(root, "python")
    python = bed / "bin" / "python"
    rerun = _python_rerun(command, output, python)
    stamp = _stamp(_python_inputs(root))
    if _reusable(bed, stamp, python):
        record_event(kind="step", message="Reusing the throwaway Python environment.")
        return run_command(workspace, rerun, timeout_seconds)
    record_event(
        kind="step",
        message=f"Building a throwaway Python environment for {package}.",
    )
    if _python_local(workspace, bed, package, timeout_seconds):
        _write_stamp(bed, stamp)
        record_event(kind="step", message="Running the suite in the throwaway Python environment.")
        return run_command(workspace, rerun, timeout_seconds)
    if _docker_available():
        inner = _python_docker_inner(root, package, command, output)
        return _docker(workspace, "python", inner, timeout_seconds)
    record_event(
        kind="step",
        message=f"Could not build a throwaway Python environment for {package}.",
    )
    _discard(bed)
    return None


def _python_local(workspace: Workspace, bed: Path, package: str, timeout_seconds: int) -> bool:
    root = workspace.root
    _discard(bed)
    quoted_package = _q(package)
    python = _q(bed / "bin" / "python")
    if shutil.which("uv") and (root / "uv.lock").is_file() and (root / "pyproject.toml").is_file():
        command = (
            f"env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT={_q(bed)} "
            "uv sync --frozen --all-extras --all-groups"
        )
        synced = run_command(workspace, command, timeout_seconds)
        if synced.ok and _python_exists(bed):
            run_command(
                workspace,
                f"env -u VIRTUAL_ENV uv pip install --python {python} {quoted_package}",
                timeout_seconds,
            )
            return True
        _discard(bed)
    if shutil.which("uv") and (root / "pyproject.toml").is_file():
        spec = _editable_spec(root, package)
        command = (
            f"env -u VIRTUAL_ENV uv venv {_q(bed)} && "
            f"env -u VIRTUAL_ENV uv pip install --python {python} -e {_q(spec)} {quoted_package}"
        )
        if run_command(workspace, command, timeout_seconds).ok and _python_exists(bed):
            return True
        _discard(bed)
    if shutil.which("python3") and _python_project(root):
        requirements = _requirement_flags(root)
        install = f"{python} -m pip install {requirements} {quoted_package}".strip()
        command = f"env -u VIRTUAL_ENV python3 -m venv {_q(bed)} && env -u VIRTUAL_ENV {install}"
        if run_command(workspace, command, timeout_seconds).ok and _python_exists(bed):
            return True
        _discard(bed)
    return False


def _python_exists(bed: Path) -> bool:
    return (bed / "bin" / "python").is_file()


def _python_docker_inner(root: Path, package: str, command: str, output: str) -> str:
    frozen = " --frozen" if (root / "uv.lock").is_file() else ""
    args = _pytest_args(command, output)
    tail = f" {args}" if args else ""
    return (
        "unset VIRTUAL_ENV && "
        "export UV_PROJECT_ENVIRONMENT=/tmp/lbw-testbed && "
        f"uv sync{frozen} --all-extras --all-groups && "
        f"uv pip install --python /tmp/lbw-testbed/bin/python {_q(package)} && "
        f"PYTHONPATH=. /tmp/lbw-testbed/bin/python -m pytest{tail}"
    )


def _node_bed(workspace: Workspace, output: str, timeout_seconds: int) -> ToolResult | None:
    root = workspace.root
    manifest = root / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    package = _missing_package(output)
    script = _node_script(data, package)
    if not script:
        return None
    bed = _bed(root, "node")
    ready = bed / "node_modules" / ".bin"
    stamp = _stamp(_node_inputs(root))
    run = f"env PATH={_q(ready)}:$PATH {script}"
    if _reusable(bed, stamp, ready):
        record_event(kind="step", message="Reusing the throwaway Node environment.")
        return run_command(workspace, run, timeout_seconds)
    record_event(
        kind="step", message=f"Building a throwaway Node environment for {package or script}."
    )
    if _stage_node(root, bed) and _node_install(workspace, bed, package, data, timeout_seconds):
        _write_stamp(bed, stamp)
        record_event(kind="step", message="Running the suite in the throwaway Node environment.")
        return run_command(workspace, run, timeout_seconds)
    if _docker_available():
        inner = _node_docker_inner(root, script, package, data)
        return _docker(workspace, "node", inner, timeout_seconds)
    record_event(kind="step", message="Could not build a throwaway Node environment.")
    _discard(bed)
    return None


def _node_docker_inner(root: Path, script: str, package: str | None, data: dict) -> str:
    copy = "cp /workspace/package.json /tmp/lbw-testbed/"
    install = "npm install"
    if (root / "package-lock.json").is_file():
        copy += " && cp /workspace/package-lock.json /tmp/lbw-testbed/"
        install = "npm ci"
    extra = ""
    if package and package not in _node_deps(data) and _SAFE_NAME.fullmatch(package):
        extra = f" && npm install {_q(package)}"
    return (
        "mkdir -p /tmp/lbw-testbed && "
        f"{copy} && cd /tmp/lbw-testbed && {install}{extra} && "
        "cd /workspace && PATH=/tmp/lbw-testbed/node_modules/.bin:$PATH "
        f"{script}"
    )


def _node_install(
    workspace: Workspace,
    bed: Path,
    package: str | None,
    data: dict,
    timeout_seconds: int,
) -> bool:
    manager = _node_manager(workspace.root)
    tool = {"npm": "npm", "pnpm": "pnpm", "yarn": "yarn"}[manager]
    if shutil.which(tool) is None:
        return False
    install = {
        "npm": "npm ci" if (bed / "package-lock.json").is_file() else "npm install",
        "pnpm": "pnpm install --frozen-lockfile"
        if (bed / "pnpm-lock.yaml").is_file()
        else "pnpm install",
        "yarn": "yarn install --frozen-lockfile"
        if (bed / "yarn.lock").is_file()
        else "yarn install",
    }[manager]
    deps = _node_deps(data)
    extra = ""
    if package and package not in deps and _SAFE_NAME.fullmatch(package):
        extra = f" && {tool} install {_q(package)}"
    command = f"cd {_q(bed)} && {install}{extra}"
    return (
        run_command(workspace, command, timeout_seconds).ok
        and (bed / "node_modules" / ".bin").is_dir()
    )


def _cached_tool(
    workspace: Workspace,
    *,
    kind: str,
    label: str,
    tool: str | None,
    setup: str | None,
    run: str,
    docker_inner: str,
    timeout_seconds: int,
) -> ToolResult | None:
    bed = _bed(workspace.root, kind)
    ready = bed / ".ready"
    inputs = _manifests(workspace.root, kind)
    stamp = _stamp(inputs)
    local = tool is None or shutil.which(tool) is not None
    if local and _reusable(bed, stamp, ready):
        record_event(kind="step", message=f"Reusing the throwaway {label} environment.")
        return run_command(workspace, run, timeout_seconds)
    if local:
        record_event(kind="step", message=f"Building a throwaway {label} environment.")
        _discard(bed)
        bed.mkdir(parents=True, exist_ok=True)
        if setup is None or run_command(workspace, setup, timeout_seconds).ok:
            ready.write_text("ok\n", encoding="utf-8")
            _write_stamp(bed, stamp)
            record_event(
                kind="step", message=f"Running the suite in the throwaway {label} environment."
            )
            return run_command(workspace, run, timeout_seconds)
        _discard(bed)
    if _docker_available():
        return _docker(workspace, kind, docker_inner, timeout_seconds)
    record_event(kind="step", message=f"Could not build a throwaway {label} environment.")
    return None


def _docker(workspace: Workspace, kind: str, inner: str, timeout_seconds: int) -> ToolResult:
    image = _IMAGES[kind]
    record_event(kind="step", message=f"Running tests in a throwaway {kind} container.")
    command = (
        "docker run --rm --network bridge --cap-drop=ALL "
        "--security-opt=no-new-privileges "
        f"-v {_q(workspace.root.resolve())}:/workspace -w /workspace "
        f"{image} bash -lc {_q(inner)}"
    )
    return run_command(workspace, command, timeout_seconds)


def _kind(root: Path, output: str, command: str) -> str | None:
    package = _missing_package(output)
    text = f"{command}\n{output}".lower()
    if package in _PYTHON_PACKAGES and (_python_project(root) or _pytest_args(command, output)):
        return "python"
    if package in _NODE_RUNNERS and (root / "package.json").is_file():
        return "node"
    if (root / "package.json").is_file() and (
        "missing script" in text or "no test specified" in text or "cannot find module" in text
    ):
        return "node"
    if (root / "go.mod").is_file() and re.search(r"\bgo\b", text) and "not found" in text:
        return "go"
    if (root / "Cargo.toml").is_file() and "cargo" in text and "not found" in text:
        return "rust"
    if (
        (root / "Gemfile").is_file()
        and ("rspec" in text or "bundle" in text)
        and "not found" in text
    ):
        return "ruby"
    if (root / "composer.json").is_file() and "phpunit" in text and "not found" in text:
        return "php"
    if _maven_project(root) and re.search(r"\bmvn\b", text) and "not found" in text:
        return "maven"
    if _gradle_project(root) and "gradle" in text and "not found" in text:
        return "gradle"
    return None


def _missing_package(output: str) -> str | None:
    match = re.search(
        r"No module named ['\"]?(pytest|nose|tox|nose2)['\"]?",
        output or "",
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower().replace("_", "-")
    for name in ("pytest", "jest", "vitest", "mocha"):
        if re.search(
            rf"\b{name}\b[^\n]{{0,40}}(?:not found|command not found)",
            output or "",
            re.IGNORECASE,
        ):
            return name
    return None


def _python_project(root: Path) -> bool:
    names = (
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
    )
    return any((root / name).is_file() for name in names)


def _python_inputs(root: Path) -> list[Path]:
    names = (
        "pyproject.toml",
        "uv.lock",
        "setup.py",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
    )
    return [root / name for name in names]


def _node_inputs(root: Path) -> list[Path]:
    names = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
    return [root / name for name in names]


def _manifests(root: Path, kind: str) -> list[Path]:
    names = {
        "go": ("go.mod", "go.sum"),
        "rust": ("Cargo.toml", "Cargo.lock"),
        "ruby": ("Gemfile", "Gemfile.lock"),
        "php": ("composer.json", "composer.lock"),
        "maven": ("pom.xml",),
        "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    }[kind]
    return [root / name for name in names]


def _editable_spec(root: Path, package: str) -> str:
    extras = _declared_extras(root, package)
    if not extras:
        return "."
    return f".[{','.join(extras)}]"


def _declared_extras(root: Path, package: str) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    optional = project.get("optional-dependencies") if isinstance(project, dict) else {}
    if not isinstance(optional, dict):
        return []
    declared = [
        str(name)
        for name, deps in optional.items()
        if _SAFE_NAME.fullmatch(str(name)) and _declares(deps, package)
    ]
    if not declared:
        declared = [
            name for name in ("dev", "test") if name in optional and _SAFE_NAME.fullmatch(name)
        ]
    rank = {"dev": 0, "test": 1}
    return sorted(set(declared), key=lambda name: (rank.get(name, 2), name))


def _declares(deps: object, package: str) -> bool:
    if not isinstance(deps, list):
        return False
    want = package.lower().replace("_", "-")
    for dep in deps:
        if not isinstance(dep, str):
            continue
        name = re.split(r"[\[<>=!; \t]", dep.strip(), maxsplit=1)[0]
        if name.lower().replace("_", "-") == want:
            return True
    return False


def _requirement_flags(root: Path) -> str:
    flags = []
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        if (root / name).is_file():
            flags.append(f"-r {_q(name)}")
    if (root / "setup.py").is_file() or (root / "pyproject.toml").is_file():
        flags.append("-e .")
    return " ".join(flags)


def _pytest_args(command: str, output: str) -> str:
    for source in (output, command):
        for line in reversed((source or "").splitlines()):
            stripped = line.strip()
            if "no module named" in stripped.lower() or stripped.startswith("make:"):
                continue
            match = _PYTEST_LINE.match(stripped)
            if match is None:
                continue
            args = match.group("args").strip()
            if args and not all(_SAFE_ARG.fullmatch(part) for part in args.split()):
                continue
            return args
    return ""


def _python_rerun(command: str, output: str, python: Path) -> str:
    env = _safe_env(output) or "PYTHONPATH=."
    args = _pytest_args(command, output)
    tail = f" {args}" if args else ""
    return f"env -u VIRTUAL_ENV {env} {_q(python)} -m pytest{tail}"


def _safe_env(output: str) -> str:
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not _PYTEST_LINE.match(stripped):
            continue
        kept = []
        for token in stripped.split():
            match = _ENV_ASSIGN.match(token)
            if match is None:
                break
            if not _SAFE_ARG.fullmatch(match.group("value")) and match.group("value") != "":
                break
            kept.append(token)
        if kept:
            return " ".join(kept)
    return ""


def _node_script(data: dict, package: str | None) -> str | None:
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(script, str):
        body = script.strip()
        if body and "no test specified" not in body and "\n" not in body and len(body) <= 500:
            return body
    if package in _NODE_RUNNERS:
        return package
    deps = _node_deps(data)
    for name in _NODE_RUNNERS:
        if name in deps:
            return name
    return None


def _node_deps(data: dict) -> dict:
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    dev = data.get("devDependencies") if isinstance(data.get("devDependencies"), dict) else {}
    return {**deps, **dev}


def _node_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _stage_node(root: Path, bed: Path) -> bool:
    try:
        _discard(bed)
        bed.mkdir(parents=True)
        for name in ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            source = root / name
            if source.is_file():
                shutil.copy2(source, bed / name)
    except OSError:
        return False
    return (bed / "package.json").is_file()


def _go_command(root: Path) -> str:
    cache = _bed(root, "go-build")
    modules = _bed(root, "go-mod")
    return f"env GOCACHE={_q(cache)} GOMODCACHE={_q(modules)} go test ./..."


def _wrapper(root: Path, wrapper: str, binary: str) -> str:
    if (root / wrapper).is_file():
        return f"./{wrapper}"
    return binary


def _maven_project(root: Path) -> bool:
    return (root / "pom.xml").is_file() or (root / "mvnw").is_file()


def _gradle_project(root: Path) -> bool:
    return any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts", "gradlew"))


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _bed(root: Path, kind: str) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"lbw-testbed-{digest}-{kind}"


def _stamp(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _stamp_path(bed: Path) -> Path:
    return bed.with_name(bed.name + ".stamp")


def _reusable(bed: Path, stamp: str, marker: Path) -> bool:
    path = _stamp_path(bed)
    if not marker.exists() or not path.is_file():
        return False
    try:
        return path.read_text(encoding="utf-8") == stamp
    except OSError:
        return False


def _write_stamp(bed: Path, stamp: str) -> None:
    try:
        _stamp_path(bed).write_text(stamp, encoding="utf-8")
    except OSError:
        return


def _discard(bed: Path) -> None:
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = bed.resolve()
    except OSError:
        return
    if resolved != temp and temp not in resolved.parents:
        return
    if bed.name.startswith("lbw-testbed-") and bed.exists():
        shutil.rmtree(bed, ignore_errors=True)
    stamp = _stamp_path(bed)
    if stamp.name.startswith("lbw-testbed-"):
        stamp.unlink(missing_ok=True)


def _q(value: object) -> str:
    return shlex.quote(str(value))


@dataclass(frozen=True)
class _Toolchain:
    name: str
    package: str
    commands: tuple[str, ...]
    # Commands that run through another one, such as pnpm through corepack.
    through: tuple[tuple[str, str], ...] = ()


_TOOLCHAINS = (
    _Toolchain("uv", "uv", ("uv", "uvx")),
    _Toolchain(
        "Node",
        "nodejs-wheel",
        ("node", "npm", "npx", "corepack"),
        through=(("pnpm", "corepack pnpm"), ("yarn", "corepack yarn")),
    ),
)
_BY_COMMAND = {
    command: chain
    for chain in _TOOLCHAINS
    for command in (*chain.commands, *(name for name, _ in chain.through))
}
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "make"})
# "make: uv: No such file or directory", "/bin/sh: 1: npm: not found", or
# "bash: npm: command not found".
_MISSING = re.compile(
    rf"(?im)(?:^|[\s:/])({_NAME}):\s*(?:command not found|not found|No such file or directory)\b"
)
# "zsh: command not found: npm".
_MISSING_AFTER = re.compile(rf"(?im)command not found:\s*({_NAME})\b")
_NODE_MISSING_MODULE = re.compile(r"Cannot find module|ERR_MODULE_NOT_FOUND|Cannot find package")
_SKIP_DIRS = {"node_modules", ".git", ".lbw", ".venv", "venv", "dist", "build"}
_LOCKS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")
_ROUNDS = len(_TOOLCHAINS) + 1


def missing_commands(output: str) -> list[str]:
    """Each command the shell said it could not find, in the order it said so."""
    text = output or ""
    matches = [*_MISSING.finditer(text), *_MISSING_AFTER.finditer(text)]
    found: list[str] = []
    for match in sorted(matches, key=lambda item: item.start(1)):
        name = match.group(1)
        if name not in found and name not in _SHELLS:
            found.append(name)
    return found


def toolchain_missing(output: str, root: Path | None = None) -> bool:
    """The command called a toolchain this machine does not have, such as uv or npm.

    With the checkout's root, a Node package whose dependencies are not
    installed counts too.
    """
    if any(name in _BY_COMMAND for name in missing_commands(output)):
        return True
    return root is not None and _needs_node_modules(root, output, {})


def run_with_throwaway_toolchains(
    workspace: Workspace,
    command: str,
    output: str,
    timeout_seconds: int,
) -> ToolResult | None:
    """Install what the command could not find and run it again.

    Returns None when nothing could be installed, so the caller keeps the
    first result.
    """
    try:
        return _install_and_rerun(workspace, command, output, timeout_seconds)
    except OSError as exc:
        record_event(kind="step", message=f"Could not set up a throwaway toolchain: {exc}")
        return None


def _install_and_rerun(
    workspace: Workspace,
    command: str,
    output: str,
    timeout_seconds: int,
) -> ToolResult | None:
    provided: dict[str, Path] = {}
    linked = False
    result: ToolResult | None = None
    for _ in range(_ROUNDS):
        added = False
        for name in missing_commands(output):
            chain = _BY_COMMAND.get(name)
            if chain is None or chain.name in provided:
                continue
            shim = _ensure(workspace, chain, timeout_seconds)
            if shim is None:
                record_event(kind="step", message=f"Could not install a throwaway {chain.name}.")
                return result
            provided[chain.name] = shim
            added = True
        if not linked and _needs_node_modules(workspace.root, output, provided):
            linked = True
            added = _link_node_modules(workspace, provided, timeout_seconds) or added
        if not added:
            return result
        labels = ", ".join(sorted(provided))
        record_event(kind="step", message=f"Running the command again with the throwaway {labels}.")
        result = run_command(workspace, _rerun(workspace.root, provided, command), timeout_seconds)
        if result.ok:
            return result
        output = result.output or ""
    return result


def _ensure(workspace: Workspace, chain: _Toolchain, timeout_seconds: int) -> Path | None:
    """A directory holding only this toolchain's commands, so the project's python stays first."""
    bed = _bed(workspace.root, chain.package)
    tool = bed / "tool"
    shim = bed / "shim"
    if all((shim / name).exists() for name in chain.commands):
        record_event(kind="step", message=f"Reusing the throwaway {chain.name}.")
        return shim
    record_event(kind="step", message=f"Installing a throwaway {chain.name} outside the project.")
    _discard(bed)
    python = tool / "bin" / "python"
    command = (
        f"env -u VIRTUAL_ENV {_q(sys.executable)} -m venv {_q(tool)} && "
        f"env -u VIRTUAL_ENV {_q(python)} -m pip install --no-input --no-cache-dir "
        f"--disable-pip-version-check --only-binary=:all: {_q(chain.package)}"
    )
    installed = run_command(workspace, command, timeout_seconds)
    binaries = [tool / "bin" / name for name in chain.commands]
    if not installed.ok or not all(path.is_file() for path in binaries):
        _discard(bed)
        return None
    shim.mkdir(parents=True, exist_ok=True)
    for path in binaries:
        (shim / path.name).symlink_to(path)
    for name, runs in chain.through:
        script = shim / name
        script.write_text(f'#!/bin/sh\nexec {runs} "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    return shim


def _rerun(root: Path, provided: dict[str, Path], command: str) -> str:
    path = ":".join(_q(shim) for shim in provided.values())
    exports = [f"PATH={path}:$PATH"]
    if "uv" in provided:
        bed = _bed(root, "uv")
        exports += [
            f"UV_PROJECT_ENVIRONMENT={_q(bed / 'env')}",
            f"UV_CACHE_DIR={_q(bed / 'cache')}",
        ]
    if "Node" in provided:
        bed = _bed(root, "nodejs-wheel")
        exports += [
            f"npm_config_cache={_q(bed / 'cache')}",
            f"COREPACK_HOME={_q(bed / 'corepack')}",
        ]
    return f"unset VIRTUAL_ENV && export {' '.join(exports)} && {command}"


def _package_dirs(root: Path) -> list[Path]:
    """Directories with a package.json and a lock file whose dependencies LBW should
    install, three levels deep: no node_modules, or LBW's own link to an install
    that is gone or older than the manifest."""
    found: list[Path] = []
    queue = [(root, 0)]
    while queue:
        folder, depth = queue.pop(0)
        if (folder / "package.json").is_file() and any(
            (folder / lock).is_file() for lock in _LOCKS
        ):
            link = folder / "node_modules"
            if not link.exists() and not link.is_symlink():
                found.append(folder)
            elif _our_link(link) and not _current_install(root, folder):
                found.append(folder)
        if depth >= 3:
            continue
        try:
            children = sorted(item for item in folder.iterdir() if item.is_dir())
        except OSError:
            continue
        queue.extend(
            (child, depth + 1)
            for child in children
            if child.name not in _SKIP_DIRS and not child.name.startswith(".")
        )
    return found


def _needs_node_modules(root: Path, output: str, provided: dict[str, Path]) -> bool:
    """npm ran, and a missing command or module says the dependencies are not installed."""
    if "Node" not in provided and shutil.which("npm") is None:
        return False
    if not _package_dirs(root):
        return False
    missing = [name for name in missing_commands(output) if name not in _BY_COMMAND]
    return bool(missing) or _NODE_MISSING_MODULE.search(output or "") is not None


def _modules_bed(root: Path, folder: Path) -> Path:
    relative = folder.relative_to(root).as_posix()
    return _bed(root, f"node-modules-{hashlib.sha256(relative.encode()).hexdigest()[:12]}")


def _manifest_stamp(folder: Path) -> str:
    return _stamp([folder / name for name in ("package.json", *_LOCKS)])


def _current_install(root: Path, folder: Path) -> bool:
    bed = _modules_bed(root, folder)
    modules = bed / "node_modules"
    return modules.is_dir() and _reusable(bed, _manifest_stamp(folder), modules)


def _our_link(link: Path) -> bool:
    """A node_modules link LBW made into its throwaway directory, even a broken one."""
    if not link.is_symlink():
        return False
    try:
        target = Path(os.readlink(link))
    except OSError:
        return False
    return any(part.startswith("lbw-testbed-") for part in target.parts)


def _link_node_modules(
    workspace: Workspace, provided: dict[str, Path], timeout_seconds: int
) -> bool:
    linked = False
    root = workspace.root
    for folder in _package_dirs(root):
        relative = folder.relative_to(root)
        bed = _modules_bed(root, folder)
        stamp = _manifest_stamp(folder)
        modules = bed / "node_modules"
        if not _current_install(root, folder):
            record_event(
                kind="step",
                message=f"Installing {relative or '.'} dependencies outside the project.",
            )
            _discard(bed)
            bed.mkdir(parents=True)
            for name in ("package.json", *_LOCKS):
                if (folder / name).is_file():
                    shutil.copy2(folder / name, bed / name)
            install = _node_install_command(folder)
            ran = run_command(
                workspace, _rerun(root, provided, f"cd {_q(bed)} && {install}"), timeout_seconds
            )
            if not ran.ok or not modules.is_dir():
                record_event(
                    kind="step", message=f"Could not install {relative or '.'} dependencies."
                )
                _discard(bed)
                continue
            _write_stamp(bed, stamp)
        _exclude(root, relative / "node_modules")
        link = folder / "node_modules"
        if _our_link(link):
            link.unlink()
        link.symlink_to(modules, target_is_directory=True)
        linked = True
    return linked


def _node_install_command(folder: Path) -> str:
    if (folder / "pnpm-lock.yaml").is_file():
        return "corepack pnpm install --frozen-lockfile"
    if (folder / "yarn.lock").is_file():
        return "corepack yarn install --frozen-lockfile"
    return "npm ci --no-audit --no-fund"


def _exclude(root: Path, relative: Path) -> None:
    """Keep the link out of every commit, without editing the project's .gitignore."""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    exclude = git_dir / "info" / "exclude"
    line = "/" + relative.as_posix()
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    except OSError:
        return
    if line in current.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not current or current.endswith("\n") else "\n"
    exclude.write_text(f"{current}{prefix}{line}\n", encoding="utf-8")
