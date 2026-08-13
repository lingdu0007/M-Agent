"""Runtime Core public contracts.

This namespace owns one Agent Run's lifecycle and the ports needed to execute
it. Integrations that need concrete storage, provider, or telemetry adapters
should import those from :mod:`m_agent.adapters` instead.
"""

from .._clock import Clock
from .._codec import PayloadCodec
from .._context import ContextItem, ContextProvider, ContextRequest
from .._definition import (
    AgentDefinition,
    DefinitionRegistry,
    DefinitionSnapshot,
    RetryPolicy,
)
from .._errors import (
    DefinitionConflictError,
    DefinitionNotFoundError,
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    MAgentError,
    ModelCapabilityError,
    ResolutionNotAllowedError,
    RunNotFoundError,
    StaleRunVersionError,
)
from .._failure import ModelFailure, StepFailure, ToolFailure
from .._model import (
    ModelAdapter,
    ModelCapabilities,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from .._resolution import (
    ALLOWED_FOR_DEFINITION_UNAVAILABLE,
    ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT,
    ResolutionAction,
    RunResolution,
    allowed_resolutions,
)
from .._run import RunInspection, RunRecord
from .._runner import (
    DEFAULT_LEASE_TTL,
    ERROR_EFFECT_UNCONFIRMED,
    REASON_DEFINITION_UNAVAILABLE,
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    Runner,
)
from .._status import RunStatus, is_terminal
from .._steps import (
    FailureClassification,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
)
from .._store import RunLease, RunStore
from .._sync import SyncRunner
from .._telemetry import TelemetryEvent, TelemetryEventType, TelemetrySink
from .._tools import (
    DEFAULT_TOOL_PARAMETERS,
    Tool,
    ToolCall,
    ToolDeclaration,
    ToolEffect,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRequest,
    ToolSpec,
)
from .._updates import RunUpdate, RunUpdateType

__all__ = [
    "AgentDefinition",
    "ALLOWED_FOR_DEFINITION_UNAVAILABLE",
    "ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT",
    "Clock",
    "ContextItem",
    "ContextProvider",
    "ContextRequest",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_TOOL_PARAMETERS",
    "DefinitionConflictError",
    "DefinitionNotFoundError",
    "DefinitionRegistry",
    "DefinitionSnapshot",
    "DuplicateRunError",
    "ERROR_EFFECT_UNCONFIRMED",
    "FailureClassification",
    "IllegalRunTransitionError",
    "LeaseNotHeldError",
    "MAgentError",
    "ModelAdapter",
    "ModelCapabilities",
    "ModelCapabilityError",
    "ModelDelta",
    "ModelFailure",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "PayloadCodec",
    "REASON_DEFINITION_UNAVAILABLE",
    "REASON_UNCERTAIN_NON_IDEMPOTENT",
    "ResolutionAction",
    "ResolutionNotAllowedError",
    "RetryPolicy",
    "RunInspection",
    "RunLease",
    "RunNotFoundError",
    "RunRecord",
    "Runner",
    "RunResolution",
    "RunStatus",
    "RunStore",
    "RunUpdate",
    "RunUpdateType",
    "StaleRunVersionError",
    "StepAttempt",
    "StepCheckpoint",
    "StepFailure",
    "StepRecord",
    "StepStatus",
    "StepType",
    "SyncRunner",
    "TelemetryEvent",
    "TelemetryEventType",
    "TelemetrySink",
    "Tool",
    "ToolCall",
    "ToolDeclaration",
    "ToolEffect",
    "ToolFailure",
    "ToolOutcome",
    "ToolOutcomeStatus",
    "ToolRequest",
    "ToolSpec",
    "allowed_resolutions",
    "is_terminal",
]
