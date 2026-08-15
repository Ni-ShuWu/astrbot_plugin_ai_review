"""候选审批竞态测试（fix/candidate-approve-race）。

原实现 approve_candidate 在锁内检查存在性后释放锁调用 add()，
再重新上锁消费候选：并发 deny 可在窗口内消费候选，approve 仍会
继续创建规则（拒绝意图被覆盖）；并发双 approve 会出现重复提示。

修复后整个"检查→建规则→消费候选"在同一临界区完成，且 add 拆出
锁内版本 _add_locked 供复用。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

# 以固定别名的命名空间包方式导入插件源码（与 tests/test_core.py 一致）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_plugin_under_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_REPO_ROOT)]  # 命名空间包
_pkg.__package__ = _PKG
sys.modules[_PKG] = _pkg
sys.path.insert(0, str(_REPO_ROOT))

from _plugin_under_test.models import RuleCandidate  # noqa: E402
from _plugin_under_test.review.persistence import KVStore  # noqa: E402
from _plugin_under_test.review.rules import RuleEngine  # noqa: E402

_RULE_CFG = {
    "regex_min_hits": 3,
    "regex_min_accuracy": 0.7,
    "regex_max_rules": 200,
    "enable_regex_prefilter": True,
    "regex_sediment": True,
}


class _FakeKV(KVStore):
    """内存版 KV 存储（用于测试持久化行为）。"""

    def __init__(self) -> None:
        super().__init__(self._get, self._put)
        self.data: dict = {}

    async def _get(self, key: str, default=None):
        return self.data.get(key, default)

    async def _put(self, key: str, value) -> None:
        self.data[key] = value


def _make_candidate(candidate_id: str = "cand0000001") -> RuleCandidate:
    return RuleCandidate(
        candidate_id=candidate_id,
        pattern=r"加我微信",
        note="广告",
        level=1,
        group_id="g1",
        user_id="u1",
        platform_id="aiocqhttp",
        session_id="aiocqhttp:GroupMessage:g1",
        source_task_id="task1",
    )


class CandidateApproveAtomicityTest(unittest.TestCase):
    @staticmethod
    def _engine(**cfg_overrides) -> RuleEngine:
        cfg = dict(_RULE_CFG, **cfg_overrides)
        return RuleEngine(_FakeKV(), lambda gid="": cfg)

    def test_concurrent_double_approve_creates_single_rule(self) -> None:
        rules = self._engine()
        rules._candidates = {"c1": _make_candidate()}

        async def scenario():
            first, second = await asyncio.gather(
                rules.approve_candidate("c1"),
                rules.approve_candidate("c1"),
            )
            return first, second

        first, second = asyncio.run(scenario())
        results = [first[0], second[0]]
        self.assertEqual(results.count(True), 1, (first, second))
        self.assertEqual(len(rules.list()), 1)
        self.assertEqual(rules.candidates(), [])

    def test_approve_deny_interleave_has_single_winner(self) -> None:
        """并发 approve/deny 同一候选时只允许一个赢家（无规则+deny 或规则+approve）。"""
        rules = self._engine()
        rules._candidates = {"c1": _make_candidate()}
        original_add = RuleEngine.add
        original_add_locked = getattr(RuleEngine, "_add_locked", None)
        entered = asyncio.Event()

        async def yielding_add(self, *args, **kwargs):
            entered.set()
            await asyncio.sleep(0.01)
            return await original_add(self, *args, **kwargs)

        async def yielding_add_locked(self, *args, **kwargs):
            entered.set()
            await asyncio.sleep(0.01)
            return await original_add_locked(self, *args, **kwargs)

        async def scenario():
            RuleEngine.add = yielding_add
            if original_add_locked is not None:
                RuleEngine._add_locked = yielding_add_locked
            try:
                approve_task = asyncio.create_task(rules.approve_candidate("c1"))
                await asyncio.wait_for(entered.wait(), timeout=2)
                deny_ok = await rules.deny_candidate("c1")
                approve_ok, message = await asyncio.wait_for(
                    approve_task, timeout=2
                )
            finally:
                RuleEngine.add = original_add
                if original_add_locked is not None:
                    RuleEngine._add_locked = original_add_locked
            return deny_ok, approve_ok, message

        deny_ok, approve_ok, message = asyncio.run(scenario())
        self.assertEqual(rules.candidates(), [])
        self.assertNotEqual(deny_ok, approve_ok, (deny_ok, approve_ok, message))
        self.assertEqual(len(rules.list()), 1 if approve_ok else 0)

    def test_deny_then_approve_does_not_create_rule(self) -> None:
        rules = self._engine()
        rules._candidates = {"c1": _make_candidate()}

        async def scenario():
            deny_ok = await rules.deny_candidate("c1")
            approve_ok, message = await rules.approve_candidate("c1")
            return deny_ok, approve_ok, message

        deny_ok, approve_ok, message = asyncio.run(scenario())
        self.assertTrue(deny_ok)
        self.assertFalse(approve_ok)
        self.assertIn("不存在", message)
        self.assertEqual(rules.list(), [])
        self.assertEqual(rules.candidates(), [])

    def test_duplicate_pattern_failure_keeps_candidate(self) -> None:
        rules = self._engine()
        rules._candidates = {"c1": _make_candidate()}

        async def scenario():
            await rules.add(r"加我微信", source="manual", note="已存在")
            return await rules.approve_candidate("c1")

        ok, message = asyncio.run(scenario())
        self.assertFalse(ok)
        self.assertIn("相同正则已存在", message)
        self.assertEqual(len(rules.candidates()), 1)
        self.assertEqual(len(rules.list()), 1)

    def test_limit_failure_keeps_candidate(self) -> None:
        rules = self._engine(regex_max_rules=1)
        rules._candidates = {"c1": _make_candidate()}

        async def scenario():
            await rules.add(r"别的规则", source="manual")
            return await rules.approve_candidate("c1")

        ok, message = asyncio.run(scenario())
        self.assertFalse(ok)
        self.assertIn("上限", message)
        self.assertEqual(len(rules.candidates()), 1)


if __name__ == "__main__":
    unittest.main()
