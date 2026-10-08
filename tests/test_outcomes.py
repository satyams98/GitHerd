from pydantic import TypeAdapter

from githerd.outcomes import (
    AuthRequired, BlockedDirty, Conflict, Diverged, Failed, FileChange,
    NetworkError, Ok, Outcome, UpToDate, describe, summarize,
)


def test_outcome_roundtrip_uses_kind_discriminator():
    adapter = TypeAdapter(Outcome)
    original = BlockedDirty(files=[FileChange(status=" M", path="a.txt")])
    restored = adapter.validate_python(adapter.dump_python(original))
    assert restored == original
    assert restored.kind == "blocked_dirty"


def test_summarize_counts_in_fixed_order():
    outcomes = [
        Ok(commits=3, files=12, before_head="a", after_head="b"),
        UpToDate(),
        UpToDate(),
        BlockedDirty(files=[]),
    ]
    assert summarize(outcomes) == "1 updated, 2 up to date, 1 blocked by local changes"


def test_describe_each_outcome():
    assert describe(Ok(commits=3, files=12, before_head="a", after_head="b")) == "updated: 3 commits, 12 files"
    assert describe(Ok(commits=1, files=0, before_head="a", after_head="a")) == "updated: 1 commit"
    assert describe(UpToDate()) == "already up to date"
    assert describe(BlockedDirty(files=[FileChange(status="??", path="x")])) == "blocked: 1 local change"
    assert describe(Diverged(ahead=1, behind=2)) == "diverged: 1 ahead, 2 behind"
    assert describe(Conflict(files=["a", "b"])) == "conflict in 2 files"
    assert describe(AuthRequired(remote="origin")) == "needs credentials"
    assert describe(NetworkError(message="timed out")) == "network error: timed out"
    assert describe(Failed(message="boom")) == "failed: boom"


# ---- H4 items 3 and 6: non-retryable failures and the kept-stash flag -------------------

def test_failed_is_retryable_by_default_and_old_data_still_loads():
    adapter = TypeAdapter(Outcome)
    assert Failed(message="x").retryable is True
    old = adapter.validate_python({"kind": "failed", "message": "x"})  # written before the field existed
    assert old == Failed(message="x") and old.retryable is True
    fixed = Failed(message="x", retryable=False)
    restored = adapter.validate_python(adapter.dump_python(fixed))
    assert restored == fixed and restored.retryable is False


def test_conflict_stash_kept_defaults_to_false_and_round_trips():
    adapter = TypeAdapter(Outcome)
    assert Conflict(files=["a"]).stash_kept is False
    old = adapter.validate_python({"kind": "conflict", "files": ["a"]})
    assert old == Conflict(files=["a"])
    kept = Conflict(files=["a"], stash_kept=True)
    assert adapter.validate_python(adapter.dump_python(kept)) == kept
