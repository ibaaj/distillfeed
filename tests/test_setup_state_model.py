from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

from rss_reader.setup_service import (
    CommitResult,
    SetupCommitError,
    SetupRecoveryRequired,
    SetupSession,
    normalize_setup_payload,
    preset_payload,
)
from rss_reader.setup_state import (
    COMMIT_TRANSITIONS,
    SETUP_TRANSITIONS,
    CommitEvent,
    CommitPhase,
    SetupEvent,
    SetupPhase,
    TransitionError,
    commit_transition,
    setup_transition,
)


SETUP_TERMINALS = {
    SetupPhase.COMPLETE,
    SetupPhase.RECOVERY_REQUIRED,
    SetupPhase.CANCELLED,
    SetupPhase.TIMED_OUT,
}
COMMIT_TERMINALS = {
    CommitPhase.COMPLETE,
    CommitPhase.ROLLED_BACK,
    CommitPhase.RECOVERY_REQUIRED,
}


def _reachable(initial, transitions):
    reached = {initial}
    while True:
        expanded = reached | {
            target for (source, _event), target in transitions.items() if source in reached
        }
        if expanded == reached:
            return reached
        reached = expanded


def _can_reach_terminal(initial, transitions, terminals):
    frontier = {initial}
    visited = set()
    while frontier:
        phase = frontier.pop()
        if phase in terminals:
            return True
        if phase in visited:
            continue
        visited.add(phase)
        frontier.update(
            target for (source, _event), target in transitions.items() if source == phase
        )
    return False


def test_setup_reducer_is_an_exact_closed_finite_state_machine():
    """Every declared edge works and every undeclared edge is rejected."""
    assert _reachable(SetupPhase.LISTENING, SETUP_TRANSITIONS) == set(SetupPhase)
    assert len(SETUP_TRANSITIONS) == len(set(SETUP_TRANSITIONS))

    for phase in SetupPhase:
        for event in SetupEvent:
            declared = SETUP_TRANSITIONS.get((phase, event))
            if declared is None:
                with pytest.raises(TransitionError, match=event.value):
                    setup_transition(phase, event)
            else:
                assert setup_transition(phase, event) is declared


def test_setup_has_no_nonterminal_dead_end_and_terminals_are_absorbing():
    for phase in SetupPhase:
        assert _can_reach_terminal(phase, SETUP_TRANSITIONS, SETUP_TERMINALS), phase

    assert {
        event for (phase, event), _target in SETUP_TRANSITIONS.items()
        if phase == SetupPhase.COMPLETE
    } == {SetupEvent.REPLAY}
    assert setup_transition(SetupPhase.COMPLETE, SetupEvent.REPLAY) is SetupPhase.COMPLETE
    for phase in {
        SetupPhase.RECOVERY_REQUIRED,
        SetupPhase.CANCELLED,
        SetupPhase.TIMED_OUT,
    }:
        assert all(source != phase for source, _event in SETUP_TRANSITIONS)


def test_setup_happy_retry_edit_cancel_and_timeout_paths_are_explicit():
    def follow(events):
        phase = SetupPhase.LISTENING
        visited = [phase]
        for event in events:
            phase = setup_transition(phase, event)
            visited.append(phase)
        return visited

    assert follow([
        SetupEvent.BOOTSTRAP,
        SetupEvent.VALIDATE,
        SetupEvent.APPLY,
        SetupEvent.SUCCEED,
    ]) == [
        SetupPhase.LISTENING,
        SetupPhase.EDITING,
        SetupPhase.REVIEWED,
        SetupPhase.COMMITTING,
        SetupPhase.COMPLETE,
    ]
    assert follow([
        SetupEvent.BOOTSTRAP,
        SetupEvent.INVALID,
        SetupEvent.VALIDATE,
        SetupEvent.EDIT,
        SetupEvent.VALIDATE,
        SetupEvent.APPLY,
        SetupEvent.FAIL,
        SetupEvent.RETRY,
        SetupEvent.VALIDATE,
        SetupEvent.CANCEL,
    ])[-1] is SetupPhase.CANCELLED
    for prefix in (
        [],
        [SetupEvent.BOOTSTRAP],
        [SetupEvent.BOOTSTRAP, SetupEvent.VALIDATE],
        [
            SetupEvent.BOOTSTRAP,
            SetupEvent.VALIDATE,
            SetupEvent.APPLY,
            SetupEvent.FAIL,
        ],
    ):
        assert follow([*prefix, SetupEvent.TIMEOUT])[-1] is SetupPhase.TIMED_OUT


