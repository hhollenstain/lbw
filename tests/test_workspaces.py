from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support import init_git_repo

from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.github_sync import recorded_workspaces, restore_checkouts
from agent_lbw.runtime.store.projects import ensure_project
from agent_lbw.runtime.workspaces import (
    archive_workspace,
    browse_directory,
    clone_workspace,
    create_workspace,
    default_checkout_parent,
    durable_checkout_parent,
    forget_workspace,
    last_workspace,
    load_archived_workspaces,
    load_workspaces,
    remember_workspace,
    repo_label,
    repo_name_from_url,
)


def test_repo_label_uses_the_owner_and_name() -> None:
    assert repo_label("https://github.com/org/demo.git") == "org/demo"
    assert repo_label("git@github.com:org/foo-bar.git") == "org/foo-bar"
    assert repo_label("ssh://git@github.com/org/demo.git") == "org/demo"
    assert repo_label("", "local-folder") == "local-folder"


def test_repo_name_from_url() -> None:
    assert repo_name_from_url("https://github.com/org/demo.git") == "demo"
    assert repo_name_from_url("git@github.com:org/foo-bar.git") == "foo-bar"
    assert repo_name_from_url("   ") == "workspace"


def test_browse_lists_visible_directories(tmp_path: Path) -> None:
    (tmp_path / "keep").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "keep" / ".git").mkdir()
    listing = browse_directory(tmp_path)
    names = [item["name"] for item in listing["entries"]]
    assert names == ["keep"]
    assert listing["entries"][0]["is_git"] is True
    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.parent.resolve())
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="not a directory"):
        browse_directory(missing)


