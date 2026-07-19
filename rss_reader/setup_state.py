from __future__ import annotations

from enum import StrEnum


class TransitionError(RuntimeError):
    pass


class SetupPhase(StrEnum):
    LISTENING = "listening"
    EDITING = "editing"
    REVIEWED = "reviewed"
    COMMITTING = "committing"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SetupEvent(StrEnum):
    BOOTSTRAP = "bootstrap"
    VALIDATE = "validate"
    INVALID = "invalid"
    EDIT = "edit"
    APPLY = "apply"
    SUCCEED = "succeed"
    FAIL = "fail"
    REQUIRE_RECOVERY = "require_recovery"
    RETRY = "retry"
    CANCEL = "cancel"
    TIMEOUT = "timeout"
    REPLAY = "replay"


SETUP_TRANSITIONS: dict[tuple[SetupPhase, SetupEvent], SetupPhase] = {
    (SetupPhase.LISTENING, SetupEvent.BOOTSTRAP): SetupPhase.EDITING,
    (SetupPhase.LISTENING, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.LISTENING, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.EDITING, SetupEvent.INVALID): SetupPhase.EDITING,
    (SetupPhase.EDITING, SetupEvent.VALIDATE): SetupPhase.REVIEWED,
    (SetupPhase.EDITING, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.EDITING, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.REVIEWED, SetupEvent.EDIT): SetupPhase.EDITING,
    (SetupPhase.REVIEWED, SetupEvent.APPLY): SetupPhase.COMMITTING,
    (SetupPhase.REVIEWED, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.REVIEWED, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    (SetupPhase.COMMITTING, SetupEvent.SUCCEED): SetupPhase.COMPLETE,
    (SetupPhase.COMMITTING, SetupEvent.FAIL): SetupPhase.FAILED,
    (SetupPhase.COMMITTING, SetupEvent.REQUIRE_RECOVERY): SetupPhase.RECOVERY_REQUIRED,
    (SetupPhase.FAILED, SetupEvent.RETRY): SetupPhase.EDITING,
    (SetupPhase.FAILED, SetupEvent.VALIDATE): SetupPhase.REVIEWED,
    (SetupPhase.FAILED, SetupEvent.CANCEL): SetupPhase.CANCELLED,
    (SetupPhase.FAILED, SetupEvent.TIMEOUT): SetupPhase.TIMED_OUT,
    # A repeated completion request is idempotent and returns the first result.
    (SetupPhase.COMPLETE, SetupEvent.REPLAY): SetupPhase.COMPLETE,
}


def setup_transition(phase: SetupPhase, event: SetupEvent) -> SetupPhase:
    try:
        return SETUP_TRANSITIONS[(phase, event)]
    except KeyError as exc:
        raise TransitionError(f"Setup event {event.value!r} is invalid while {phase.value!r}") from exc


class CommitPhase(StrEnum):
    IDLE = "idle"
    STAGING_FILES = "staging_files"
    STAGING_DATABASE = "staging_database"
    VERIFYING_STAGE = "verifying_stage"
    MARKING_STAGE = "marking_stage"
    PUBLISHING = "publishing"
    POSTCHECK = "postcheck"
    COMPLETE = "complete"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class CommitEvent(StrEnum):
    BEGIN = "begin"
    FILES_STAGED = "files_staged"
    DATABASE_STAGED = "database_staged"
    VERIFIED = "verified"
    MARKED = "marked"
    PUBLISHED = "published"
    POSTCHECKED = "postchecked"
    ROLLBACK = "rollback"
    ROLLBACK_FAILED = "rollback_failed"


COMMIT_TRANSITIONS: dict[tuple[CommitPhase, CommitEvent], CommitPhase] = {
    (CommitPhase.IDLE, CommitEvent.BEGIN): CommitPhase.STAGING_FILES,
    (CommitPhase.STAGING_FILES, CommitEvent.FILES_STAGED): CommitPhase.STAGING_DATABASE,
    (CommitPhase.STAGING_DATABASE, CommitEvent.DATABASE_STAGED): CommitPhase.VERIFYING_STAGE,
    (CommitPhase.VERIFYING_STAGE, CommitEvent.VERIFIED): CommitPhase.MARKING_STAGE,
    (CommitPhase.MARKING_STAGE, CommitEvent.MARKED): CommitPhase.PUBLISHING,
    (CommitPhase.PUBLISHING, CommitEvent.PUBLISHED): CommitPhase.POSTCHECK,
    (CommitPhase.POSTCHECK, CommitEvent.POSTCHECKED): CommitPhase.COMPLETE,
}

for _phase in (
    CommitPhase.STAGING_FILES,
    CommitPhase.STAGING_DATABASE,
    CommitPhase.VERIFYING_STAGE,
    CommitPhase.MARKING_STAGE,
    CommitPhase.PUBLISHING,
    CommitPhase.POSTCHECK,
):
    COMMIT_TRANSITIONS[(_phase, CommitEvent.ROLLBACK)] = CommitPhase.ROLLED_BACK
    COMMIT_TRANSITIONS[(_phase, CommitEvent.ROLLBACK_FAILED)] = CommitPhase.RECOVERY_REQUIRED


def commit_transition(phase: CommitPhase, event: CommitEvent) -> CommitPhase:
    try:
        return COMMIT_TRANSITIONS[(phase, event)]
    except KeyError as exc:
        raise TransitionError(f"Commit event {event.value!r} is invalid while {phase.value!r}") from exc