def test_commit_reducer_models_the_only_publish_chain_and_every_rollback():
    success_events = [
        CommitEvent.BEGIN,
        CommitEvent.FILES_STAGED,
        CommitEvent.DATABASE_STAGED,
        CommitEvent.VERIFIED,
        CommitEvent.MARKED,
        CommitEvent.PUBLISHED,
        CommitEvent.POSTCHECKED,
    ]
    expected_phases = [
        CommitPhase.IDLE,
        CommitPhase.STAGING_FILES,
        CommitPhase.STAGING_DATABASE,
        CommitPhase.VERIFYING_STAGE,
        CommitPhase.MARKING_STAGE,
        CommitPhase.PUBLISHING,
        CommitPhase.POSTCHECK,
        CommitPhase.COMPLETE,
    ]
    phase = CommitPhase.IDLE
    visited = [phase]
    for event in success_events:
        phase = commit_transition(phase, event)
        visited.append(phase)
    assert visited == expected_phases

    in_flight = set(expected_phases[1:-1])
    for phase in in_flight:
        assert commit_transition(phase, CommitEvent.ROLLBACK) is CommitPhase.ROLLED_BACK
        assert (
            commit_transition(phase, CommitEvent.ROLLBACK_FAILED)
            is CommitPhase.RECOVERY_REQUIRED
        )
    assert _reachable(CommitPhase.IDLE, COMMIT_TRANSITIONS) == set(CommitPhase)
    for phase in CommitPhase:
        assert _can_reach_terminal(phase, COMMIT_TRANSITIONS, COMMIT_TERMINALS), phase


def test_commit_reducer_rejects_every_undeclared_transition():
    for phase in CommitPhase:
        for event in CommitEvent:
            target = COMMIT_TRANSITIONS.get((phase, event))
            if target is None:
                with pytest.raises(TransitionError, match=event.value):
                    commit_transition(phase, event)
            else:
                assert commit_transition(phase, event) is target
    for terminal in COMMIT_TERMINALS:
        assert all(source != terminal for source, _event in COMMIT_TRANSITIONS)


class _ControlledCommitter:
    def __init__(self, root: Path, *, failures: int = 0, block: bool = False):
        self.state_root = root
        self.instance = root / "instance"
        self.failures = failures
        self.calls = 0
        self.started = Event()
        self.release = Event()
        self.block = block
        self._counter_lock = Lock()
        self.result = CommitResult(
            self.instance,
            self.instance / "config.toml",
            "http://127.0.0.1:8080/",
            {},
        )

    def commit(self, _draft):
        with self._counter_lock:
            self.calls += 1
            call = self.calls
        self.started.set()
        if self.block:
            assert self.release.wait(timeout=5)
        if call <= self.failures:
            raise SetupCommitError("injected commit failure")
        return self.result


def _reviewed_session(tmp_path: Path, committer: _ControlledCommitter) -> tuple[SetupSession, str]:
    session = SetupSession(committer)  # type: ignore[arg-type]
    session.bootstrap()
    token, _review = session.validate(
        preset_payload("recommended"), environment={}, check_port=False,
    )
    return session, token


def test_setup_session_retry_rotates_review_token_and_clears_failure(tmp_path):
    committer = _ControlledCommitter(tmp_path, failures=1)
    session, first_token = _reviewed_session(tmp_path, committer)

    with pytest.raises(SetupCommitError, match="injected"):
        session.complete(first_token)
    assert session.phase is SetupPhase.FAILED
    assert session.public_state()["error"] == "injected commit failure"

    second_token, review = session.validate(
        preset_payload("recommended"), environment={}, check_port=False,
    )
    assert second_token != first_token
    assert review["access"] == "Only this computer · 127.0.0.1"
    assert session.phase is SetupPhase.REVIEWED
    assert session.public_state()["error"] is None
    assert session.complete(second_token) is committer.result
    assert session.phase is SetupPhase.COMPLETE
    assert committer.calls == 2


def test_duplicate_completion_is_idempotent_even_when_requests_overlap(tmp_path):
    committer = _ControlledCommitter(tmp_path, block=True)
    session, token = _reviewed_session(tmp_path, committer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.complete, token)
        assert committer.started.wait(timeout=5)
        second = pool.submit(session.complete, token)
        committer.release.set()
        results = (first.result(timeout=5), second.result(timeout=5))

    assert results[0] is results[1] is committer.result
    assert committer.calls == 1
    assert session.phase is SetupPhase.COMPLETE
    assert session.history[-3:] == [
        (SetupEvent.APPLY.value, SetupPhase.COMMITTING.value),
        (SetupEvent.SUCCEED.value, SetupPhase.COMPLETE.value),
        (SetupEvent.REPLAY.value, SetupPhase.COMPLETE.value),
    ]
    with pytest.raises(SetupCommitError, match="no longer valid"):
        session.complete("stale-token")
    assert committer.calls == 1


