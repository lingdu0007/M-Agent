"""Bounded M-Agent 0.1 compatibility shim for the 0.2.x window.

Only the original synchronous Agent loop and directly mappable data types are
preserved. Unsupported concepts fail explicitly with migration guidance.
"""

from __future__ import annotations

import importlib
import importlib.abc
import sys
import warnings


class LegacyMigrationError(RuntimeError):
    """A legacy concept has no semantics-preserving M-Agent 0.2 mapping."""


warnings.warn(
    "agent_framework is deprecated in M-Agent 0.2.x and will be removed in "
    "0.3.0. Use m_agent; see docs/migrating-from-0.1.md.",
    DeprecationWarning,
    stacklevel=2,
)

_SUPPORTED = {
    "Agent": (".agent", "Agent", "m_agent.SyncRunner plus AgentDefinition"),
    "AgentResult": (".types", "AgentResult", "m_agent.RunRecord and RunInspection"),
    "EchoModel": (".models", "EchoModel", "m_agent.DeterministicModelAdapter"),
    "Message": (".types", "Message", "m_agent.ModelRequest"),
    "ModelClient": (".models", "ModelClient", "m_agent.ModelAdapter"),
    "ModelResponse": (".types", "ModelResponse", "m_agent.ModelResponse"),
    "OpenAICompatibleClient": (
        ".models", "OpenAICompatibleClient", "m_agent.provider.ChatCompletionsModelAdapter"
    ),
    "OpenAIResponsesClient": (
        ".models", "OpenAIResponsesClient", "m_agent.provider.ResponsesModelAdapter"
    ),
    "RuleBasedDemoModel": (
        ".models", "RuleBasedDemoModel", "m_agent.DeterministicModelAdapter"
    ),
    "Tool": (".tools", "Tool", "m_agent.Tool with ToolEffect"),
    "ToolCall": (".types", "ToolCall", "m_agent.ToolCall"),
    "ToolRegistry": (".tools", "ToolRegistry", "AgentDefinition.tools"),
    "tool": (".tools", "tool", "an explicit m_agent.Tool adapter"),
}

_UNSUPPORTED = {
    "AgentStep": "compose multiple Agent Runs in the embedding application",
    "ContainsKeywordsEvaluator": "keep evaluation outside Runtime Core",
    "DocumentChunk": "pass external data through ContextItem",
    "EvalCase": "keep evaluation outside Runtime Core",
    "EvalCaseResult": "keep evaluation outside Runtime Core",
    "EvalCheck": "keep evaluation outside Runtime Core",
    "EvalReport": "keep evaluation outside Runtime Core",
    "EvalRunner": "keep evaluation outside Runtime Core",
    "EvalSubjectResult": "keep evaluation outside Runtime Core",
    "Evaluator": "keep evaluation outside Runtime Core",
    "ExpectedToolCall": "keep evaluation outside Runtime Core",
    "ExactMatchEvaluator": "keep evaluation outside Runtime Core",
    "FunctionStep": "compose multiple Agent Runs in the embedding application",
    "Guardrail": "enforce constraints in application code before Runner commands",
    "GuardrailDecision": "enforce constraints in application code before Runner commands",
    "InMemoryMemory": "supply context explicitly; Session is not 0.2 core",
    "InMemorySessionMemory": "supply context explicitly; Session is not 0.2 core",
    "InMemoryTracer": "use m_agent.TelemetrySink",
    "JsonDirectorySessionMemory": "supply context explicitly; Session is not 0.2 core",
    "JsonFileMemory": "supply context explicitly; Session is not 0.2 core",
    "JsonlTracer": "use m_agent.JsonlTelemetrySink",
    "KeywordRagIndex": "implement a ContextProvider or ordinary Tool adapter",
    "Memory": "supply context explicitly; Session is not 0.2 core",
    "MultiAgentContext": "compose multiple Agent Runs in the embedding application",
    "MultiAgentResult": "compose multiple Agent Runs in the embedding application",
    "MultiAgentTeam": "compose multiple Agent Runs in the embedding application",
    "NoopTracer": "use m_agent.TelemetrySink",
    "OutputSchema": "validate the final Run output in the embedding application",
    "RetrievedChunk": "pass external data through ContextItem",
    "SensitiveArgumentGuardrail": "enforce constraints in application code before Runner commands",
    "SessionMemory": "supply context explicitly; Session is not 0.2 core",
    "StepResult": "compose multiple Agent Runs in the embedding application",
    "StructuredFieldEvaluator": "keep evaluation outside Runtime Core",
    "StructuredOutputError": "validate the final Run output in the embedding application",
    "TeamMember": "compose multiple Agent Runs in the embedding application",
    "ToolAllowlistGuardrail": "enforce constraints in application code before Runner commands",
    "ToolDenylistGuardrail": "enforce constraints in application code before Runner commands",
    "ToolTrajectoryEvaluator": "keep evaluation outside Runtime Core",
    "TraceEvent": "use m_agent.TelemetryEvent",
    "Tracer": "use m_agent.TelemetrySink",
    "Workflow": "compose multiple Agent Runs in the embedding application",
    "WorkflowContext": "compose multiple Agent Runs in the embedding application",
    "default_evaluators": "keep evaluation outside Runtime Core",
    "load_env_file": "load configuration in the embedding application",
    "make_file_tools": "provide an application-owned Tool adapter",
    "make_rag_tools": "implement a ContextProvider or ordinary Tool adapter",
    "parse_structured_output": "validate the final Run output in the embedding application",
    "run_agent_evals": "keep evaluation outside Runtime Core",
}

