"""Eval Companion 内部共享的确定性摘要 helper。

所有 identity（bundle digest、item id、suite digest、evidence digest）
都从规范 JSON 派生，同一输入恒得到同一输出；不引入随机源。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Evidence digest 的稳定域前缀（防跨域摘要碰撞的可读版本标记）。
_EVIDENCE_DIGEST_DOMAIN = "m-agent-evidence-v1"


def canonical_json(value: Any) -> str:
    """按固定选项序列化 JSON：键排序、无空格、ASCII 安全。"""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def sha256_hex(text: str) -> str:
    """字符串的稳定 sha256 hex 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(*parts: str) -> str:
    """把若干部件以换行连接后取摘要（部件内含换行时仍无歧义）。"""
    return sha256_hex("\n".join(parts))


def evidence_digest(artifact_id: str, subject_ref: str, payload: str) -> str:
    """Evidence Artifact 的统一内容摘要（三类 Adapter 共用）。

    FIELD 写入方用同一 helper 计算 content-addressed 文件名/index 摘要，
    读取方按同一公式复核，从而检测任何读取期篡改。
    """
    return digest_of(_EVIDENCE_DIGEST_DOMAIN, artifact_id, subject_ref, payload)