@pytest.mark.parametrize("terminal", ["cancel", "timeout"])
@pytest.mark.parametrize("starting_phase", ["listening", "editing", "reviewed", "failed"])
def test_cancel_and_timeout_leave_no_restart_or_apply_dead_end(
    tmp_path, terminal, starting_phase,
):
    committer = _ControlledCommitter(tmp_path, failures=1)
    session = SetupSession(committer)  # type: ignore[arg-type]
    token = None
    if starting_phase != "listening":
        session.bootstrap()
    if starting_phase in {"reviewed", "failed"}:
        token, _ = session.validate(
            preset_payload("recommended"), environment={}, check_port=False,
        )
    if starting_phase == "failed":
        with pytest.raises(SetupCommitError):
            session.complete(token or "")

    getattr(session, terminal)()
    expected = SetupPhase.CANCELLED if terminal == "cancel" else SetupPhase.TIMED_OUT
    assert session.phase is expected
    assert session.public_state()["complete"] is False
    session.timeout()  # Terminal timeout is deliberately idempotent/no-op.
    assert session.phase is expected
    with pytest.raises((TransitionError, SetupCommitError)):
        session.validate(preset_payload("recommended"), environment={}, check_port=False)
    with pytest.raises(SetupCommitError):
        session.complete(token or "not-reviewed")
    assert not committer.instance.exists()


def test_edit_invalidates_review_and_stale_apply_cannot_commit(tmp_path):
    committer = _ControlledCommitter(tmp_path)
    session, token = _reviewed_session(tmp_path, committer)
    session.edit()
    assert session.phase is SetupPhase.EDITING
    assert session.review_token is None
    with pytest.raises(SetupCommitError, match="Review the current settings"):
        session.complete(token)
    assert committer.calls == 0


class _RecoveryCommitter:
    def __init__(self, root: Path):
        self.state_root = root
        self.instance = root / "instance"
        self.commit_phase = CommitPhase.RECOVERY_REQUIRED
        self.calls = 0

    def commit(self, _draft):
        self.calls += 1
        raise SetupRecoveryRequired(
            f"Rollback could not be confirmed; preserve {self.state_root}"
        )


def test_unprovable_rollback_is_a_distinct_nonretryable_setup_terminal(tmp_path):
    committer = _RecoveryCommitter(tmp_path / ".distillfeed")
    session, token = _reviewed_session(tmp_path, committer)  # type: ignore[arg-type]

    with pytest.raises(SetupRecoveryRequired, match="preserve"):
        session.complete(token)

    assert session.phase is SetupPhase.RECOVERY_REQUIRED
    assert session.history[-2:] == [
        (SetupEvent.APPLY.value, SetupPhase.COMMITTING.value),
        (
            SetupEvent.REQUIRE_RECOVERY.value,
            SetupPhase.RECOVERY_REQUIRED.value,
        ),
    ]
    assert session.public_state()["recovery_required"] is True
    assert session.public_state()["recovery_path"] == str(committer.state_root)
    assert session.review_token is None
    assert session.draft is None
    session.timeout()  # Terminal expiry is an idempotent no-op.
    assert session.phase is SetupPhase.RECOVERY_REQUIRED
    with pytest.raises((TransitionError, SetupCommitError)):
        session.validate(preset_payload("recommended"), environment={}, check_port=False)
    with pytest.raises(TransitionError):
        session.cancel()
    with pytest.raises(SetupCommitError):
        session.complete(token)
    assert committer.calls == 1


@pytest.mark.parametrize(
    "phase",
    [
        SetupPhase.LISTENING,
        SetupPhase.COMMITTING,
        SetupPhase.RECOVERY_REQUIRED,
        SetupPhase.COMPLETE,
        SetupPhase.CANCELLED,
        SetupPhase.TIMED_OUT,
    ],
)
def test_forbidden_validate_is_atomic_for_every_noneditable_phase(tmp_path, phase):
    committer = _ControlledCommitter(tmp_path)
    session = SetupSession(committer)  # type: ignore[arg-type]
    sentinel_draft = normalize_setup_payload(
        preset_payload("recommended"), environment={}, check_port=False,
    )
    session.phase = phase
    session.review_token = "sentinel-review-token"
    session.draft = sentinel_draft
    session.result = committer.result
    session.last_error = "sentinel error"
    session.history = [("sentinel-event", "sentinel-phase")]
    before_public = session.public_state()
    before_history = list(session.history)

    with pytest.raises(TransitionError, match=phase.value):
        session.validate(
            preset_payload("recommended"), environment={}, check_port=False,
        )

    assert session.public_state() == before_public
    assert session.phase is phase
    assert session.review_token == "sentinel-review-token"
    assert session.draft is sentinel_draft
    assert session.result is committer.result
    assert session.last_error == "sentinel error"
    assert session.history == before_history
