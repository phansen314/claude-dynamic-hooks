"""Hook event types — internal canonical names matching Claude's wire spec."""
from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    # Session lifecycle
    SESSION_START = "sessionStart"
    SETUP = "setup"
    SESSION_END = "sessionEnd"

    # Per-turn
    USER_PROMPT_SUBMIT = "userPromptSubmit"
    USER_PROMPT_EXPANSION = "userPromptExpansion"
    STOP = "stop"
    STOP_FAILURE = "stopFailure"

    # Tool execution
    PRE_TOOL_USE = "preToolUse"
    PERMISSION_REQUEST = "permissionRequest"
    PERMISSION_DENIED = "permissionDenied"
    POST_TOOL_USE = "postToolUse"
    POST_TOOL_USE_FAILURE = "postToolUseFailure"
    POST_TOOL_BATCH = "postToolBatch"

    # Subagents
    SUBAGENT_START = "subagentStart"
    SUBAGENT_STOP = "subagentStop"

    # Tasks
    TASK_CREATED = "taskCreated"
    TASK_COMPLETED = "taskCompleted"

    # Agent team
    TEAMMATE_IDLE = "teammateIdle"

    # Context & configuration
    INSTRUCTIONS_LOADED = "instructionsLoaded"
    CONFIG_CHANGE = "configChange"
    FILE_CHANGED = "fileChanged"
    CWD_CHANGED = "cwdChanged"

    # Compaction
    PRE_COMPACT = "preCompact"
    POST_COMPACT = "postCompact"

    # Worktree
    WORKTREE_CREATE = "worktreeCreate"
    WORKTREE_REMOVE = "worktreeRemove"

    # User interaction
    NOTIFICATION = "notification"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitationResult"
