from __future__ import annotations

from pathlib import Path

from agent_lbw.sandbox import Workspace
from agent_lbw.tools.files import file_tools


def _tool(workspace: Workspace, name: str):
    return next(tool for tool in file_tools(workspace) if tool.name == name)


def test_write_file_rejects_a_config_command_with_a_bare_colon(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.parent.mkdir()
    config.write_text("name: demo\n", encoding="utf-8")
    command = "python3 -c \"with Server(('', PORT), Handler) as httpd: print('up')\""
    result = _tool(workspace, "write_file").handler(
        path=".lbw/config.yaml",
        content=f"name: demo\npreview_command: {command}\n",
    )
    assert not result.ok
    assert "configure_project" in result.output
    assert config.read_text(encoding="utf-8") == "name: demo\n"


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    written = _tool(workspace, "write_file").handler(path="hello.txt", content="hi\n")
    assert written.ok
    listed = _tool(workspace, "list_dir").handler(path=".")
    assert "hello.txt" in listed.output
    read = _tool(workspace, "read_file").handler(path="hello.txt")
    assert read.ok
    assert "hi" in read.output
    assert read.output.startswith("hello.txt  lines 1-1 of 1")


def test_str_replace_strips_numbered_read_prefixes(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    result = _tool(workspace, "str_replace").handler(
        path="app.py",
        old_string="   1|def add(a, b):\n   2|    return a + b",
        new_string="def add(a, b):\n    return a - b",
    )
    assert result.ok
    assert "ignored numbered read_file prefixes" in result.output
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a - b\n"
    )


def test_str_replace_missing_text_includes_nearby_lines(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "note.txt").write_text("alpha lbw beta\n", encoding="utf-8")
    missing = _tool(workspace, "str_replace").handler(
        path="note.txt",
        old_string="gamma",
        new_string="delta",
    )
    assert not missing.ok
    assert "not found" in missing.output
    assert "File starts with:" in missing.output
    assert "alpha lbw beta" in missing.output


def test_write_file_records_create_and_update_diffs(tmp_path: Path) -> None:
    from agent_lbw.progress import bind_progress, current_events, reset_progress

    workspace = Workspace(tmp_path)
    token = bind_progress()
    try:
        created = _tool(workspace, "write_file").handler(path="note.txt", content="alpha\n")
        assert created.ok
        updated = _tool(workspace, "write_file").handler(path="note.txt", content="beta\n")
        assert updated.ok
        events = current_events()
    finally:
        reset_progress(token)
    assert [event["action"] for event in events] == ["created", "updated"]
    assert events[0]["path"] == "note.txt"
    assert events[0]["diff"].startswith("--- /dev/null")
    assert "+alpha" in events[0]["diff"]
    assert "--- a/note.txt" in events[1]["diff"]
    assert "-alpha" in events[1]["diff"]
    assert "+beta" in events[1]["diff"]
    assert events[1]["net_action"] == "created"
    assert events[1]["net_diff"].startswith("--- /dev/null")
    assert "+beta" in events[1]["net_diff"]
    assert "-alpha" not in events[1]["net_diff"]
    assert events[0]["at"].endswith("Z")


def test_str_replace_updates_unique_match(tmp_path: Path) -> None:
    from agent_lbw.progress import bind_progress, current_events, reset_progress

    workspace = Workspace(tmp_path)
    (tmp_path / "ui.html").write_text("<header>old</header>\n<main></main>\n", encoding="utf-8")
    token = bind_progress()
    try:
        result = _tool(workspace, "str_replace").handler(
            path="ui.html",
            old_string="<header>old</header>",
            new_string="<header>new</header>",
        )
        second = _tool(workspace, "str_replace").handler(
            path="ui.html",
            old_string="<main></main>",
            new_string="<main>body</main>",
        )
        events = current_events()
    finally:
        reset_progress(token)
    assert result.ok
    assert "1 replacement" in result.output
    assert second.ok
    assert (tmp_path / "ui.html").read_text(encoding="utf-8") == (
        "<header>new</header>\n<main>body</main>\n"
    )
    assert events[0]["action"] == "updated"
    assert "-<header>old</header>" in events[0]["diff"]
    assert "+<header>new</header>" in events[0]["diff"]
    assert len(events) == 2
    assert "-<main></main>" in events[1]["diff"]
    assert "+<main>body</main>" in events[1]["diff"]
    assert "-<header>old</header>" in events[1]["net_diff"]
    assert "+<header>new</header>" in events[1]["net_diff"]
    assert "+<main>body</main>" in events[1]["net_diff"]
    assert events[1]["net_action"] == "updated"


def test_str_replace_requires_unique_match_unless_replace_all(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "note.txt").write_text("alpha\nalpha\n", encoding="utf-8")
    missing = _tool(workspace, "str_replace").handler(
        path="note.txt",
        old_string="beta",
        new_string="gamma",
    )
    assert not missing.ok
    assert "not found" in missing.output
    ambiguous = _tool(workspace, "str_replace").handler(
        path="note.txt",
        old_string="alpha",
        new_string="beta",
    )
    assert not ambiguous.ok
    assert "2 times" in ambiguous.output
    replaced = _tool(workspace, "str_replace").handler(
        path="note.txt",
        old_string="alpha",
        new_string="beta",
        replace_all="true",
    )
    assert replaced.ok
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "beta\nbeta\n"


def test_search_finds_literal(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "note.txt").write_text("alpha lbw beta\n", encoding="utf-8")
    result = _tool(workspace, "search_text").handler(query="lbw")
    assert result.ok
    assert "note.txt" in result.output


def test_glob_files_finds_a_name_and_skips_dependencies(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    nested = tmp_path / "node_modules" / "leftpad"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    found = _tool(workspace, "glob_files").handler(pattern="*.py")
    assert found.ok
    assert "src/app.py" in found.output
    assert "node_modules" not in found.output
    scoped = _tool(workspace, "glob_files").handler(pattern="src/**/*.py")
    assert scoped.ok
    assert "src/app.py" in scoped.output


def test_search_can_stay_in_one_directory_and_show_context(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("before\nlbw_token\nafter\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("lbw_token\n", encoding="utf-8")
    scoped = _tool(workspace, "search_text").handler(query="lbw_token", path="src")
    assert scoped.ok
    assert "src/app.py" in scoped.output
    assert "other.py" not in scoped.output
    around = _tool(workspace, "search_text").handler(
        query="lbw_token", path="src/app.py", context=1
    )
    assert around.ok
    assert "before" in around.output
    assert "after" in around.output


def test_delete_file_removes_one_file_and_refuses_a_directory(tmp_path: Path) -> None:
    from agent_lbw.progress import bind_progress, current_events, reset_progress

    workspace = Workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "old.py").write_text("gone = True\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    token = bind_progress()
    try:
        removed = _tool(workspace, "delete_file").handler(path="src/old.py")
        events = current_events()
    finally:
        reset_progress(token)
    assert removed.ok
    assert not (tmp_path / "src" / "old.py").exists()
    assert events[-1]["action"] == "deleted"
    assert events[-1]["path"] == "src/old.py"
    assert "-gone = True" in events[-1]["diff"]
    directory = _tool(workspace, "delete_file").handler(path="src")
    assert not directory.ok
    assert "not a file" in directory.output
    secret = _tool(workspace, "delete_file").handler(path=".env")
    assert not secret.ok
    assert (tmp_path / ".env").is_file()
