"""File tools: read, search and edit the intended files; reject everything else."""

import subprocess

import pytest

from harness.errors import Denied, ToolError
from harness.repository import WorkingTree


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "ws"
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / ".git/config").write_text("secret\n")
    (root / "src/pkg/model.py").write_text("def f():\n    a = 1\n    return a\n")
    (root / "tests/test_model.py").write_text("assert True\n")
    (tmp_path / "outside.txt").write_text("host file\n")
    return WorkingTree(root, writable=["src/pkg/"])


def test_list_read_search_and_edit_the_intended_file(repository):
    assert repository.enumerate_files() == ["src/pkg/model.py", "tests/test_model.py"]
    assert repository.read_text("src/pkg/model.py").startswith("def f():")
    assert repository.search_text("a = 1") == [
        {"path": "src/pkg/model.py", "line": 2, "excerpt": "a = 1"}
    ]
    assert (
        repository.replace_text("src/pkg/model.py", "    a = 1\n", "    a = 2\n")
        == "edited src/pkg/model.py"
    )
    assert repository.read_text("src/pkg/model.py") == "def f():\n    a = 2\n    return a\n"


@pytest.mark.parametrize(
    "path", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", ".git/config", ""]
)
def test_paths_outside_the_allowed_area_are_rejected(repository, path):
    with pytest.raises(Denied):
        repository.read_text(path)
    with pytest.raises(Denied):
        repository.replace_text(path, "a", "b")


def test_edit_outside_the_writable_area_is_denied_but_reading_is_allowed(repository):
    with pytest.raises(Denied, match="outside the writable area"):
        repository.replace_text("tests/test_model.py", "True", "False")
    assert repository.read_text("tests/test_model.py") == "assert True\n"


def test_symlink_cannot_escape_the_repository_or_the_writable_area(repository, tmp_path):
    try:
        (repository.root / "src/pkg/out.txt").symlink_to(tmp_path / "outside.txt")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account lacks permission to create symlinks")
        raise
    (repository.root / "src/pkg/alias.py").symlink_to(repository.root / "tests/test_model.py")
    with pytest.raises(Denied):
        repository.read_text("src/pkg/out.txt")
    with pytest.raises(Denied):
        repository.replace_text("src/pkg/alias.py", "True", "False")
    assert (tmp_path / "outside.txt").read_text() == "host file\n"


def test_long_file_is_bounded_and_marked_as_shortened(repository):
    (repository.root / "src/pkg/big.txt").write_text("a" * 13_000)
    text = repository.read_text("src/pkg/big.txt")
    assert len(text) < 12_100 and "[shortened" in text
    with pytest.raises(ToolError, match="larger than"):
        repository.replace_text("src/pkg/big.txt", "a" * 13_000, "b")


def test_edit_must_identify_one_place(repository):
    (repository.root / "src/pkg/two.py").write_text("x = 1\ny = 1\n")
    with pytest.raises(ToolError, match="occurs 2 times"):
        repository.replace_text("src/pkg/two.py", "= 1", "= 2")
    with pytest.raises(ToolError, match="occurs 0 times"):
        repository.replace_text("src/pkg/two.py", "missing", "x")
    with pytest.raises(ToolError, match="not a file"):
        repository.replace_text("src/pkg/nope.py", "a", "b")


def test_unindented_old_is_matched_and_new_is_reindented(repository):
    result = repository.replace_text("src/pkg/model.py", "a = 1", "a = 3\nif a:\n    a = 4")
    assert "re-indented" in result
    assert (
        repository.read_text("src/pkg/model.py")
        == "def f():\n    a = 3\n    if a:\n        a = 4\n    return a\n"
    )


def test_python_that_does_not_parse_is_never_written(repository):
    with pytest.raises(ToolError) as error:
        repository.replace_text("src/pkg/model.py", "    return a", "    return (a")
    assert "SyntaxError" in error.value.args[0]["error"]
    assert repository.read_text("src/pkg/model.py") == "def f():\n    a = 1\n    return a\n"


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "pkg-repo"
    (root / "src/shop/domain").mkdir(parents=True)
    (root / "src/shop/domain/__init__.py").write_text("")
    (root / "src/shop/__init__.py").write_text("")
    (root / "src/shop/domain/events.py").write_text("class Paid:\n    pass\n")
    (root / "src/shop/domain/model.py").write_text(
        "from . import events\n\n\nclass Order:\n    def pay(self):\n        return events.Paid()\n"
    )
    (root / "src/shop/service.py").write_text(
        "from shop.domain import model\n\n\ndef pay(order: model.Order):\n    return order.pay()\n"
    )
    (root / "src/broken.py").write_text("def (:\n")
    (root / "README.md").write_text("Order docs\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start"],
        cwd=root,
        check=True,
    )
    return WorkingTree(root, writable=["src/"])


def test_find_symbol_returns_definitions_and_mentions(package):
    found = package.locate_symbol("Order")
    assert found["definitions"] == [
        {"path": "src/shop/domain/model.py", "line": 4, "kind": "class"}
    ]
    assert {entry["path"] for entry in found["references"]} == {
        "src/shop/domain/model.py",
        "src/shop/service.py",
    }
    with pytest.raises(ToolError, match="identifier"):
        package.locate_symbol("a b")


def test_dependencies_follow_absolute_and_relative_imports(package):
    assert package.import_relationships("src/shop/domain/model.py") == {
        "imports": ["src/shop/domain/events.py"],
        "imported_by": ["src/shop/service.py"],
    }
    with pytest.raises(ToolError, match="parsable"):
        package.import_relationships("src/broken.py")
    with pytest.raises(Denied):
        package.import_relationships("../outside.py")


def test_repo_map_is_compact_and_survives_unparsable_files(package):
    lines = package.symbol_outline()
    assert "src/shop/domain/model.py: class Order(pay)" in lines
    assert "src/shop/service.py: def pay" in lines
    assert "src/broken.py" in lines and "README.md" in lines


def test_git_state_shows_the_commit_and_the_edit(package):
    start = package.describe_changes()
    assert len(start["head"]) == 40 and start["changed_files"] == []
    package.replace_text("src/shop/service.py", "return order.pay()", "return None")
    state = package.describe_changes()
    assert state["changed_files"] == ["src/shop/service.py"]
    assert "service.py" in state["diff_stat"] and state["head"] == start["head"]
    assert not list(package.root.glob("src/shop/.edit-*"))  # the temporary file is gone
