import asyncio
from pathlib import Path

import pytest

from githerd import bulk
from githerd.bulk import RepoEvent, pull_repos, run_bulk
from githerd.journal import Journal
from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, NetworkError, Ok, UpToDate,
)


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


async def test_run_bulk_times_out_a_slow_operation(tmp_path):
    slow, fast = tmp_path / "slow", tmp_path / "fast"

    async def op(repo, progress):
        if repo == slow:
            await asyncio.sleep(5)
        return UpToDate()

    results = await run_bulk([slow, fast], op, timeout=0.05)
    assert results[fast] == UpToDate()
    assert isinstance(results[slow], Failed)
    assert "timed out after 0.05s" in results[slow].message


async def test_run_bulk_timeout_actually_cancels_the_operation(tmp_path):
    repo = tmp_path / "r"
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def op(repo, progress):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        finally:
            cancelled.append(False)
        return UpToDate()

    results = await asyncio.wait_for(run_bulk([repo], op, timeout=0.05), timeout=5)
    assert started.is_set()
    assert cancelled == [True, False]  # CancelledError was delivered, then cleanup ran
    assert isinstance(results[repo], Failed)


@pytest.mark.parametrize("timeout", [None, 0])
async def test_run_bulk_none_or_zero_timeout_means_no_timeout(tmp_path, timeout):
    repo = tmp_path / "r"

    async def op(repo, progress):
        await asyncio.sleep(0.1)  # would exceed any tiny positive timeout
        return UpToDate()

    results = await run_bulk([repo], op, timeout=timeout)
    assert results[repo] == UpToDate()


async def test_run_bulk_timeout_does_not_swallow_outer_cancellation(tmp_path):
    started = asyncio.Event()

    async def op(repo, progress):
        started.set()
        await asyncio.sleep(30)
        return UpToDate()

    task = asyncio.create_task(run_bulk([tmp_path / "r"], op, timeout=10))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


def test_pull_entries_only_includes_ok_with_a_moved_head(tmp_path):
    from githerd.bulk import pull_entries

    moved = Ok(commits=1, files=1, before_head="a", after_head="b")
    fetched = Ok(commits=1, files=0, before_head="a", after_head="a")
    entries = pull_entries({
        tmp_path / "m": moved, tmp_path / "f": fetched,
        tmp_path / "u": UpToDate(), tmp_path / "x": Failed(message="x"),
    })
    assert [(e.repo, e.op, e.before_head, e.after_head) for e in entries] == [
        (str(tmp_path / "m"), "pull", "a", "b")
    ]


def test_record_pulls_never_raises(tmp_path, monkeypatch):
    from githerd import bulk

    def boom(self, description, entries):
        raise OSError("disk full")

    monkeypatch.setattr(bulk.Journal, "record", boom)
    moved = Ok(commits=1, files=1, before_head="a", after_head="b")
    bulk.record_pulls(tmp_path, "pull 1 repos", {tmp_path / "m": moved})  # must not raise


def test_record_pulls_writes_a_journal_that_round_trips(tmp_path):
    moved = Ok(commits=1, files=1, before_head="a" * 40, after_head="b" * 40)
    bulk.record_pulls(tmp_path, "pull 2 repos", {
        tmp_path / "m": moved, tmp_path / "u": UpToDate(),
    })
    op_set = Journal(tmp_path).last_undoable()
    assert op_set is not None
    assert op_set.description == "pull 2 repos"
    assert [(e.repo, e.op, e.before_head, e.after_head) for e in op_set.entries] == [
        (str(tmp_path / "m"), "pull", "a" * 40, "b" * 40)
    ]


