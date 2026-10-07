import asyncio

import pytest

from githerd import bulk
from githerd.bulk import RepoEvent, pull_repos, run_bulk
from githerd.journal import Journal
from githerd.outcomes import Failed, Ok, UpToDate


async def test_run_bulk_limits_concurrency_and_keeps_order(tmp_path):
    repos = [tmp_path / f"r{i}" for i in range(6)]
    active = 0
    peak = 0

    async def op(repo, progress):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return UpToDate()

    results = await run_bulk(repos, op, concurrency=2)
    assert peak == 2
    assert list(results) == repos


async def test_run_bulk_turns_exceptions_into_failed(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"

    async def op(repo, progress):
        if repo == bad:
            raise RuntimeError("kaput")
        return UpToDate()

    results = await run_bulk([good, bad], op)
    assert results[good] == UpToDate()
    assert results[bad] == Failed(message="kaput")


async def test_run_bulk_emits_start_progress_done(tmp_path):
    repo = tmp_path / "r"
    events: list[RepoEvent] = []

    async def op(repo, progress):
        progress("Receiving objects:  50% (1/2)")
        return UpToDate()

    await run_bulk([repo], op, on_event=events.append)
    assert [e.kind for e in events] == ["start", "progress", "done"]
    assert events[1].percent == 50
    assert events[1].text == "Receiving objects:  50% (1/2)"
    assert events[2].outcome == UpToDate()
    assert events[0].repo == str(repo)


async def test_pull_repos_pulls_in_parallel_and_journals(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a, b, c = make_repo("a"), make_repo("b"), make_repo("c")
    push_upstream(a, "a.txt")
    push_upstream(b, "b.txt")
    events: list[RepoEvent] = []
    results = await pull_repos(root, [a, b, c], on_event=events.append)
    assert isinstance(results[a], Ok) and isinstance(results[b], Ok)
    assert results[c] == UpToDate()
    assert sum(1 for e in events if e.kind == "done") == 3
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    assert op_set.description == "pull 3 repos"
    assert sorted(e.repo for e in op_set.entries) == sorted([str(a), str(b)])


async def test_pull_repos_without_changes_writes_no_journal(make_repo, tmp_path):
    root = tmp_path / "work"
    repo = make_repo("a")
    await pull_repos(root, [repo])
    assert Journal(root).last_undoable() is None


def _raising_cb(event):
    raise RuntimeError("callback boom")


async def test_run_bulk_survives_raising_callback(tmp_path):
    good, other = tmp_path / "good", tmp_path / "other"

    async def op(repo, progress):
        progress("Receiving objects:  50% (1/2)")
        if repo == good:
            return UpToDate()
        return Ok(commits=1, files=1, before_head="a" * 40, after_head="b" * 40)

    results = await run_bulk([good, other], op, on_event=_raising_cb)
    assert results[good] == UpToDate()
    assert isinstance(results[other], Ok)
    assert not any(isinstance(o, Failed) for o in results.values())


async def test_pull_repos_journals_despite_raising_callback(make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a, c = make_repo("a"), make_repo("c")
    push_upstream(a, "a.txt")
    results = await pull_repos(root, [a, c], on_event=_raising_cb)
    assert isinstance(results[a], Ok)
    assert results[c] == UpToDate()
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    assert [e.repo for e in op_set.entries] == [str(a)]


async def test_pull_repos_journals_completed_repos_on_cancel(monkeypatch, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"
    a_done = asyncio.Event()

    async def fake_pull(repo, progress):
        if repo == a:
            return Ok(commits=1, files=1, before_head="a" * 40, after_head="b" * 40)
        await asyncio.Event().wait()

    def on_event(event):
        if event.kind == "done" and event.repo == str(a):
            a_done.set()

    monkeypatch.setattr(bulk, "pull", fake_pull)
    task = asyncio.create_task(pull_repos(root, [a, b], on_event=on_event))
    await asyncio.wait_for(a_done.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    assert [e.repo for e in op_set.entries] == [str(a)]


async def test_pull_repos_survives_journal_write_error(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "a.txt")

    def boom(self, description, entries):
        raise OSError("disk full")

    monkeypatch.setattr(bulk.Journal, "record", boom)
    results = await pull_repos(root, [a])
    assert isinstance(results[a], Ok)


async def test_run_bulk_rejects_non_positive_concurrency(tmp_path):
    async def op(repo, progress):
        return UpToDate()

    with pytest.raises(ValueError):
        await asyncio.wait_for(run_bulk([tmp_path / "r"], op, concurrency=0), timeout=5)


async def test_run_bulk_failed_message_never_empty(tmp_path):
    async def op(repo, progress):
        raise RuntimeError()

    results = await run_bulk([tmp_path / "r"], op)
    assert results[tmp_path / "r"] == Failed(message="RuntimeError")


async def test_pull_repos_survives_any_journal_exception(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "a.txt")

    def boom(self, description, entries):
        raise ValueError("bad journal")

    monkeypatch.setattr(bulk.Journal, "record", boom)
    results = await pull_repos(root, [a])
    assert isinstance(results[a], Ok)
