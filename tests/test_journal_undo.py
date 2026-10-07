import os
import shutil
import stat
from pathlib import Path

from githerd.bulk import pull_repos
from githerd.gitops import pull
from githerd.journal import Journal, JournalEntry
from githerd.outcomes import Ok
from githerd.undo import undo_last


def _entry(repo="r", op="pull", before="a", after="b"):
    return JournalEntry(repo=repo, op=op, before_head=before, after_head=after)


def test_record_with_no_entries_returns_none(tmp_path):
    assert Journal(tmp_path).record("nothing", []) is None
    assert Journal(tmp_path).last_undoable() is None


def test_record_and_last_undoable_roundtrip(tmp_path):
    journal = Journal(tmp_path)
    first = journal.record("first", [_entry()])
    second = journal.record("second", [_entry(repo="r2")])
    assert journal.last_undoable() == second
    journal.mark_undone(second.id)
    assert journal.last_undoable() == first


def test_journal_directory_ignores_itself(tmp_path):
    Journal(tmp_path).record("x", [_entry()])
    assert (tmp_path / ".githerd" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


async def _pulled(make_repo, push_upstream, git, tmp_path):
    repo = make_repo("a")
    push_upstream(repo, "new.txt")
    outcome = await pull(repo)
    assert isinstance(outcome, Ok)
    root = tmp_path / "work"
    journal = Journal(root)
    journal.record("pull 1 repo", [JournalEntry(
        repo=str(repo), op="pull",
        before_head=outcome.before_head, after_head=outcome.after_head,
    )])
    return repo, root, journal, outcome


async def test_undo_restores_previous_head(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    op_set, items = await undo_last(root, journal)
    assert op_set is not None
    assert [i.status for i in items] == ["restored"]
    assert git(repo, "rev-parse", "HEAD") == outcome.before_head
    assert not (repo / "new.txt").exists()


async def test_undo_can_itself_be_undone(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    await undo_last(root, journal)
    await undo_last(root, journal)  # undo the undo
    assert git(repo, "rev-parse", "HEAD") == outcome.after_head
    op_set, items = await undo_last(root, journal)
    assert op_set is None and items == []


async def test_undo_nothing_to_undo(tmp_path):
    op_set, items = await undo_last(tmp_path, Journal(tmp_path))
    assert op_set is None and items == []


async def test_undo_skips_repo_that_moved_since(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    head_after_later = git(repo, "rev-parse", "HEAD")
    op_set, items = await undo_last(root, journal)
    assert [i.status for i in items] == ["skipped"]
    assert "moved" in items[0].detail
    assert git(repo, "rev-parse", "HEAD") == head_after_later
    assert journal.last_undoable() == op_set  # not consumed


async def test_undo_unknown_op_is_skipped(tmp_path, make_repo, git):
    repo = make_repo("a")
    head = git(repo, "rev-parse", "HEAD")
    journal = Journal(tmp_path)
    journal.record("weird", [JournalEntry(repo=str(repo), op="mystery", before_head="x", after_head=head)])
    _, items = await undo_last(tmp_path, journal)
    assert [i.status for i in items] == ["skipped"]
    assert "no undo" in items[0].detail


# ---- journal robustness ----

def test_journal_skips_malformed_lines(tmp_path):
    journal = Journal(tmp_path)
    first = journal.record("first", [_entry()])
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "opset", "id": "dead", "timest')  # truncated write
    assert journal.last_undoable() == first


def test_journal_creates_nested_missing_directories(tmp_path):
    journal = Journal(tmp_path / "does" / "not" / "exist")
    op_set = journal.record("x", [_entry()])
    assert journal.last_undoable() == op_set


# ---- partial undo ----

def _rmtree(path):
    def onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=onexc)


async def _two_pulled(make_repo, push_upstream, tmp_path):
    a, b = make_repo("a"), make_repo("b")
    push_upstream(a, "new.txt")
    push_upstream(b, "README.md", "changed upstream\n")
    root = tmp_path / "work"
    results = await pull_repos(root, [a, b])
    assert all(isinstance(o, Ok) for o in results.values())
    return a, b, root, Journal(root), results


async def test_partial_undo_leaves_residual_for_failed_repo(make_repo, push_upstream, git, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    a_before, b_before = results[a].before_head, results[b].before_head
    (b / "README.md").write_text("local edit\n", encoding="utf-8")

    op_set, items = await undo_last(root, journal)
    by_name = {Path(i.repo).name: i.status for i in items}
    assert by_name == {"a": "restored", "b": "failed"}
    assert git(a, "rev-parse", "HEAD") == a_before
    residual = journal.last_undoable()
    assert residual is not None
    assert residual.description == f"{op_set.description} (not yet undone)"
    assert [(e.repo, e.op, e.before_head, e.after_head) for e in residual.entries] == [
        (str(b), "pull", b_before, results[b].after_head)
    ]

    git(b, "checkout", "--", "README.md")  # clear the blocking edit
    op2, items2 = await undo_last(root, journal)
    assert op2.id == residual.id
    assert [i.status for i in items2] == ["restored"]
    assert git(b, "rev-parse", "HEAD") == b_before
    assert git(a, "rev-parse", "HEAD") == a_before  # a not re-applied


async def test_moved_repo_becomes_residual_and_is_retried(make_repo, push_upstream, git, commit_local, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    commit_local(b, "later.txt")

    op_set, items = await undo_last(root, journal)
    assert {Path(i.repo).name: i.status for i in items} == {"a": "restored", "b": "skipped"}
    residual = journal.last_undoable()
    assert residual.description.endswith("(not yet undone)")
    assert [e.repo for e in residual.entries] == [str(b)]

    git(b, "reset", "--hard", results[b].after_head)  # repo returns to after_head
    _, items2 = await undo_last(root, journal)
    assert [i.status for i in items2] == ["restored"]
    assert git(b, "rev-parse", "HEAD") == results[b].before_head


async def test_missing_repo_is_permanent_skip_not_retried(make_repo, push_upstream, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    _rmtree(b)

    op_set, items = await undo_last(root, journal)
    by = {Path(i.repo).name: i for i in items}
    assert by["a"].status == "restored"
    assert by["b"].status == "skipped"
    assert by["b"].detail == "repo not found or not a git repository"
    last = journal.last_undoable()
    assert last.description == f"undo: {op_set.description}"  # no residual recorded


async def test_nothing_restored_leaves_op_set_unchanged(make_repo, push_upstream, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    (a / "new.txt").write_text("local edit\n", encoding="utf-8")
    (b / "README.md").write_text("local edit\n", encoding="utf-8")

    op_set, items = await undo_last(root, journal)
    assert {i.status for i in items} == {"failed"}
    assert journal.last_undoable() == op_set


async def test_undo_survives_journal_write_error(monkeypatch, make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "mark_undone", boom)
    monkeypatch.setattr(Journal, "record", boom)
    op_set, items = await undo_last(root, journal)
    assert [i.status for i in items] == ["restored"]
    assert git(repo, "rev-parse", "HEAD") == outcome.before_head