async def test_pull_repos_forwards_timeout_to_run_bulk(monkeypatch, tmp_path):
    seen: dict = {}

    async def fake_run_bulk(repos, op, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(bulk, "run_bulk", fake_run_bulk)
    await pull_repos(tmp_path, [], timeout=12.5)
    assert seen["timeout"] == 12.5


@pytest.mark.parametrize("timeout", [None, 0])
async def test_run_bulk_inner_timeout_error_without_timeout_is_a_plain_failure(tmp_path, timeout):
    bad, good = tmp_path / "bad", tmp_path / "good"

    async def op(repo, progress):
        if repo == bad:
            raise TimeoutError("inner")
        return UpToDate()

    results = await run_bulk([bad, good], op, timeout=timeout)
    assert results[bad] == Failed(message="inner")
    assert results[good] == UpToDate()


async def test_run_bulk_inner_timeout_error_message_never_empty(tmp_path):
    async def op(repo, progress):
        raise TimeoutError()

    results = await run_bulk([tmp_path / "r"], op)
    assert results[tmp_path / "r"] == Failed(message="TimeoutError")


async def test_run_bulk_inner_timeout_error_with_real_timeout_does_not_crash(tmp_path):
    bad, good = tmp_path / "bad", tmp_path / "good"

    async def op(repo, progress):
        if repo == bad:
            raise TimeoutError("inner")
        return UpToDate()

    results = await run_bulk([bad, good], op, timeout=30)
    assert results[good] == UpToDate()
    assert isinstance(results[bad], Failed)
    assert "inner" in results[bad].message  # not mislabelled as the bulk timeout


@pytest.mark.parametrize("timeout", [-1, -0.5, float("nan"), float("inf"), float("-inf")])
async def test_run_bulk_rejects_invalid_timeout(tmp_path, timeout):
    started: list[Path] = []

    async def op(repo, progress):
        started.append(repo)
        return UpToDate()

    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        await run_bulk([tmp_path / "r"], op, timeout=timeout)
    assert started == []


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
async def test_pull_repos_rejects_invalid_timeout_before_any_work(monkeypatch, tmp_path, timeout):
    async def fake_pull(repo, progress):
        raise AssertionError("no work may start")

    monkeypatch.setattr(bulk, "pull", fake_pull)
    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        await pull_repos(tmp_path / "work", [tmp_path / "r"], timeout=timeout)
    assert not (tmp_path / "work").exists()  # nothing journalled either


def test_pull_entries_copy_the_branch(tmp_path):
    from githerd.bulk import pull_entries

    moved = Ok(commits=1, files=1, before_head="a", after_head="b", branch="feature/x")
    plain = Ok(commits=1, files=1, before_head="a", after_head="b")
    entries = pull_entries({tmp_path / "m": moved, tmp_path / "p": plain})
    assert [e.branch for e in entries] == ["feature/x", None]


# ---- H2 item 2: journal by HEAD comparison -------------------------------------------

def _git_init_unborn(path):
    import subprocess

    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    return path


def _moves_then_sleeps(moved: asyncio.Event | None = None):
    """A pull op that really fast-forwards the repo, then never finishes in time."""
    from githerd.runner import run_git

    async def op(repo, progress):
        res = await run_git(repo, "pull", "--ff-only")
        assert res.ok, res.stderr
        if moved is not None:
            moved.set()
        await asyncio.sleep(60)
        return UpToDate()

    return op


async def test_pull_that_moves_head_then_times_out_is_still_journaled(
    monkeypatch, make_repo, push_upstream, git, tmp_path
):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "new.txt")
    before = git(a, "rev-parse", "HEAD")
    monkeypatch.setattr(bulk, "pull", _moves_then_sleeps())
    results = await pull_repos(root, [a], timeout=3)
    assert isinstance(results[a], Failed) and "timed out" in results[a].message
    after = git(a, "rev-parse", "HEAD")
    assert after != before
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    (entry,) = op_set.entries
    assert (entry.repo, entry.op) == (str(a), "pull")
    assert (entry.before_head, entry.after_head, entry.branch) == (before, after, "main")


