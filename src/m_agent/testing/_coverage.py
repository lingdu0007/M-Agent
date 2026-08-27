"""Coverage Matrix 文档与冻结 Manifest 的一致性校验（Ticket 22）。

发布文档 ``docs/acceptance-coverage-matrix.md`` 必须为 0.5 发布
Manifest 的**每一个** required check 记录一行 ``| `check-id` | ... |``
表格行。:func:`coverage_matrix_gaps` 解析文档中实际记录的 check id 集合
并返回缺失的 required check id（gap）——任何一个 required check 没有
文档行都是发布材料的完整性缺口，不能凭执行结果补写。
"""

from __future__ import annotations

import re

from ._pack import AcceptanceManifest

__all__ = ["coverage_matrix_gaps"]

# 表格行第一个单元格中的反引号 check id，例如 ``| `model.routing.fallback` | ...``。
_DOCUMENTED_CHECK_ROW = re.compile(r"^\|\s*`([a-z0-9][a-z0-9._-]*)`", re.MULTILINE)


def documented_check_ids(markdown: str) -> frozenset[str]:
    """Return every check id documented in a leading table cell."""
    return frozenset(_DOCUMENTED_CHECK_ROW.findall(markdown))


def coverage_matrix_gaps(
    markdown: str, manifest: AcceptanceManifest
) -> tuple[str, ...]:
    """Return the required check ids that the coverage matrix does not document."""
    documented = documented_check_ids(markdown)
    return tuple(
        check.check_id
        for check in manifest.required_checks
        if check.check_id not in documented
    )
