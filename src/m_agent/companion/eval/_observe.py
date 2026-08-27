"""OBSERVE：对显式选择 Run 的严格只读归一化（ADR 0029 / AC 3-4）。

EvalObserver 只通过公开只读查询路径（get_run / inspect_run）读取
调用方在版本化 ObservationSelection 中显式列出的 Run：不扫描 Store、
不创建 Run、不提交 Session、不恢复/取消/重放，也不以任何方式修改
被观察状态（零写入）。归一化结果与 EXECUTE 共用 immutable
EvalObservation 契约。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._errors import RunNotFoundError
from ..._runner import Runner
from ._identity import digest_of
from ._observation import (
    EvalMode,
    EvalObservation,
    SamplingDisclosure,
    observation_from_inspection,
)

__all__ = ["EvalObserver", "ObservationSelection"]


class ObservationSelection(BaseModel):
    """调用方显式选择且授权的既有 Run 集合（版本化 selection manifest）。

    ``run_ids`` 必须显式、有序且无重复——Eval 绝不扫描 Store 自行
    发现候选；携带 ``sampling`` 披露时，归一化结果标记为 SAMPLED，
    不得表述为全量证明。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    run_ids: tuple[str, ...] = Field(min_length=1)
    sampling: SamplingDisclosure | None = None

    @model_validator(mode="after")
    def _reject_duplicates_and_blanks(self) -> "ObservationSelection":
        seen: set[str] = set()
        for run_id in self.run_ids:
            if not run_id.strip():
                raise ValueError("run_ids must be non-blank identifiers")
            if run_id in seen:
                raise ValueError(
                    f"run_ids must be unique; {run_id!r} appears twice"
                )
            seen.add(run_id)
        return self


class EvalObserver:
    """严格只读的既有 Run 观察者。

    :param runner: 只用于公开只读查询路径的 Runner。本组件不调用
        create/start/resume/cancel/resolve，也不触碰任何 SessionStore。
    """

    def __init__(self, *, runner: Runner) -> None:
        self._runner = runner

    async def observe_run(self, run_id: str) -> EvalObservation:
        """观察单个显式选择的 Run；Run 不存在归一化为 UNAVAILABLE。"""
        try:
            inspection = await self._runner.inspect_run(run_id)
        except RunNotFoundError:
            inspection = None
        return observation_from_inspection(
            observation_id=self._observation_id(run_id, None),
            mode=EvalMode.OBSERVE,
            inspection=inspection,
            subject_run_id=run_id,
        )

    async def observe_selection(
        self, selection: ObservationSelection
    ) -> tuple[EvalObservation, ...]:
        """按显式 selection 逐个归一化；顺序与 run_ids 一致。"""
        observations: list[EvalObservation] = []
        for run_id in selection.run_ids:
            try:
                inspection = await self._runner.inspect_run(run_id)
            except RunNotFoundError:
                inspection = None
            observations.append(
                observation_from_inspection(
                    observation_id=self._observation_id(
                        run_id, selection.selection_id
                    ),
                    mode=EvalMode.OBSERVE,
                    inspection=inspection,
                    subject_run_id=run_id,
                    sampling=selection.sampling,
                )
            )
        return tuple(observations)

    @staticmethod
    def _observation_id(run_id: str, selection_id: str | None) -> str:
        """OBSERVE Observation 身份：由 selection + run 派生（确定性）。"""
        return digest_of(
            "eval-observation",
            EvalMode.OBSERVE.value,
            selection_id or "-",
            run_id,
        )
