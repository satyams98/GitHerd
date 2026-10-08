from pathlib import Path

from githerd.names import display_names


def test_unique_names_are_the_directory_name(tmp_path):
    repos = [tmp_path / "a" / "api", tmp_path / "b" / "docs"]
    assert display_names(repos, tmp_path) == {repos[0]: "api", repos[1]: "docs"}


def test_every_repo_sharing_a_name_gets_its_path_relative_to_the_root(tmp_path):
    api_a, api_b, docs = tmp_path / "team-a" / "api", tmp_path / "team-b" / "api", tmp_path / "docs"
    labels = display_names([api_a, api_b, docs], tmp_path)
    assert labels == {api_a: "team-a/api", api_b: "team-b/api", docs: "docs"}


def test_three_way_duplicates_all_get_the_relative_path(tmp_path):
    repos = [tmp_path / "x" / "lib", tmp_path / "y" / "lib", tmp_path / "lib"]
    assert display_names(repos, tmp_path) == {
        repos[0]: "x/lib", repos[1]: "y/lib", repos[2]: "lib",
    }


def test_a_duplicate_outside_the_root_falls_back_to_the_absolute_path_with_forward_slashes(tmp_path):
    root = tmp_path / "work"
    inside, outside = root / "a" / "api", tmp_path / "elsewhere" / "api"
    labels = display_names([inside, outside], root)
    assert labels[inside] == "a/api"
    assert labels[outside] == outside.as_posix()
    assert "\\" not in labels[outside]


def test_a_duplicate_that_is_the_root_itself_uses_the_absolute_path(tmp_path):
    root = tmp_path / "api"
    nested = root / "sub" / "api"
    labels = display_names([root, nested], root)
    assert labels[nested] == "sub/api"
    assert labels[root] == root.as_posix()  # "." would say nothing


OVERRIDE = chr(0x202E)  # a bidi override, built from its code point (no raw bidi controls in tests)
HOSTILE = f"evil{OVERRIDE}\x1b[2Jname"


def test_labels_are_sanitised(tmp_path):
    evil = tmp_path / HOSTILE
    other = tmp_path / "other" / HOSTILE
    labels = display_names([evil, other], tmp_path)
    for label in labels.values():
        assert "\x1b" not in label and OVERRIDE not in label
    assert labels[evil] == "evil?name"
    assert labels[other] == "other/evil?name"


def test_a_unique_hostile_name_is_sanitised_too(tmp_path):
    evil = tmp_path / HOSTILE
    assert display_names([evil], tmp_path) == {evil: "evil?name"}


def test_no_repos_gives_no_labels(tmp_path):
    assert display_names([], tmp_path) == {}
    assert display_names([Path("x")], Path(".")) == {Path("x"): "x"}
