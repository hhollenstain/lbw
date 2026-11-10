"""A gitignored copy of the GitHub App for this machine.

`make wipe` drops the database. The private key is not stored in git or in
`.lbw/`. Export writes it to `github.local.yaml`. Hydrate seals that file
back onto the install.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.crypto import open_secret
from agent_lbw.runtime.store.github import (
    github_installation,
    restore_github_app,
    saved_installations,
)
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.tasks import tenant_dek

LOCAL_GITHUB_FILE = "github.local.yaml"


def export_github(root: Path, path: Path, *, database_url: str = "") -> Path:
    """Write the saved GitHub App to an untracked file. The file mode is 0600."""
    with database(root, database_url) as db:
        try:
            account = require_account(db)
        except ValueError as exc:
            raise ValueError("save the GitHub App before exporting it") from exc
        row = github_installation(db, account["org"]["id"])
        if row is None:
            raise ValueError("save the GitHub App before exporting it")
        dek = tenant_dek(db, root, account["org"]["id"])
        private_key = open_secret(dek, row["private_key_ciphertext"])
        secret = ""
        if row["client_secret_ciphertext"]:
            secret = open_secret(dek, row["client_secret_ciphertext"])
        document = _document(
            app_id=str(row["app_id"]),
            private_key=private_key,
            client_secret=secret,
            client_id=str(row["client_id"] or ""),
            installation_id=str(row["installation_id"] or ""),
            account_login=str(row["account_login"] or ""),
            account_type=str(row["account_type"] or ""),
            installations=saved_installations(db),
        )
    _write(path, document)
    return path


def hydrate_github(root: Path, path: Path, *, database_url: str = "") -> dict:
    """Seal the GitHub App from the local file onto this install."""
    if not path.is_file():
        raise ValueError(f"{path.name} is not on this machine")
    github = _read(path)
    load_account(root, database_url=database_url)
    with database(root, database_url) as db:
        return restore_github_app(
            db,
            root,
            app_id=_text(github.get("app_id")),
            private_key=_text(github.get("private_key")),
            client_secret=_text(github.get("client_secret")),
            client_id=_text(github.get("client_id")),
            installation_id=_text(github.get("installation_id")),
            account_login=_text(github.get("account_login")),
            account_type=_text(github.get("account_type")),
            installations=_installations(github.get("installations")),
        )


def assert_untracked(path: Path) -> None:
    """The GitHub App file stays out of git and out of `.lbw/`."""
    place = path.expanduser()
    if ".lbw" in place.parts:
        raise ValueError("keep the GitHub App out of .lbw")
    if _git_tracks(place):
        raise ValueError("that file is not ignored; choose a path git does not track")


def _document(
    *,
    app_id: str,
    private_key: str,
    client_secret: str,
    client_id: str,
    installation_id: str,
    account_login: str,
    account_type: str,
    installations: list[dict],
) -> dict:
    github: dict[str, object] = {
        "app_id": app_id,
        "private_key": LiteralScalarString(private_key.strip() + "\n"),
    }
    if client_id:
        github["client_id"] = client_id
    if client_secret:
        github["client_secret"] = client_secret
    if installation_id:
        github["installation_id"] = installation_id
    if account_login:
        github["account_login"] = account_login
    if account_type:
        github["account_type"] = account_type
    if installations:
        github["installations"] = [
            {
                "id": item["id"],
                "account_login": item["account_login"],
                "account_type": item["account_type"],
            }
            for item in installations
        ]
    return {"github": github}


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_untracked(path)
    yaml = YAML()
    yaml.default_flow_style = False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.dump(document, handle)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def _read(path: Path) -> dict:
    yaml = YAML(typ="safe")
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = yaml.load(handle)
    except Exception as exc:
        raise ValueError(f"{path.name} could not be read") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} has no github app")
    github = loaded.get("github")
    if not isinstance(github, dict):
        raise ValueError(f"{path.name} has no github app")
    return github


def _installations(value: object) -> list[dict] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("installations should be a list")
    found: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("an installation needs an id")
        found.append(dict(item))
    return found


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _git_tracks(path: Path) -> bool:
    """True when git would track this path. A folder outside a repo is kept."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", "--", path.name],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 1
