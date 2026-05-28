import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from .structured_output import OutputSchema
from .trace import NoopTracer, TraceEvent, Tracer
from .types import AgentResult, ToolCall


@dataclass
class ExpectedToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
        }


@dataclass
class EvalCase:
    name: str
    prompt: str
    expected_output: Optional[str] = None
    expected_keywords: List[str] = field(default_factory=list)
    expected_structured: Dict[str, Any] = field(default_factory=dict)
    expected_tool_calls: List[Any] = field(default_factory=list)
    output_schema: Optional[OutputSchema] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalSubjectResult:
    output: str
    structured_output: Optional[Any] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


@dataclass
class EvalCheck:
    name: str
    status: str
    message: str = ""
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    @classmethod
    def passed(
        cls,
        name: str,
        *,
        message: str = "",
        expected: Optional[Any] = None,
        actual: Optional[Any] = None,
    ) -> "EvalCheck":
        return cls(
            name=name,
            status="passed",
            message=message,
            expected=expected,
            actual=actual,
        )

    @classmethod
    def failed(
        cls,
        name: str,
        *,
        message: str,
        expected: Optional[Any] = None,
        actual: Optional[Any] = None,
    ) -> "EvalCheck":
        return cls(
            name=name,
            status="failed",
            message=message,
            expected=expected,
            actual=actual,
        )

    @classmethod
    def skipped(cls, name: str, *, message: str) -> "EvalCheck":
        return cls(name=name, status="skipped", message=message)

    @property
    def passed_check(self) -> bool:
        return self.status == "passed"

    @property
    def failed_check(self) -> bool:
        return self.status == "failed"

    @property
    def skipped_check(self) -> bool:
        return self.status == "skipped"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class EvalCaseResult:
    case_name: str
    output: str
    structured_output: Optional[Any] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[EvalCheck] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def evaluated_checks(self) -> List[EvalCheck]:
        return [check for check in self.checks if not check.skipped_check]

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        evaluated = self.evaluated_checks
        return bool(evaluated) and all(check.passed_check for check in evaluated)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_name": self.case_name,
            "passed": self.passed,
            "output": self.output,
            "structured_output": self.structured_output,
            "tool_calls": self.tool_calls,
            "checks": [check.to_dict() for check in self.checks],
            "error": self.error,
        }


@dataclass
class EvalReport:
    results: List[EvalCaseResult] = field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return len(self.results)

    @property
    def passed_cases(self) -> int:
        return len([result for result in self.results if result.passed])

    @property
    def failed_cases(self) -> int:
        return self.total_cases - self.passed_cases

    @property
    def pass_rate(self) -> float:
        if self.total_cases == 0:
            return 0.0
        return self.passed_cases / self.total_cases

    def summary(self) -> str:
        percent = self.pass_rate * 100
        return (
            f"Eval report: {self.passed_cases}/{self.total_cases} cases passed "
            f"({percent:.1f}%)."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "pass_rate": self.pass_rate,
            "results": [result.to_dict() for result in self.results],
        }


class Evaluator(Protocol):
    name: str

    def evaluate(self, case: EvalCase, result: EvalSubjectResult) -> EvalCheck:
        ...


class ExactMatchEvaluator:
    name = "exact_match"

    def __init__(self, *, strip: bool = True, case_sensitive: bool = True) -> None:
        self.strip = strip
        self.case_sensitive = case_sensitive

    def evaluate(self, case: EvalCase, result: EvalSubjectResult) -> EvalCheck:
        if case.expected_output is None:
            return EvalCheck.skipped(
                self.name,
                message="EvalCase.expected_output is not set.",
            )

        expected = self._normalize(case.expected_output)
        actual = self._normalize(result.output)
        if actual == expected:
            return EvalCheck.passed(
                self.name,
                expected=case.expected_output,
                actual=result.output,
            )
        return EvalCheck.failed(
            self.name,
            message="Output did not exactly match expected_output.",
            expected=case.expected_output,
            actual=result.output,
        )

    def _normalize(self, text: str) -> str:
        value = text.strip() if self.strip else text
        if not self.case_sensitive:
            value = value.lower()
        return value


class ContainsKeywordsEvaluator:
    name = "contains_keywords"

    def __init__(self, *, case_sensitive: bool = False) -> None:
        self.case_sensitive = case_sensitive

    def evaluate(self, case: EvalCase, result: EvalSubjectResult) -> EvalCheck:
        if not case.expected_keywords:
            return EvalCheck.skipped(
                self.name,
                message="EvalCase.expected_keywords is empty.",
            )

        output = result.output if self.case_sensitive else result.output.lower()
        missing = []
        for keyword in case.expected_keywords:
            candidate = keyword if self.case_sensitive else keyword.lower()
            if candidate not in output:
                missing.append(keyword)

        if not missing:
            return EvalCheck.passed(
                self.name,
                expected=case.expected_keywords,
                actual=result.output,
            )
        return EvalCheck.failed(
            self.name,
            message=f"Missing keywords: {', '.join(missing)}",
            expected=case.expected_keywords,
            actual=result.output,
        )


