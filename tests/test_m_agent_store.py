"""InMemoryRunStore 行为契约测试。

与 SQLiteRunStore 共享同一份 RunStore 行为契约（见
``tests/store_contract.py`` 的 ``RunStoreContractMixin``），
由 Ticket 01 引入、Ticket 02 扩展为两种实现共用的契约套件。
"""

from __future__ import annotations

import unittest

from m_agent import InMemoryRunStore, PlaintextPayloadCodec

from store_contract import RunStoreContractMixin


class InMemoryRunStoreContractTests(
    RunStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    def make_store(  # type: ignore[override]
        self, codec=None, clock=None
    ):
        return InMemoryRunStore(
            payload_codec=codec or PlaintextPayloadCodec(), clock=clock
        )


if __name__ == "__main__":
    unittest.main()