async def test_pull_that_moves_head_then_is_cancelled_is_still_journaled(
    monkeypatch, make_repo, push_upstream, git, tmp_path
):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "new.txt")
    before = git(a, "rev-parse", "HEAD")
    moved = asyncio.Event()
    monkeypatch.setattr(bulk, "pull", _moves_then_sleeps(moved))
    task = asyncio.create_task(pull_repos(root, [a]))
    await asyncio.wait_for(moved.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    op_set = Journal(root).last_undoable()
    assert op_set is not None
    (entry,) = op_set.entries
    assert (entry.before_head, entry.after_head, entry.branch) == (
        before, git(a, "rev-parse", "HEAD"), "main")


async def test_failed_repo_whose_head_did_not_move_gets_no_entry(monkeypatch, make_repo, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")

    async def fails(repo, progress):
        raise RuntimeError("kaput")

    monkeypatch.setattr(bulk, "pull", fails)
    results = await pull_repos(root, [a])
    assert isinstance(results[a], Failed)
    assert Journal(root).last_undoable() is None


async def test_unborn_repo_is_skipped_without_error(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "a.txt")
    unborn = _git_init_unborn(tmp_path / "unborn")
    real_pull = bulk.pull

    async def fake(repo, progress):
        if repo == unborn:
            return Failed(message="no commits")
        return await real_pull(repo, progress)

    monkeypatch.setattr(bulk, "pull", fake)
    results = await pull_repos(root, [a, unborn])
    assert isinstance(results[a], Ok) and isinstance(results[unborn], Failed)
    op_set = Journal(root).last_undoable()
    assert [e.repo for e in op_set.entries] == [str(a)]


async def test_a_failing_before_read_does_not_break_the_run(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "a.txt")

    async def boom(repo):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(bulk, "head_and_branch", boom)
    results = await pull_repos(root, [a])
    assert isinstance(results[a], Ok)
    op_set = Journal(root).last_undoable()  # the Ok outcome still carries its own heads
    assert [e.repo for e in op_set.entries] == [str(a)]


async def test_a_failing_after_read_does_not_break_the_run(monkeypatch, make_repo, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")

    async def fails(repo, progress):
        raise RuntimeError("kaput")

    def boom(repo):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(bulk, "pull", fails)
    monkeypatch.setattr(bulk, "head_sync", boom)
    results = await pull_repos(root, [a])
    assert isinstance(results[a], Failed)
    assert Journal(root).last_undoable() is None


async def test_before_read_is_one_call_per_repo_with_bounded_concurrency(monkeypatch, tmp_path):
    repos = [tmp_path / f"r{i}" for i in range(6)]
    active = peak = 0
    seen = []

    async def fake_read(repo):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        seen.append(repo)
        return ("a" * 40, "main")

    async def op(repo, progress):
        return UpToDate()

    monkeypatch.setattr(bulk, "head_and_branch", fake_read)
    monkeypatch.setattr(bulk, "pull", op)
    monkeypatch.setattr(bulk, "head_sync", lambda repo: "a" * 40)
    await pull_repos(tmp_path / "work", repos, concurrency=2)
    assert sorted(seen) == sorted(repos)
    assert peak == 2


async def test_ok_outcomes_are_journaled_without_re_reading_head(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a, c = make_repo("a"), make_repo("c")
    push_upstream(a, "a.txt")
    reread = []
    real = bulk.head_sync

    def spy(repo):
        reread.append(repo)
        return real(repo)

    monkeypatch.setattr(bulk, "head_sync", spy)
    results = await pull_repos(root, [a, c])
    assert isinstance(results[a], Ok) and results[c] == UpToDate()
    assert reread == []  # neither an Ok nor an UpToDate outcome can need a re-read
    (entry,) = Journal(root).last_undoable().entries
    assert (entry.repo, entry.branch) == (str(a), "main")


def test_record_pulls_adds_head_moves_the_outcomes_do_not_carry(tmp_path):
    moved = Ok(commits=1, files=1, before_head="a" * 40, after_head="b" * 40, branch="main")
    bulk.record_pulls(tmp_path, "pull 2 repos", {tmp_path / "m": moved},
                      moves={tmp_path / "x": ("c" * 40, "d" * 40, "dev"),
                             tmp_path / "same": ("e" * 40, "e" * 40, "dev"),
                             tmp_path / "m": ("1" * 40, "2" * 40, "other")})
    op_set = Journal(tmp_path).last_undoable()
    assert sorted((e.repo, e.before_head, e.after_head, e.branch) for e in op_set.entries) == sorted([
        (str(tmp_path / "m"), "a" * 40, "b" * 40, "main"),   # the outcome wins
        (str(tmp_path / "x"), "c" * 40, "d" * 40, "dev"),
    ])


# ---- H2 fix: lazy, bounded HEAD capture -------------------------------------------------

async def test_the_first_start_fires_before_slow_baseline_reads_of_later_repos_finish(monkeypatch, tmp_path):
    repos = [tmp_path / f"r{i}" for i in range(3)]
    events = []

    async def fake_read(repo):
        if repo != repos[0]:
            await asyncio.sleep(0.3)  # slow reads of the later repos
        events.append(("read", repo))
        return ("a" * 40, "main")

    async def op(repo, progress):
        return UpToDate()

    def on_event(event):
        if event.kind == "start":
            events.append(("start", Path(event.repo)))

    monkeypatch.setattr(bulk, "head_and_branch", fake_read)
    monkeypatch.setattr(bulk, "pull", op)
    monkeypatch.setattr(bulk, "head_sync", lambda repo: "a" * 40)
    await pull_repos(tmp_path / "work", repos, on_event=on_event)
    assert events[0] == ("start", repos[0])  # no pre-run pass: the dashboard starts at once
    slow_done = [i for i, e in enumerate(events) if e[0] == "read" and e[1] != repos[0]]
    first_slow_start = min(i for i, e in enumerate(events) if e[0] == "start" and e[1] != repos[0])
    assert first_slow_start < min(slow_done)


async def test_a_hung_baseline_read_is_covered_by_the_per_repo_timeout(monkeypatch, tmp_path):
    a = tmp_path / "a"
    pulled = []

    async def hangs(repo):
        await asyncio.sleep(60)

    async def op(repo, progress):
        pulled.append(repo)
        return UpToDate()

    monkeypatch.setattr(bulk, "head_and_branch", hangs)
    monkeypatch.setattr(bulk, "pull", op)
    results = await pull_repos(tmp_path / "work", [a], timeout=0.2)
    assert isinstance(results[a], Failed) and "timed out" in results[a].message
    assert pulled == []  # the pull never began, so nothing was journaled
    assert Journal(tmp_path / "work").last_undoable() is None


async def test_a_cancelled_baseline_read_leaves_the_repo_uncompared(monkeypatch, make_repo, push_upstream, tmp_path):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "new.txt")  # HEAD would differ from any bogus baseline if it were compared
    started = asyncio.Event()

    async def hangs(repo):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(bulk, "head_and_branch", hangs)
    task = asyncio.create_task(pull_repos(root, [a]))
    await asyncio.wait_for(started.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert Journal(root).last_undoable() is None


@pytest.mark.parametrize("outcome, rereads", [
    (UpToDate(), False),
    (BlockedDirty(files=[]), False),
    (Diverged(ahead=1, behind=1), False),
    (Failed(message="x"), True),
    (NetworkError(message="x"), True),
    (AuthRequired(remote="x"), True),
    (Conflict(files=[]), True),
])
async def test_only_outcomes_that_could_have_moved_head_are_re_read(monkeypatch, tmp_path, outcome, rereads):
    a = tmp_path / "a"
    reread = []

    async def fake_read(repo):
        return ("a" * 40, "main")

    async def op(repo, progress):
        return outcome

    def spy(repo):
        reread.append(repo)
        return "a" * 40

    monkeypatch.setattr(bulk, "head_and_branch", fake_read)
    monkeypatch.setattr(bulk, "pull", op)
    monkeypatch.setattr(bulk, "head_sync", spy)
    await pull_repos(tmp_path / "work", [a])
    assert reread == ([a] if rereads else [])


async def test_a_repo_without_an_outcome_is_re_read(monkeypatch, tmp_path):
    a = tmp_path / "a"
    started = asyncio.Event()
    reread = []

    async def fake_read(repo):
        return ("a" * 40, "main")

    async def op(repo, progress):
        started.set()
        await asyncio.sleep(60)

    def spy(repo):
        reread.append(repo)
        return "b" * 40

    monkeypatch.setattr(bulk, "head_and_branch", fake_read)
    monkeypatch.setattr(bulk, "pull", op)
    monkeypatch.setattr(bulk, "head_sync", spy)
    root = tmp_path / "work"
    task = asyncio.create_task(pull_repos(root, [a]))
    await asyncio.wait_for(started.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reread == [a]
    (entry,) = Journal(root).last_undoable().entries
    assert (entry.before_head, entry.after_head, entry.branch) == ("a" * 40, "b" * 40, "main")


async def test_the_after_read_loop_has_an_overall_deadline(monkeypatch, tmp_path, caplog):
    import logging
    import time

    repos = [tmp_path / f"r{i}" for i in range(4)]

    async def fake_read(repo):
        return ("a" * 40, "main")

    async def fails(repo, progress):
        return Failed(message="x")

    def slow(repo):
        time.sleep(0.6)
        return "b" * 40

    monkeypatch.setattr(bulk, "head_and_branch", fake_read)
    monkeypatch.setattr(bulk, "pull", fails)
    monkeypatch.setattr(bulk, "head_sync", slow)
    monkeypatch.setattr(bulk, "AFTER_READ_DEADLINE", 0.1)
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="githerd.bulk"):
        results = await pull_repos(tmp_path / "work", repos, concurrency=1)
    assert time.monotonic() - start < 0.5  # gave up at the deadline, not after 4 slow reads
    assert all(isinstance(o, Failed) for o in results.values())
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "4 repos" in messages and "not checked" in messages


def test_the_after_read_deadline_default_is_thirty_seconds():
    assert bulk.AFTER_READ_DEADLINE == 30.0


# ---- H7 item F: a repo listed twice is pulled once ---------------------------------------------

@pytest.mark.parametrize("concurrency", [1, 2])
async def test_a_repo_listed_twice_is_pulled_and_journaled_once(
    monkeypatch, make_repo, push_upstream, tmp_path, concurrency
):
    root = tmp_path / "work"
    a = make_repo("a")
    push_upstream(a, "a.txt")
    calls = []
    real = bulk.pull

    async def spy(repo, progress=None):
        calls.append(repo)
        await asyncio.sleep(0.05)  # long enough for a concurrent twin to start and collide
        return await real(repo, progress)

    monkeypatch.setattr(bulk, "pull", spy)
    results = await pull_repos(root, [a, a], concurrency=concurrency)
    assert calls == [a]
    assert list(results) == [a] and isinstance(results[a], Ok)
    op_set = Journal(root).last_undoable()
    assert op_set.description == "pull 1 repos"
    assert [e.repo for e in op_set.entries] == [str(a)]


async def test_repos_that_resolve_to_the_same_path_are_one_repo_and_order_is_kept(
    monkeypatch, make_repo, tmp_path
):
    a, b = make_repo("a"), make_repo("b")
    alias = a.parent / "b" / ".." / "a"  # the same directory spelled another way
    calls = []

    async def fake(repo, progress=None):
        calls.append(repo)
        return UpToDate()

    monkeypatch.setattr(bulk, "pull", fake)
    results = await pull_repos(tmp_path / "work", [b, a, alias, b], concurrency=1)
    assert calls == [b, a]  # first-seen order, one pull each
    assert list(results) == [b, a]