class StructuredFieldEvaluator:
    name = "structured_fields"

    def evaluate(self, case: EvalCase, result: EvalSubjectResult) -> EvalCheck:
        if not case.expected_structured:
            return EvalCheck.skipped(
                self.name,
                message="EvalCase.expected_structured is empty.",
            )

        if result.structured_output is None:
            return EvalCheck.failed(
                self.name,
                message="No structured output was returned.",
                expected=case.expected_structured,
                actual=None,
            )

        mismatches = {}
        for path, expected in case.expected_structured.items():
            found, actual = _get_path(result.structured_output, path)
            if not found or actual != expected:
                mismatches[path] = {"expected": expected, "actual": actual}

        if not mismatches:
            return EvalCheck.passed(
                self.name,
                expected=case.expected_structured,
                actual=result.structured_output,
            )
        return EvalCheck.failed(
            self.name,
            message="Structured fields did not match.",
            expected=case.expected_structured,
            actual=mismatches,
        )


class ToolTrajectoryEvaluator:
    name = "tool_trajectory"

    def __init__(self, *, allow_extra_tool_calls: bool = False) -> None:
        self.allow_extra_tool_calls = allow_extra_tool_calls

    def evaluate(self, case: EvalCase, result: EvalSubjectResult) -> EvalCheck:
        if not case.expected_tool_calls:
            return EvalCheck.skipped(
                self.name,
                message="EvalCase.expected_tool_calls is empty.",
            )

        expected = [
            _coerce_expected_tool_call(item) for item in case.expected_tool_calls
        ]
        actual = result.tool_calls
        if self.allow_extra_tool_calls:
            return self._evaluate_subsequence(expected, actual)
        return self._evaluate_exact(expected, actual)

    def _evaluate_exact(
        self,
        expected: List[ExpectedToolCall],
        actual: List[ToolCall],
    ) -> EvalCheck:
        expected_dicts = [call.to_dict() for call in expected]
        actual_dicts = [call.to_dict() for call in actual]

        if len(expected) != len(actual):
            return EvalCheck.failed(
                self.name,
                message="Tool call count did not match.",
                expected=expected_dicts,
                actual=actual_dicts,
            )

        mismatches = _tool_call_mismatches(expected, actual)
        if not mismatches:
            return EvalCheck.passed(
                self.name,
                expected=expected_dicts,
                actual=actual_dicts,
            )
        return EvalCheck.failed(
            self.name,
            message="Tool call trajectory did not match.",
            expected=expected_dicts,
            actual={"tool_calls": actual_dicts, "mismatches": mismatches},
        )

    def _evaluate_subsequence(
        self,
        expected: List[ExpectedToolCall],
        actual: List[ToolCall],
    ) -> EvalCheck:
        expected_dicts = [call.to_dict() for call in expected]
        actual_dicts = [call.to_dict() for call in actual]
        actual_index = 0

        for expected_call in expected:
            matched = False
            while actual_index < len(actual):
                candidate = actual[actual_index]
                actual_index += 1
                if _tool_call_matches(expected_call, candidate):
                    matched = True
                    break
            if not matched:
                return EvalCheck.failed(
                    self.name,
                    message="Expected tool call was not found in order.",
                    expected=expected_dicts,
                    actual=actual_dicts,
                )

        return EvalCheck.passed(
            self.name,
            expected=expected_dicts,
            actual=actual_dicts,
        )


EvalTarget = Callable[[EvalCase], Any]