_UNSUPPORTED_MODULES = {
    name: guidance
    for name, guidance in {
        "builtin_tools": "provide an application-owned Tool adapter",
        "config": "load configuration in the embedding application",
        "evals": "keep evaluation outside Runtime Core",
        "guardrails": "enforce constraints in application code before Runner commands",
        "memory": "supply context explicitly; Session is not 0.2 core",
        "multi_agent": "compose multiple Agent Runs in the embedding application",
        "rag": "implement a ContextProvider or ordinary Tool adapter",
        "session_memory": "supply context explicitly; Session is not 0.2 core",
        "structured_output": "validate the final Run output in the embedding application",
        "trace": "use m_agent.TelemetrySink",
        "workflow": "compose multiple Agent Runs in the embedding application",
    }.items()
}


class _UnsupportedModuleFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        prefix = f"{__name__}."
        if fullname.startswith(prefix):
            leaf = fullname[len(prefix):]
            if leaf == "_legacy" or leaf.startswith("_legacy."):
                raise LegacyMigrationError(
                    "agent_framework._legacy is not a supported M-Agent 0.2 "
                    "compatibility namespace. See docs/migrating-from-0.1.md."
                )
            guidance = _UNSUPPORTED_MODULES.get(leaf)
            if guidance:
                raise LegacyMigrationError(
                    f"{fullname} has no semantics-preserving M-Agent 0.2 mapping; "
                    f"{guidance}. See docs/migrating-from-0.1.md."
                )
        return None


if not any(isinstance(finder, _UnsupportedModuleFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _UnsupportedModuleFinder())


def __getattr__(name: str):
    supported = _SUPPORTED.get(name)
    if supported is not None:
        module_name, attribute_name, replacement = supported
        warnings.warn(
            f"agent_framework.{name} is deprecated in M-Agent 0.2.x and will be "
            f"removed in 0.3.0. Migrate to {replacement}; see "
            "docs/migrating-from-0.1.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(module_name, __name__), attribute_name)
    guidance = _UNSUPPORTED.get(name)
    if guidance is None:
        raise AttributeError(name)
    raise LegacyMigrationError(
        f"agent_framework.{name} has no semantics-preserving M-Agent 0.2 mapping; "
        f"{guidance}. See docs/migrating-from-0.1.md."
    )


__all__ = ["LegacyMigrationError", *_SUPPORTED]
