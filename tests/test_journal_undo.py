import os
import shutil
import stat
from pathlib import Path

import pytest

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


# ---- undo of a moved repo with confirmation ----

async def test_undo_moved_repo_when_confirmed(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    asked = []

    def confirm(entry, current):
        asked.append((entry.repo, current))
        return True

    _, items = await undo_last(root, journal, confirm_moved=confirm)
    assert [i.status for i in items] == ["restored"]
    assert items[0].detail == f"back to {outcome.before_head[:7]} (1 newer commit dropped)"
    assert git(repo, "rev-parse", "HEAD") == outcome.before_head
    assert asked == [(str(repo), later)]


async def test_undo_moved_repo_when_declined(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    before = journal.last_undoable()
    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: False)
    assert [i.status for i in items] == ["skipped"]
    assert git(repo, "rev-parse", "HEAD") == later
    assert journal.last_undoable() == before  # still retryable


async def test_confirmed_undo_of_moved_repo_can_be_redone(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    await undo_last(root, journal)  # redo: moves back to the later commit
    assert git(repo, "rev-parse", "HEAD") == later


async def test_confirm_moved_receives_entry_and_current_head(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    seen = []

    def confirm(entry, current):
        seen.append((entry, current))
        return False

    await undo_last(root, journal, confirm_moved=confirm)
    assert len(seen) == 1
    entry, current = seen[0]
    assert isinstance(entry, JournalEntry)
    assert (entry.repo, entry.op) == (str(repo), "pull")
    assert (entry.before_head, entry.after_head) == (outcome.before_head, outcome.after_head)
    assert current == later != entry.after_head


async def test_confirm_moved_asked_once_per_moved_repo_only(make_repo, push_upstream, git, commit_local, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    commit_local(b, "later.txt")  # only b moves; a's HEAD still matches
    asked = []

    def confirm(entry, current):
        asked.append(entry.repo)
        return True

    _, items = await undo_last(root, journal, confirm_moved=confirm)
    assert asked == [str(b)]
    assert {Path(i.repo).name: i.status for i in items} == {"a": "restored", "b": "restored"}
    assert git(a, "rev-parse", "HEAD") == results[a].before_head
    assert git(b, "rev-parse", "HEAD") == results[b].before_head


async def test_confirm_moved_not_called_when_head_matches(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    asked = []

    def confirm(entry, current):
        asked.append(entry.repo)
        return True

    _, items = await undo_last(root, journal, confirm_moved=confirm)
    assert asked == []
    assert [i.status for i in items] == ["restored"]
    assert "newer commits dropped" not in items[0].detail


async def test_raising_confirm_is_treated_as_declined(make_repo, push_upstream, git, commit_local, tmp_path, caplog):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    commit_local(a, "later.txt")
    a_later = git(a, "rev-parse", "HEAD")

    def confirm(entry, current):
        raise RuntimeError("prompt exploded")

    with caplog.at_level("ERROR", logger="githerd.undo"):
        op_set, items = await undo_last(root, journal, confirm_moved=confirm)
    assert {Path(i.repo).name: i.status for i in items} == {"a": "skipped", "b": "restored"}
    assert git(a, "rev-parse", "HEAD") == a_later
    assert git(b, "rev-parse", "HEAD") == results[b].before_head
    assert any(r.name == "githerd.undo" and "prompt exploded" in (r.exc_text or "") for r in caplog.records)
    # still retryable: the moved repo is the residual op set
    residual = journal.last_undoable()
    assert residual.description == f"{op_set.description} (not yet undone)"
    assert [e.repo for e in residual.entries] == [str(a)]


async def test_raising_confirm_on_only_repo_leaves_op_set_untouched(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    before = journal.last_undoable()

    def confirm(entry, current):
        raise ValueError("boom")

    _, items = await undo_last(root, journal, confirm_moved=confirm)
    assert [i.status for i in items] == ["skipped"]
    assert journal.last_undoable() == before


def _undo_opsets(journal):
    return [ln for ln in journal._lines()
            if ln["type"] == "opset" and ln["description"].startswith("undo:")]


async def test_confirmed_reset_failure_is_failed_retryable_and_not_journaled(make_repo, push_upstream, git, commit_local, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    commit_local(b, "later.txt")
    b_later = git(b, "rev-parse", "HEAD")
    # An uncommitted edit to a file the reversal must change: `reset --keep` refuses.
    (b / "README.md").write_text("local edit\n", encoding="utf-8")

    op_set, items = await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    assert {Path(i.repo).name: i.status for i in items} == {"a": "restored", "b": "failed"}
    assert git(b, "rev-parse", "HEAD") == b_later
    residual = journal.last_undoable()
    assert residual.description == f"{op_set.description} (not yet undone)"
    assert [(e.repo, e.op) for e in residual.entries] == [(str(b), "pull")]
    # the reversal journaled for this undo covers only the repo that was actually reset
    undo_sets = _undo_opsets(journal)
    assert len(undo_sets) == 1
    assert [e["repo"] for e in undo_sets[0]["entries"]] == [str(a)]


async def test_confirmed_reset_failure_on_only_repo_keeps_op_set(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("local edit\n", encoding="utf-8")  # blocks reset --keep

    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    assert [i.status for i in items] == ["failed"]
    assert git(repo, "rev-parse", "HEAD") == later
    assert journal.last_undoable().description == "pull 1 repo"
    assert _undo_opsets(journal) == []


async def test_reversal_of_confirmed_moved_undo_records_current_head(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")
    await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    reversal = journal.last_undoable()
    assert [(e.op, e.before_head, e.after_head) for e in reversal.entries] == [
        ("undo", later, outcome.before_head)
    ]


# ---- moved repos: only the same history is offered; accurate detail ----

DIFFERENT_HISTORY = "repo is on different history than this operation; not undone"


class _PromptWasShown(BaseException):
    """BaseException so undo_last cannot swallow it as a failing callback."""


def _must_not_ask(entry, current):
    raise _PromptWasShown(entry.repo)


async def test_undo_moved_repo_two_newer_commits_detail(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later1.txt")
    commit_local(repo, "later2.txt")
    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    assert [i.status for i in items] == ["restored"]
    assert items[0].detail == f"back to {outcome.before_head[:7]} (2 newer commits dropped)"


async def test_undo_moved_repo_count_failure_falls_back_to_generic_detail(
    monkeypatch, make_repo, push_upstream, git, commit_local, tmp_path
):
    import githerd.undo as undo_mod
    from githerd.runner import GitResult

    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    real = undo_mod.run_git

    async def flaky(repo_path, *args, **kwargs):
        if args[:1] == ("rev-list",):
            return GitResult(code=128, stdout="", stderr="fatal: boom")
        return await real(repo_path, *args, **kwargs)

    monkeypatch.setattr(undo_mod, "run_git", flaky)
    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: True)
    assert [i.status for i in items] == ["restored"]
    assert items[0].detail == f"back to {outcome.before_head[:7]} (newer commits dropped)"


async def test_moved_repo_on_different_history_is_skipped_without_prompt(
    make_repo, push_upstream, git, commit_local, tmp_path
):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    # The pull was recorded on main; the user then moved to an unrelated line of work.
    git(repo, "switch", "-c", "other", outcome.before_head)
    commit_local(repo, "other.txt")
    other_head = git(repo, "rev-parse", "HEAD")
    before = journal.last_undoable()

    _, items = await undo_last(root, journal, confirm_moved=_must_not_ask)
    assert [i.status for i in items] == ["skipped"]
    assert items[0].detail == DIFFERENT_HISTORY
    assert git(repo, "rev-parse", "HEAD") == other_head
    assert journal.last_undoable() == before  # entry stays retryable


async def test_different_history_is_retryable_residual_next_to_restored_repo(
    make_repo, push_upstream, git, commit_local, tmp_path
):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    git(b, "switch", "-c", "other", results[b].before_head)
    commit_local(b, "other.txt")

    op_set, items = await undo_last(root, journal, confirm_moved=_must_not_ask)
    by = {Path(i.repo).name: i for i in items}
    assert by["a"].status == "restored"
    assert by["b"].status == "skipped"
    assert by["b"].detail == DIFFERENT_HISTORY
    residual = journal.last_undoable()
    assert residual.description == f"{op_set.description} (not yet undone)"
    assert [e.repo for e in residual.entries] == [str(b)]


async def test_unresolvable_after_head_is_treated_as_different_history(make_repo, git, tmp_path):
    repo = make_repo("a")
    head = git(repo, "rev-parse", "HEAD")
    journal = Journal(tmp_path)
    journal.record("gone", [JournalEntry(
        repo=str(repo), op="pull", before_head="2" * 40, after_head="1" * 40,
    )])
    before = journal.last_undoable()
    _, items = await undo_last(tmp_path, journal, confirm_moved=_must_not_ask)
    assert [i.status for i in items] == ["skipped"]
    assert items[0].detail == DIFFERENT_HISTORY
    assert git(repo, "rev-parse", "HEAD") == head
    assert journal.last_undoable() == before


async def test_repo_already_at_before_head_is_skipped_not_retryable(make_repo, push_upstream, git, tmp_path):
    a, b, root, journal, results = await _two_pulled(make_repo, push_upstream, tmp_path)
    git(b, "reset", "--hard", results[b].before_head)  # user already reset elsewhere

    op_set, items = await undo_last(root, journal, confirm_moved=_must_not_ask)
    by = {Path(i.repo).name: i for i in items}
    assert by["a"].status == "restored"
    assert by["b"].status == "skipped"
    assert by["b"].detail == f"already at {results[b].before_head[:7]}"
    assert git(b, "rev-parse", "HEAD") == results[b].before_head
    last = journal.last_undoable()
    assert last.description == f"undo: {op_set.description}"  # no residual for b
    assert [e.repo for e in last.entries] == [str(a)]


async def test_already_at_before_head_is_not_counted_as_restored(make_repo, push_upstream, git, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    git(repo, "reset", "--hard", outcome.before_head)
    before = journal.last_undoable()
    _, items = await undo_last(root, journal, confirm_moved=_must_not_ask)
    assert [(i.status, i.detail) for i in items] == [("skipped", f"already at {outcome.before_head[:7]}")]
    assert journal.last_undoable() is None  # only permanent skips: the op set is closed, not stuck
    assert _undo_opsets(journal) == []  # and no undo of an undo is journaled


async def test_keyboard_interrupt_from_confirm_propagates(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    later = git(repo, "rev-parse", "HEAD")

    def confirm(entry, current):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await undo_last(root, journal, confirm_moved=confirm)
    assert git(repo, "rev-parse", "HEAD") == later


async def test_truthy_non_bool_confirm_is_accepted(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "later.txt")
    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: "yes")
    assert [i.status for i in items] == ["restored"]
    assert git(repo, "rev-parse", "HEAD") == outcome.before_head


# ---- op sets made only of permanent skips must not block older ones ----------------

async def test_all_permanent_skips_close_the_op_set_so_older_ones_are_reachable(make_repo, git, tmp_path):
    repo = make_repo("a")
    head = git(repo, "rev-parse", "HEAD")
    journal = Journal(tmp_path)
    older = journal.record("older", [JournalEntry(repo=str(repo), op="pull", before_head="x", after_head=head)])
    newer = journal.record("newer", [
        JournalEntry(repo=str(tmp_path / "gone"), op="pull", before_head="a", after_head="b"),
        JournalEntry(repo=str(repo), op="mystery", before_head="a", after_head=head),
    ])
    op_set, items = await undo_last(tmp_path, journal)
    assert op_set == newer
    assert [i.status for i in items] == ["skipped", "skipped"]  # items are reported as before
    assert journal.last_undoable() == older
    assert _undo_opsets(journal) == []


async def test_a_retryable_entry_next_to_permanent_skips_keeps_the_op_set(make_repo, push_upstream, git, commit_local, tmp_path):
    repo, root, journal, outcome = await _pulled(make_repo, push_upstream, git, tmp_path)
    commit_local(repo, "mine.txt")
    gone = root / "gone"
    journal.record("pull 2 repos", [
        JournalEntry(repo=str(gone), op="pull", before_head="a", after_head="b"),
        JournalEntry(repo=str(repo), op="pull", before_head=outcome.before_head, after_head=outcome.after_head),
    ])
    before = journal.last_undoable()
    _, items = await undo_last(root, journal, confirm_moved=lambda entry, current: False)
    assert {i.status for i in items} == {"skipped"}
    assert journal.last_undoable() == before  # the declined moved repo is still retryable


async def test_closing_a_permanent_skip_op_set_survives_a_journal_write_error(monkeypatch, make_repo, git, tmp_path):
    repo = make_repo("a")
    journal = Journal(tmp_path)
    journal.record("weird", [JournalEntry(repo=str(repo), op="mystery", before_head="x", after_head="y")])

    def boom(self, op_set_id):
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "mark_undone", boom)
    _, items = await undo_last(tmp_path, journal)
    assert [i.status for i in items] == ["skipped"]