class EvalRunner:
    def __init__(
        self,
        target: EvalTarget,
        evaluators: Optional[Iterable[Evaluator]] = None,
        *,
        tracer: Optional[Tracer] = None,
    ) -> None:
        self.target = target
        selected_evaluators = (
            default_evaluators() if evaluators is None else evaluators
        )
        self.evaluators = list(selected_evaluators)
        self.tracer = tracer or NoopTracer()

    def run(self, cases: Iterable[EvalCase]) -> EvalReport:
        case_list = list(cases)
        results: List[EvalCaseResult] = []
        self._trace(
            "eval.run.start",
            case_count=len(case_list),
            evaluator_count=len(self.evaluators),
        )

        for case in case_list:
            self._trace("eval.case.start", case=case.name)
            try:
                subject_result = _coerce_subject_result(self.target(case))
                checks = [
                    evaluator.evaluate(case, subject_result)
                    for evaluator in self.evaluators
                ]
                case_result = EvalCaseResult(
                    case_name=case.name,
                    output=subject_result.output,
                    structured_output=subject_result.structured_output,
                    tool_calls=[
                        tool_call.to_dict() for tool_call in subject_result.tool_calls
                    ],
                    checks=checks,
                )
            except Exception as exc:
                case_result = EvalCaseResult(
                    case_name=case.name,
                    output="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(case_result)
            self._trace(
                "eval.case.end",
                case=case.name,
                passed=case_result.passed,
                error=case_result.error,
            )

        report = EvalReport(results)
        self._trace(
            "eval.run.end",
            total_cases=report.total_cases,
            passed_cases=report.passed_cases,
            failed_cases=report.failed_cases,
            pass_rate=report.pass_rate,
        )
        return report

    def _trace(self, name: str, **data: object) -> None:
        self.tracer.record(TraceEvent(name=name, data=data))


def default_evaluators() -> List[Evaluator]:
    return [
        ExactMatchEvaluator(),
        ContainsKeywordsEvaluator(),
        StructuredFieldEvaluator(),
        ToolTrajectoryEvaluator(),
    ]


def run_agent_evals(
    agent: Any,
    cases: Iterable[EvalCase],
    evaluators: Optional[Iterable[Evaluator]] = None,
    *,
    tracer: Optional[Tracer] = None,
) -> EvalReport:
    def target(case: EvalCase) -> AgentResult:
        return agent.run(case.prompt, output_schema=case.output_schema)

    return EvalRunner(target, evaluators, tracer=tracer).run(cases)


def _coerce_subject_result(value: Any) -> EvalSubjectResult:
    if isinstance(value, EvalSubjectResult):
        return value
    if isinstance(value, AgentResult):
        return EvalSubjectResult(
            output=value.output,
            structured_output=value.structured_output,
            tool_calls=_extract_tool_calls(value.messages),
        )

    structured_output = getattr(value, "structured_output", None)
    tool_calls = _extract_tool_calls(getattr(value, "messages", []))
    if hasattr(value, "output"):
        output = str(getattr(value, "output"))
    elif hasattr(value, "final_output"):
        output = str(getattr(value, "final_output"))
    elif isinstance(value, (dict, list)):
        output = json.dumps(value, ensure_ascii=False, sort_keys=True)
        structured_output = value
    else:
        output = str(value)

    if structured_output is None:
        structured_output = _parse_json_if_possible(output)
    return EvalSubjectResult(
        output=output,
        structured_output=structured_output,
        tool_calls=tool_calls,
    )


def _parse_json_if_possible(text: str) -> Optional[Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def _get_path(value: Any, path: str) -> Tuple[bool, Any]:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return False, None
    return True, current


def _extract_tool_calls(messages: Iterable[Any]) -> List[ToolCall]:
    tool_calls: List[ToolCall] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append(call)
    return tool_calls


def _coerce_expected_tool_call(value: Any) -> ExpectedToolCall:
    if isinstance(value, ExpectedToolCall):
        return value
    if isinstance(value, str):
        return ExpectedToolCall(name=value)
    if isinstance(value, dict):
        name = value.get("name")
        if not name:
            raise ValueError("Expected tool call dictionaries require a name.")
        return ExpectedToolCall(
            name=str(name),
            arguments=dict(value.get("arguments") or {}),
        )
    raise TypeError(f"Unsupported expected tool call: {value!r}")


def _tool_call_mismatches(
    expected: List[ExpectedToolCall],
    actual: List[ToolCall],
) -> List[Dict[str, Any]]:
    mismatches = []
    for index, (expected_call, actual_call) in enumerate(zip(expected, actual)):
        if expected_call.name != actual_call.name:
            mismatches.append(
                {
                    "index": index,
                    "field": "name",
                    "expected": expected_call.name,
                    "actual": actual_call.name,
                }
            )
            continue
        arguments_match = _contains_expected_subset(
            actual_call.arguments,
            expected_call.arguments,
        )
        if not arguments_match:
            mismatches.append(
                {
                    "index": index,
                    "field": "arguments",
                    "expected": expected_call.arguments,
                    "actual": actual_call.arguments,
                }
            )
    return mismatches


def _tool_call_matches(expected: ExpectedToolCall, actual: ToolCall) -> bool:
    return (
        expected.name == actual.name
        and _contains_expected_subset(actual.arguments, expected.arguments)
    )


def _contains_expected_subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, expected_value in expected.items():
            if key not in actual:
                return False
            if not _contains_expected_subset(actual[key], expected_value):
                return False
        return True
    return actual == expected
