"""审批→处罚原子性测试（fix/task-punish-atomicity）。

处罚失败（平台接口错误）时，任务不得停留在已通过状态：应恢复为
待处理以便重试，且不记录决策/规则反馈（避免污染统计与熔断）。
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

# 以固定别名的命名空间包方式导入插件源码（与 tests/test_core.py 一致）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_plugin_under_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_REPO_ROOT)]  # 命名空间包
_pkg.__package__ = _PKG
sys.modules[_PKG] = _pkg
sys.path.insert(0, str(_REPO_ROOT))

from _plugin_under_test.commands.review import ReviewCommandMixin  # noqa: E402
from _plugin_under_test.models import (  # noqa: E402
    ReviewResult,
    ReviewStatus,
    ReviewTask,
)
from _plugin_under_test.review.punish_stages import (  # noqa: E402
    MuteStrategy,
    StageResult,
)
from _plugin_under_test.review.punishment import Punisher  # noqa: E402
from _plugin_under_test.review.queue import ReviewQueue  # noqa: E402


def _make_task(rule_id: str = "") -> ReviewTask:
    return ReviewTask.create(
        group_id="g1",
        user_id="u1",
        nickname="n",
        result=ReviewResult.from_dict(
            {
                "illegal": True,
                "risk": 95,
                "type": "t",
                "reason": "r",
                "evidence": ["e"],
                "suggestion": "mute",
            }
        ),
        context=[],
        timeout=3600,
        platform_id="aiocqhttp",
        session_id="aiocqhttp:GroupMessage:g1",
        rule_id=rule_id,
        llm_provider="stub",
    )


class _FailingExecutor:
    async def send_message(self, session, text):
        return ""

    async def ban_user(self, platform_id, group_id, user_id, duration):
        return "执行 set_group_ban 失败: network error"


class _OkExecutor:
    async def send_message(self, session, text):
        return ""

    async def ban_user(self, platform_id, group_id, user_id, duration):
        return ""


class _RecordingRules:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, bool]] = []

    async def record_decision(self, rule_id: str, approved: bool) -> None:
        self.decisions.append((rule_id, approved))


class _RecordingStats:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, bool, str]] = []

    async def record_decision(self, group_id, user_id, approved, punishment):
        self.decisions.append((group_id, user_id, approved, punishment))


class _Event:
    def get_sender_id(self) -> str:
        return "admin1"


def _make_mixin(
    task: ReviewTask,
    failed: bool,
    rules: _RecordingRules | None = None,
    stats: _RecordingStats | None = None,
) -> ReviewCommandMixin:
    mixin = object.__new__(ReviewCommandMixin)
    mixin.queue = ReviewQueue()
    asyncio.run(mixin.queue.add(task))
    mixin._task_id = task.task_id

    class _Punisher:
        async def execute(self, task, admin_id):
            return SimpleNamespace(
                failed=failed,
                message="禁言失败：network error" if failed else "已禁言 n 10 分钟。",
            )

    mixin.punisher = _Punisher()
    mixin.stats = stats if stats is not None else _RecordingStats()
    mixin.rules = rules if rules is not None else _RecordingRules()
    return mixin


class StageResultTest(unittest.TestCase):
    def test_mute_failure_reports_failed(self) -> None:
        strategy = MuteStrategy(_FailingExecutor(), 600)
        task = _make_task()
        result = asyncio.run(strategy.execute(task, "admin1"))
        self.assertIsInstance(result, StageResult)
        self.assertFalse(result.success)
        self.assertIn("禁言失败", result.message)

    def test_mute_success_reports_ok(self) -> None:
        strategy = MuteStrategy(_OkExecutor(), 600)
        result = asyncio.run(strategy.execute(_make_task(), "admin1"))
        self.assertTrue(result.success)
        self.assertIn("已禁言", result.message)

    def test_skip_reports_success(self) -> None:
        strategy = MuteStrategy(_OkExecutor(), 600)
        task = _make_task()
        task.user_id = ""
        result = asyncio.run(strategy.execute(task, "admin1"))
        self.assertTrue(result.success)


class PunisherOutcomeTest(unittest.TestCase):
    def test_pipeline_failure_flag(self) -> None:
        cfg = {"enable_blacklist": False, "mute_duration": 600, "punish_pipeline": {}}
        punisher = Punisher(_FailingExecutor(), None, lambda gid="": cfg)
        outcome = asyncio.run(punisher.execute(_make_task(), "admin1"))
        self.assertTrue(outcome.failed)
        self.assertIn("禁言失败", outcome.message)

    def test_pipeline_success_flag(self) -> None:
        cfg = {"enable_blacklist": False, "mute_duration": 600, "punish_pipeline": {}}
        punisher = Punisher(_OkExecutor(), None, lambda gid="": cfg)
        outcome = asyncio.run(punisher.execute(_make_task(), "admin1"))
        self.assertFalse(outcome.failed)
        self.assertIn("已禁言", outcome.message)


class QueueRevertTest(unittest.TestCase):
    def test_revert_restores_pending_and_clears_admin(self) -> None:
        task = _make_task(rule_id="r1")

        async def scenario():
            queue = ReviewQueue()
            await queue.add(task)
            approved = await queue.approve(task.task_id, "admin1")
            self.assertIsNotNone(approved)
            reverted = await queue.revert_to_pending(task.task_id)
            self.assertIsNotNone(reverted)
            restored = await queue.get(task.task_id)
            return restored

        restored = asyncio.run(scenario())
        self.assertEqual(restored.status, ReviewStatus.PENDING)
        self.assertEqual(restored.admin_id, "")
        self.assertIsNone(restored.decided_at)
        self.assertGreater(restored.expires_at, time.time())

    def test_revert_only_applies_to_approved(self) -> None:
        task = _make_task()

        async def scenario():
            queue = ReviewQueue()
            await queue.add(task)
            await queue.reject(task.task_id, "admin1")
            return await queue.revert_to_pending(task.task_id)

        self.assertIsNone(asyncio.run(scenario()))

    def test_revert_missing_task_returns_none(self) -> None:
        async def scenario():
            queue = ReviewQueue()
            return await queue.revert_to_pending("no-such-task")

        self.assertIsNone(asyncio.run(scenario()))


class ApproveAtomicityTest(unittest.TestCase):
    def _approve(self, mixin: ReviewCommandMixin) -> str:
        event = _Event()
        task = asyncio.run(mixin.queue.get(mixin._task_id))
        return asyncio.run(mixin._approve_task(event, task))

    def test_punishment_failure_reverts_task(self) -> None:
        task = _make_task()
        mixin = _make_mixin(task, failed=True)
        result = self._approve(mixin)
        self.assertIn("恢复为待处理", result)
        restored = asyncio.run(mixin.queue.get(task.task_id))
        self.assertEqual(restored.status, ReviewStatus.PENDING)

    def test_punishment_failure_skips_decision_recording(self) -> None:
        task = _make_task(rule_id="r1")
        rules = _RecordingRules()
        stats = _RecordingStats()
        mixin = _make_mixin(task, failed=True, rules=rules, stats=stats)
        self._approve(mixin)
        self.assertEqual(rules.decisions, [])
        self.assertEqual(stats.decisions, [])

    def test_punishment_success_records_decision(self) -> None:
        task = _make_task(rule_id="r1")
        rules = _RecordingRules()
        stats = _RecordingStats()
        mixin = _make_mixin(task, failed=False, rules=rules, stats=stats)
        result = self._approve(mixin)
        self.assertIn("已通过任务", result)
        restored = asyncio.run(mixin.queue.get(task.task_id))
        self.assertEqual(restored.status, ReviewStatus.APPROVED)
        self.assertEqual(rules.decisions, [("r1", True)])
        self.assertEqual(len(stats.decisions), 1)


if __name__ == "__main__":
    unittest.main()
