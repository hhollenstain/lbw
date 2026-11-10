"""The GitHub App can be copied to an untracked file and sealed back after a wipe."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

from agent_lbw.cli import app
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.local_github import LOCAL_GITHUB_FILE, assert_untracked
from agent_lbw.runtime.store.crypto import open_secret
from agent_lbw.runtime.store.github import restore_github_app
from agent_lbw.runtime.store.tasks import tenant_dek

runner = CliRunner()
_ENV = {
    "LBW_DATABASE_URL": "",
    "LBW_MODEL_NAME": "test-model",
    "LBW_MODEL_BASE_URL": "http://test:9/v1",
}


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _seed(root: Path, private_key: str) -> None:
    load_account(root)
    with database(root) as db:
        restore_github_app(
            db,
            root,
            app_id="5589431",
            private_key=private_key,
            client_secret="secret-one",
            client_id="Iv1.example",
            installation_id="178050691",
            account_login="acme",
            account_type="Organization",
        )


def test_the_default_file_is_gitignored() -> None:
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "github.local.yaml" in ignore
    assert LOCAL_GITHUB_FILE == "github.local.yaml"


def test_export_refuses_lbw_and_a_tracked_path(tmp_path: Path, monkeypatch) -> None:
    private_key = _pem()
    _seed(tmp_path, private_key)
    inside = tmp_path / ".lbw" / "github.yaml"
    refused = runner.invoke(
        app,
        ["export-github", "--workspace", str(tmp_path), "--file", str(inside)],
        env=_ENV,
    )
    assert refused.exit_code == 1
    assert "out of .lbw" in refused.stdout
    assert "BEGIN" not in refused.stdout
    assert not inside.exists()

    monkeypatch.setattr(
        "agent_lbw.runtime.local_github._git_tracks",
        lambda _path: True,
    )
    tracked = tmp_path / "tracked.yaml"
    refused = runner.invoke(
        app,
        ["export-github", "--workspace", str(tmp_path), "--file", str(tracked)],
        env=_ENV,
    )
    assert refused.exit_code == 1
    assert "not ignored" in refused.stdout
    assert not tracked.exists()


def test_export_and_hydrate_survive_a_wiped_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_request(*_args, **_kwargs):
        raise AssertionError("hydrate must not call GitHub")

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fail_request)
    private_key = _pem()
    _seed(tmp_path, private_key)
    seed = tmp_path / "github.local.yaml"
    exported = runner.invoke(
        app,
        ["export-github", "--workspace", str(tmp_path), "--file", str(seed)],
        env=_ENV,
    )
    assert exported.exit_code == 0
    assert "Saved the GitHub App" in exported.stdout
    assert "BEGIN" not in exported.stdout
    assert "secret-one" not in exported.stdout
    text = seed.read_text(encoding="utf-8")
    assert "5589431" in text
    assert "BEGIN RSA PRIVATE KEY" in text
    assert "secret-one" in text
    assert "178050691" in text
    assert seed.stat().st_mode & 0o777 == 0o600

    database_file = tmp_path / ".lbw" / "lbw.db"
    database_file.unlink()
    restored = runner.invoke(
        app,
        ["hydrate", "--workspace", str(tmp_path), "--file", str(seed)],
        env=_ENV,
    )
    assert restored.exit_code == 0
    assert "Restored GitHub App 5589431 on acme" in restored.stdout
    assert "BEGIN" not in restored.stdout
    assert "secret-one" not in restored.stdout

    account = load_account(tmp_path)
    with database(tmp_path) as db:
        row = db.fetchone(
            """
            SELECT app_id, private_key_ciphertext, client_secret_ciphertext,
                   client_id, installation_id, account_login
            FROM github_apps
            """
        )
        assert row is not None
        dek = tenant_dek(db, tmp_path, account["org"]["id"])
        assert open_secret(dek, row["private_key_ciphertext"]) == private_key.strip()
        assert open_secret(dek, row["client_secret_ciphertext"]) == "secret-one"
        assert private_key.strip() not in row["private_key_ciphertext"]
        listed = db.fetchone("SELECT installation_id, account_login FROM github_installations")
    assert row["app_id"] == "5589431"
    assert row["client_id"] == "Iv1.example"
    assert row["installation_id"] == "178050691"
    assert row["account_login"] == "acme"
    assert listed is not None
    assert listed["installation_id"] == "178050691"
    assert listed["account_login"] == "acme"


def test_hydrate_reads_a_hand_written_file(tmp_path: Path) -> None:
    private_key = _pem()
    seed = tmp_path / "github.local.yaml"
    seed.write_text(
        "github:\n"
        "  app_id: 123\n"
        "  client_id: Iv1.example\n"
        "  client_secret: secret-one\n"
        "  installation_id: 42\n"
        "  account_login: acme\n"
        "  account_type: Organization\n"
        "  private_key: |\n"
        + "".join(f"    {line}\n" for line in private_key.strip().splitlines())
        + "\n",
        encoding="utf-8",
    )
    restored = runner.invoke(
        app,
        ["hydrate", "--workspace", str(tmp_path), "--file", str(seed)],
        env=_ENV,
    )
    assert restored.exit_code == 0
    assert "Restored GitHub App 123 on acme" in restored.stdout
    with database(tmp_path) as db:
        row = db.fetchone("SELECT app_id, installation_id FROM github_apps")
    assert row is not None
    assert row["app_id"] == "123"
    assert row["installation_id"] == "42"


def test_hydrate_reports_a_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "github.local.yaml"
    result = runner.invoke(
        app,
        ["hydrate", "--workspace", str(tmp_path), "--file", str(missing)],
        env=_ENV,
    )
    assert result.exit_code == 1
    assert "not on this machine" in result.stdout


def test_a_path_under_lbw_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="out of .lbw"):
        assert_untracked(tmp_path / ".lbw" / LOCAL_GITHUB_FILE)
