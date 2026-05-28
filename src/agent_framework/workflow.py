from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol

from .agent import Agent
from .trace import NoopTracer, TraceEvent, Tracer


WorkflowContext = Dict[str, Any]


@dataclass
class StepResult:
    updates: WorkflowContext = field(default_factory=dict)
    next_step: Optional[str] = None
    stop: bool = False

    @classmethod
    def next(cls, step_name: str, **updates: Any) -> "StepResult":
        return cls(updates=updates, next_step=step_name)

    @classmethod
    def done(cls, **updates: Any) -> "StepResult":
        return cls(updates=updates, stop=True)


class WorkflowStep(Protocol):
    name: str

    def run(self, context: WorkflowContext) -> StepResult:
        ...


class FunctionStep:
    def __init__(
        self,
        name: str,
        func: Callable[[WorkflowContext], Any],
        *,
        next_step: Optional[str] = None,
    ) -> None:
        self.name = name
        self.func = func
        self.next_step = next_step

    def run(self, context: WorkflowContext) -> StepResult:
        value = self.func(context)
        if isinstance(value, StepResult):
            return value
        if isinstance(value, dict):
            return StepResult(updates=value, next_step=self.next_step)
        return StepResult(updates={self.name: value}, next_step=self.next_step)


class AgentStep:
    def __init__(
        self,
        name: str,
        agent: Agent,
        prompt: Callable[[WorkflowContext], str],
        *,
        output_key: Optional[str] = None,
        next_step: Optional[str] = None,
    ) -> None:
        self.name = name
        self.agent = agent
        self.prompt = prompt
        self.output_key = output_key or name
        self.next_step = next_step

    def run(self, context: WorkflowContext) -> StepResult:
        result = self.agent.run(self.prompt(context))
        updates = {self.output_key: result.output}
        if result.structured_output is not None:
            updates[f"{self.output_key}_structured"] = result.structured_output
        return StepResult(updates=updates, next_step=self.next_step)


class Workflow:
    def __init__(
        self,
        steps: Iterable[WorkflowStep],
        *,
        start: str,
        tracer: Optional[Tracer] = None,
        max_steps: int = 20,
    ) -> None:
        self.steps = {step.name: step for step in steps}
        self.start = start
        self.tracer = tracer or NoopTracer()
        self.max_steps = max_steps
        if start not in self.steps:
            raise ValueError(f"Workflow start step not found: {start}")

    def run(self, initial_context: Optional[WorkflowContext] = None) -> WorkflowContext:
        context = dict(initial_context or {})
        current = self.start
        visited: List[str] = []
        self._trace("workflow.run.start", start=current)

        for index in range(1, self.max_steps + 1):
            step = self.steps.get(current)
            if step is None:
                raise ValueError(f"Workflow step not found: {current}")

            visited.append(current)
            self._trace(
                "workflow.step.start",
                step=current,
                step_index=index,
                context_keys=sorted(context.keys()),
            )
            result = step.run(context)
            context.update(result.updates)
            self._trace(
                "workflow.step.end",
                step=current,
                update_keys=sorted(result.updates.keys()),
                next_step=result.next_step,
                stop=result.stop,
            )

            if result.stop:
                self._trace("workflow.run.end", stop_reason="step", steps=visited)
                return context

            next_step = result.next_step or self._next_linear_step(current)
            if next_step is None:
                self._trace("workflow.run.end", stop_reason="complete", steps=visited)
                return context
            current = next_step

        self._trace("workflow.run.end", stop_reason="max_steps", steps=visited)
        raise RuntimeError(f"Workflow exceeded max_steps={self.max_steps}")

    def _next_linear_step(self, current: str) -> Optional[str]:
        names = list(self.steps.keys())
        index = names.index(current)
        if index + 1 >= len(names):
            return None
        return names[index + 1]

    def _trace(self, name: str, **data: object) -> None:
        self.tracer.record(TraceEvent(name=name, data=data))
