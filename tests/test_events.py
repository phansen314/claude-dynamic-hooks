"""EventType — enum and dispatcher method parsing."""
from __future__ import annotations

import pytest

from claude_dynamic_hooks.events import EventType


def test_event_value_round_trip():
    """EventType is a str enum; value matches the on-the-wire camelCase."""
    assert EventType.PRE_TOOL_USE.value == "preToolUse"
    assert EventType("postToolUse") is EventType.POST_TOOL_USE


def test_full_hook_catalog_size():
    """All 29 documented Claude Code hook events are represented."""
    assert len(list(EventType)) == 29


def test_hook_catalog_members():
    """Spot-check several previously-missing hooks made it in."""
    assert EventType("sessionEnd") is EventType.SESSION_END
    assert EventType("taskCreated") is EventType.TASK_CREATED
    assert EventType("worktreeCreate") is EventType.WORKTREE_CREATE
    assert EventType("elicitation") is EventType.ELICITATION
    assert EventType("postToolBatch") is EventType.POST_TOOL_BATCH
    assert EventType("permissionDenied") is EventType.PERMISSION_DENIED
    assert EventType("teammateIdle") is EventType.TEAMMATE_IDLE


def test_hook_prefix_round_trip():
    """Dispatcher strips 'hook.' prefix then calls EventType(suffix)."""
    for ev in EventType:
        method = "hook." + ev.value
        assert method.startswith("hook.")
        assert EventType(method[5:]) is ev


def test_non_hook_prefix_not_in_enum():
    with pytest.raises(ValueError):
        EventType("rpc.something")


def test_unknown_suffix_not_in_enum():
    with pytest.raises(ValueError):
        EventType("frobnicate")