def test_remember_and_restore_workspaces(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    saved = remember_workspace(tmp_path, first)
    assert saved[0]["path"] == str(first.resolve())
    saved = remember_workspace(tmp_path, second)
    assert [item["path"] for item in saved[:2]] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    again = remember_workspace(tmp_path, first)
    assert [item["path"] for item in again[:2]] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    payload = json.loads((tmp_path / ".lbw" / "workspaces.json").read_text(encoding="utf-8"))
    assert payload["last"] == str(first.resolve())
    gitignore = (tmp_path / ".lbw" / ".gitignore").read_text(encoding="utf-8")
    assert "*" in gitignore
    assert ".lbw/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert last_workspace(tmp_path) == str(first.resolve())
    gone = tmp_path / "gone"
    (tmp_path / ".lbw" / "workspaces.json").write_text(
        json.dumps({"last": str(gone), "workspaces": [{"path": str(gone)}]}),
        encoding="utf-8",
    )
    assert last_workspace(tmp_path, default=first) == str(first.resolve())
    loaded = load_workspaces(tmp_path, default=first)
    assert [item["path"] for item in loaded] == [str(first.resolve())]


def test_archive_and_forget_workspaces(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    third = tmp_path / "three"
    first.mkdir()
    second.mkdir()
    third.mkdir()
    remember_workspace(tmp_path, first)
    remember_workspace(tmp_path, second)
    remember_workspace(tmp_path, third)
    saved = archive_workspace(tmp_path, second)
    assert [item["path"] for item in saved] == [
        str(first.resolve()),
        str(third.resolve()),
    ]
    archived = load_archived_workspaces(tmp_path)
    assert [item["path"] for item in archived] == [str(second.resolve())]
    payload = json.loads((tmp_path / ".lbw" / "workspaces.json").read_text(encoding="utf-8"))
    assert payload["last"] == str(third.resolve())
    restored = remember_workspace(tmp_path, second)
    assert [item["path"] for item in restored] == [
        str(first.resolve()),
        str(third.resolve()),
        str(second.resolve()),
    ]
    assert load_archived_workspaces(tmp_path) == []
    archive_workspace(tmp_path, first)
    forgotten = forget_workspace(tmp_path, first)
    assert [item["path"] for item in forgotten] == [
        str(third.resolve()),
        str(second.resolve()),
    ]
    assert load_archived_workspaces(tmp_path) == []
    assert first.exists()
    archive_workspace(tmp_path, second)
    assert [item["path"] for item in load_workspaces(tmp_path)] == [str(third.resolve())]
    with pytest.raises(ValueError, match="keep at least one"):
        archive_workspace(tmp_path, third)
    with pytest.raises(ValueError, match="keep at least one"):
        forget_workspace(tmp_path, third)
    assert third.exists()
    assert last_workspace(tmp_path) == str(third.resolve())


def test_create_workspace_inits_lbw_and_git(tmp_path: Path) -> None:
    target = tmp_path / "new-app"
    created = create_workspace(target)
    assert created["path"] == str(target.resolve())
    assert created["is_lbw"] is True
    assert created["is_git"] is True
    assert created["git_remote"] == ""
    assert (target / ".lbw" / "config.yaml").exists()
    assert (target / ".lbw" / "guidelines.md").exists()
    assert (target / ".git").exists()


def test_clone_workspace_from_local_repo(tmp_path: Path) -> None:
    source = tmp_path / "upstream"
    source.mkdir()
    (source / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(source)
    parent = tmp_path / "projects"
    cloned = clone_workspace(str(source), parent, name="checkout")
    dest = parent / "checkout"
    assert cloned["path"] == str(dest.resolve())
    assert (dest / "readme.txt").read_text(encoding="utf-8") == "hello\n"
    assert cloned["is_lbw"] is True
    assert cloned["is_git"] is True
    assert cloned["git_remote"]
    with pytest.raises(ValueError, match="already exists"):
        clone_workspace(str(source), parent, name="checkout")
    with pytest.raises(ValueError, match="repository URL"):
        clone_workspace("  ", parent)


def test_a_container_home_is_not_a_durable_checkout(tmp_path: Path) -> None:
    store = tmp_path / "workspaces"
    home = tmp_path / "home"
    assert default_checkout_parent(store, home=home) == store.resolve()
    assert (
        durable_checkout_parent(store, str(home / "projects"), home=home)
        == (store / "projects").resolve()
    )
    local = home / "work"
    assert default_checkout_parent(local, home=home) == (home / "projects").resolve()
    assert (
        durable_checkout_parent(local, str(home / "projects"), home=home)
        == (home / "projects").resolve()
    )


def test_a_missing_github_checkout_is_restored_onto_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "workspaces"
    store.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr(
        "agent_lbw.runtime.workspaces.Path.home",
        classmethod(lambda cls: home),
    )
    account = load_account(store)
    kept = store / "kept"
    kept.mkdir()
    with database(store) as db:
        ensure_project(
            db,
            account["org"]["id"],
            name="kept",
            kind="folder",
            locator=str(kept),
        )
        ensure_project(
            db,
            account["org"]["id"],
            name="demo",
            kind="github",
            locator=str(home / "projects" / "demo"),
        )

    def fake_repos(_db, _root):
        return [
            {"full_name": "acme/demo", "private": False},
            {"full_name": "other/demo", "private": False},
        ]

    monkeypatch.setattr("agent_lbw.runtime.store.github_sync.list_repos", fake_repos)
    restore_checkouts(store)
    with database(store) as db:
        row = db.fetchone("SELECT locator FROM projects WHERE name = ?", ("demo",))
    assert row is not None
    assert row["locator"] == str(home / "projects" / "demo")

    def one_repo(_db, _root):
        return [{"full_name": "acme/demo", "private": True}]

    def fake_clone(url, parent, *, name=None, git_config=None):
        assert url == "https://github.com/acme/demo.git"
        assert Path(parent) == store.resolve()
        assert git_config is None
        dest = Path(parent) / (name or "demo")
        dest.mkdir(parents=True)
        return {"path": str(dest)}

    monkeypatch.setattr("agent_lbw.runtime.store.github_sync.list_repos", one_repo)
    monkeypatch.setattr("agent_lbw.runtime.workspaces.clone_workspace", fake_clone)
    restore_checkouts(store)
    with database(store) as db:
        row = db.fetchone("SELECT locator FROM projects WHERE name = ?", ("demo",))
    assert row is not None
    assert row["locator"] == "acme/demo"
    assert (store / "demo").is_dir()
    paths = {item["path"] for item in recorded_workspaces(store)}
    assert "acme/demo" in paths or str((store / "demo").resolve()) in paths
    assert str(kept.resolve()) in paths
