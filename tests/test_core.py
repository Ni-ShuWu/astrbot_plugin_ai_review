"""核心逻辑冒烟测试（标准库 unittest，无需安装 astrbot/pytest）。

运行方式：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
import unittest
from pathlib import Path

# 以固定别名的命名空间包方式导入插件源码，与仓库目录名解耦：
# 将仓库根目录注册为命名空间包 _plugin_under_test，其子模块的相对导入
# （如 review.punishment 中的 ..models）可正常解析。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_plugin_under_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_REPO_ROOT)]  # 命名空间包
_pkg.__package__ = _PKG
sys.modules[_PKG] = _pkg
sys.path.insert(0, str(_REPO_ROOT))

from _plugin_under_test.config import ConfigManager  # noqa: E402
from _plugin_under_test.models import ChatRecord, ReviewResult, ReviewTask  # noqa: E402
from _plugin_under_test.prompt import PromptManager  # noqa: E402
from _plugin_under_test.review.persistence import KVStore  # noqa: E402
from _plugin_under_test.review.filters import to_record, trim_records  # noqa: E402
from _plugin_under_test.review.history import HistoryCache  # noqa: E402
from _plugin_under_test.review.punishment import Punisher  # noqa: E402
from _plugin_under_test.review.queue import ReviewQueue  # noqa: E402
from _plugin_under_test.review.rules import RuleEngine  # noqa: E402
from _plugin_under_test.review.stats import StatsStore  # noqa: E402
from _plugin_under_test.review.workflow import ReviewWorkflow  # noqa: E402
from _plugin_under_test.utils.llm import LLMClient  # noqa: E402
from _plugin_under_test.utils.logger import (  # noqa: E402
    get_logger,
    review_context,
)
from _plugin_under_test.utils.parser import parse_review_result  # noqa: E402

# 轻量 fake astrbot 模块：让 commands/review.py、main.py 等可在无 AstrBot
# 环境导入，从而运行纯逻辑/格式化测试；组件行为与真实 AstrBot 对齐。
if "astrbot" not in sys.modules:
    _fake_astrbot = types.ModuleType("astrbot")
    _fake_astrbot.__path__ = []
    _fake_api = types.ModuleType("astrbot.api")
    _fake_api.__path__ = []

    _filter = types.ModuleType("astrbot.api.event.filter")
    _filter.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    _filter.PermissionType = types.SimpleNamespace(ADMIN="admin", MEMBER="member")
    _filter.event_message_type = lambda *a, **k: (lambda fn: fn)
    _filter.command = lambda *a, **k: (lambda fn: fn)
    _filter.permission_type = lambda *a, **k: (lambda fn: fn)

    _fake_event = types.ModuleType("astrbot.api.event")
    _fake_event.AstrMessageEvent = object
    _fake_event.filter = _filter

    class _FakeAt:
        """模拟 AstrBot At 组件（AtAll 为其子类，qq="all" 表示全体）。"""

        type = "at"

        def __init__(self, qq: str = "", name: str = "", **_k) -> None:
            self.qq = qq
            self.name = name

    class _FakeAtAll(_FakeAt):
        def __init__(self, qq: str = "all", **kw) -> None:
            super().__init__(qq=qq, **kw)

    class _FakePlain:
        type = "text"

        def __init__(self, text: str = "", **_k) -> None:
            self.text = text

    _fake_components = types.ModuleType("astrbot.api.message_components")
    _fake_components.At = _FakeAt
    _fake_components.AtAll = _FakeAtAll
    _fake_components.Plain = _FakePlain

    _fake_star = types.ModuleType("astrbot.api.star")
    _fake_star.Context = object
    _fake_star.Star = object
    _fake_star.register = lambda *a, **k: (lambda cls: cls)

    sys.modules["astrbot"] = _fake_astrbot
    sys.modules["astrbot.api"] = _fake_api
    sys.modules["astrbot.api.event"] = _fake_event
    sys.modules["astrbot.api.event.filter"] = _filter
    sys.modules["astrbot.api.message_components"] = _fake_components
    sys.modules["astrbot.api.star"] = _fake_star

from _plugin_under_test.commands.review import ReviewCommandMixin  # noqa: E402


class _FakeMessageObj:
    def __init__(self, timestamp: float | None = 1234567890.0) -> None:
        self.timestamp = timestamp


class _FakeKV(KVStore):
    """内存版 KV 存储（用于测试持久化行为）。"""

    def __init__(self) -> None:
        super().__init__(self._get, self._put)
        self.data: dict = {}

    async def _get(self, key: str, default=None):
        return self.data.get(key, default)

    async def _put(self, key: str, value) -> None:
        self.data[key] = value


class _FakeEvent:
    def __init__(self, timestamp: float | None = 1234567890.0) -> None:
        self.message_obj = _FakeMessageObj(timestamp)

    def get_sender_name(self) -> str:
        return "测试用户"

    def get_sender_id(self) -> str:
        return "10001"

    def get_message_outline(self) -> str:
        return "测试消息"


class BooleanParsingTest(unittest.TestCase):
    def test_string_false_is_false(self) -> None:
        result = ReviewResult.from_dict(
            {"illegal": "false", "risk": 95, "type": "x", "reason": "r"}
        )
        self.assertFalse(result.illegal)

    def test_string_true_is_true(self) -> None:
        result = ReviewResult.from_dict(
            {"illegal": "true", "risk": 95, "type": "x", "reason": "r"}
        )
        self.assertTrue(result.illegal)

    def test_int_flags(self) -> None:
        self.assertTrue(ReviewResult.from_dict({"illegal": 1}).illegal)
        self.assertFalse(ReviewResult.from_dict({"illegal": 0}).illegal)


class JsonParsingTest(unittest.TestCase):
    _VALID = (
        '{"illegal": true, "risk": 95, "type": "刷屏", '
        '"reason": "r", "evidence": ["e1"], "suggestion": "mute"}'
    )

    def test_plain_json(self) -> None:
        result = parse_review_result(self._VALID)
        self.assertEqual(result.risk, 95)
        self.assertEqual(result.suggestion, "mute")

    def test_markdown_fence(self) -> None:
        text = f"```json\n{self._VALID}\n```"
        self.assertEqual(parse_review_result(text).risk, 95)

    def test_prose_with_braces_before_and_after(self) -> None:
        text = (
            "审核结果说明：{左括号} 表示开始。\n"
            f"结论：{self._VALID}\n"
            "后续说明：{右括号} 表示结束。"
        )
        result = parse_review_result(text)
        self.assertEqual(result.risk, 95)

    def test_invalid_text_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_review_result("没有任何 JSON 内容")

    def test_foreign_json_object_skipped(self) -> None:
        """散文中的合法但非审核结果的 JSON 对象应被跳过。"""
        text = (
            '审核说明：{"note": "接上一条"}。\n'
            f"结论：{self._VALID}"
        )
        result = parse_review_result(text)
        self.assertEqual(result.risk, 95)

    def test_empty_braces_raises(self) -> None:
        """仅含空对象 {} 的回复应解析失败而非误判为不违规。"""
        with self.assertRaises(ValueError):
            parse_review_result("说明：{} 表示空对象，本次无结论。")

    def test_unknown_suggestion_falls_back_to_warn(self) -> None:
        text = self._VALID.replace('"mute"', '"delete_hard"')
        self.assertEqual(parse_review_result(text).suggestion, "warn")


class ToRecordTest(unittest.TestCase):
    def test_uses_message_obj_timestamp(self) -> None:
        record = to_record(_FakeEvent(1700000000.0), "g1")
        self.assertEqual(record.timestamp, 1700000000.0)
        self.assertEqual(record.group_id, "g1")

    def test_falls_back_to_now(self) -> None:
        before = time.time()
        record = to_record(_FakeEvent(None), "g1")
        self.assertGreaterEqual(record.timestamp, before)


class TrimRecordsTest(unittest.TestCase):
    def test_trim_respects_budget(self) -> None:
        records = [
            ChatRecord(timestamp=1.0, nickname="a", user_id="1", content="hello", group_id="g"),
            ChatRecord(timestamp=2.0, nickname="b", user_id="2", content="world", group_id="g"),
        ]
        trimmed = trim_records(records, max_chars=10, max_msg_chars=100)
        self.assertGreater(len(trimmed), 0)
        self.assertLessEqual(len(trimmed), len(records))


class PunisherHotReloadTest(unittest.TestCase):
    def test_config_changes_apply(self) -> None:
        cfg = {
            "enable_blacklist": False,
            "mute_duration": 600,
            "punish_pipeline": {},
        }
        punisher = Punisher(None, None, lambda gid="": cfg)
        self.assertEqual(punisher._stages["mute"]._duration, 600)
        self.assertFalse(punisher._blacklist_enabled)

        cfg["mute_duration"] = 1200
        cfg["enable_blacklist"] = True
        cfg["punish_pipeline"] = {"mute": ["mute"]}
        punisher._sync_config()

        self.assertEqual(punisher._stages["mute"]._duration, 1200)
        self.assertTrue(punisher._blacklist_enabled)
        self.assertEqual(punisher._pipelines["mute"], ["mute"])


class QueueTest(unittest.TestCase):
    def test_pending_count_cleans_expired(self) -> None:
        async def scenario() -> int:
            queue = ReviewQueue()
            task = ReviewTask.create(
                group_id="g",
                user_id="u",
                nickname="n",
                result=ReviewResult.from_dict(
                    {"illegal": True, "risk": 90, "type": "t", "reason": "r"}
                ),
                context=[],
                timeout=0.001,
            )
            await queue.add(task)
            await asyncio.sleep(0.01)
            return await queue.pending_count()

        self.assertEqual(asyncio.run(scenario()), 0)


class TaskIdTest(unittest.TestCase):
    def test_task_id_length(self) -> None:
        task = ReviewTask.create(
            group_id="g",
            user_id="u",
            nickname="n",
            result=ReviewResult.from_dict(
                {"illegal": True, "risk": 90, "type": "t", "reason": "r"}
            ),
            context=[],
            timeout=300,
        )
        self.assertEqual(len(task.task_id), 12)


class _StubLLM:
    """记录调用次数并返回固定审核结果的 LLM 桩。"""

    def __init__(self) -> None:
        self.calls = 0
        self.last_provider_id = "stub-model"

    async def chat(self, system: str, user: str, output: str, umo: str) -> str:
        self.calls += 1
        return (
            '{"illegal": true, "risk": 95, "type": "测试", '
            '"reason": "r", "evidence": ["e"], "suggestion": "mute"}'
        )


class _StubGroupEvent:
    """满足 workflow.on_message 最小接口的群消息事件桩。"""

    def __init__(self, sender_id: str = "10001", content: str = "测试消息") -> None:
        self.message_obj = _FakeMessageObj(1700000000.0)
        self.unified_msg_origin = "aiocqhttp:GroupMessage:g1"
        self.role = "member"
        self._sender_id = sender_id
        self._content = content

    def get_group_id(self) -> str:
        return "g1"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return "测试用户"

    def get_message_outline(self) -> str:
        return self._content

    def get_self_id(self) -> str:
        return "bot"

    def is_admin(self) -> bool:
        return False

    def get_platform_id(self) -> str:
        return "aiocqhttp"


class PassiveReviewToggleTest(unittest.TestCase):
    def test_default_enabled(self) -> None:
        cfg = ConfigManager({})
        self.assertTrue(cfg.get("enable_passive_review", True))

    def test_toggle_via_set_value(self) -> None:
        cfg = ConfigManager({})
        ok, _ = asyncio.run(cfg.set_value("enable_passive_review", "false"))
        self.assertTrue(ok)
        self.assertFalse(cfg.get("enable_passive_review", True))

        ok, _ = asyncio.run(cfg.set_value("enable_passive_review", "true"))
        self.assertTrue(ok)
        self.assertTrue(cfg.get("enable_passive_review", False))


class PassiveReviewWorkflowTest(unittest.TestCase):
    @staticmethod
    def _make_cfg(enabled: bool) -> dict:
        return {
            "enable_passive_review": enabled,
            "enable_history": True,
            "review_mode": "both",
            "risk_threshold": 80,
            "history_count": 50,
            "whitelist": [],
            "min_msg_len": 2,
            "cooldown": 300,
            "review_timeout": 300,
            "max_chat_chars": 3000,
            "max_msg_chars": 200,
            "max_pending_per_user": 2,
            "max_pending_total": 200,
        }

    @classmethod
    def _make_workflow(cls, cfg: dict, llm: _StubLLM):
        history = HistoryCache(lambda gid="": cfg)
        prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": cfg,
        )
        queue = ReviewQueue()
        workflow = ReviewWorkflow(
            history,
            prompt,
            llm,
            queue,
            lambda gid="": cfg,
        )
        return workflow, history, queue

    def test_disabled_skips_review_but_caches(self) -> None:
        llm = _StubLLM()
        cfg = self._make_cfg(False)
        workflow, history, _ = self._make_workflow(cfg, llm)

        asyncio.run(workflow.on_message(_StubGroupEvent()))

        self.assertEqual(llm.calls, 0)
        self.assertEqual(len(history.get_recent("g1")), 1)

    def test_enabled_triggers_review(self) -> None:
        llm = _StubLLM()
        cfg = self._make_cfg(True)
        workflow, _, queue = self._make_workflow(cfg, llm)

        async def scenario() -> int:
            await workflow.on_message(_StubGroupEvent())
            return await queue.pending_count()

        count = asyncio.run(scenario())
        self.assertEqual(llm.calls, 1)
        self.assertEqual(count, 1)

    def test_task_records_provider(self) -> None:
        llm = _StubLLM()
        llm.last_provider_id = "auto_DeepSeek"
        cfg = self._make_cfg(True)
        workflow, _, queue = self._make_workflow(cfg, llm)

        async def scenario() -> list:
            await workflow.on_message(_StubGroupEvent())
            return await queue.list_pending("g1")

        tasks = asyncio.run(scenario())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].llm_provider, "auto_DeepSeek")


def _make_task(group_id: str = "g1", user_id: str = "u1") -> ReviewTask:
    return ReviewTask.create(
        group_id=group_id,
        user_id=user_id,
        nickname="n",
        result=ReviewResult.from_dict(
            {"illegal": True, "risk": 90, "type": "t", "reason": "r"}
        ),
        context=[],
        timeout=300,
    )


class SerializationTest(unittest.TestCase):
    def test_task_roundtrip(self) -> None:
        task = ReviewTask.create(
            group_id="g1",
            user_id="u1",
            nickname="nick",
            result=ReviewResult.from_dict(
                {
                    "illegal": True,
                    "risk": 92,
                    "type": "辱骂",
                    "reason": "r",
                    "evidence": ["e"],
                    "suggestion": "mute",
                }
            ),
            context=[
                ChatRecord(1.0, "nick", "u1", "hello", "g1"),
            ],
            timeout=300,
            platform_id="aiocqhttp",
            session_id="session",
            llm_provider="auto_DeepSeek",
        )
        restored = ReviewTask.from_dict(task.to_dict())
        self.assertEqual(restored.task_id, task.task_id)
        self.assertEqual(restored.status, task.status)
        self.assertEqual(restored.result.risk, 92)
        self.assertEqual(restored.result.suggestion, "mute")
        self.assertEqual(len(restored.context), 1)
        self.assertEqual(restored.context[0].content, "hello")
        self.assertEqual(restored.session_id, "session")
        self.assertEqual(restored.llm_provider, "auto_DeepSeek")


class QueuePersistenceTest(unittest.TestCase):
    def test_save_and_restore(self) -> None:
        async def scenario():
            store = _FakeKV()
            queue1 = ReviewQueue(store=store)
            task = _make_task()
            await queue1.add(task)
            queue2 = ReviewQueue(store=store)
            await queue2.load()
            return await queue2.get(task.task_id), task.task_id

        restored, task_id = asyncio.run(scenario())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.task_id, task_id)
        self.assertEqual(restored.user_id, "u1")
        self.assertEqual(restored.result.risk, 90)


class QueueGovernanceTest(unittest.TestCase):
    def test_per_user_and_total_limits(self) -> None:
        async def scenario():
            cfg = {"max_pending_per_user": 1, "max_pending_total": 2}
            queue = ReviewQueue(get_config=lambda gid="": cfg)
            results = []
            for user_id in ("u1", "u1", "u2", "u3"):
                results.append(await queue.add(_make_task(user_id=user_id)))
            return results

        added = asyncio.run(scenario())
        self.assertEqual(added, [True, False, True, False])

    def test_total_limit_counts_pending_only(self) -> None:
        """已处理任务不应占用待处理总量上限。"""
        async def scenario():
            cfg = {"max_pending_per_user": 10, "max_pending_total": 2}
            queue = ReviewQueue(get_config=lambda gid="": cfg)
            tasks = [_make_task(user_id=f"u{i}") for i in range(4)]
            for task in tasks:
                await queue.add(task)
            await queue.approve(tasks[0].task_id, "admin")
            await queue.reject(tasks[1].task_id, "admin")
            return [await queue.add(_make_task(user_id="u9"))]

        self.assertEqual(asyncio.run(scenario()), [True])


class QueueRetentionTest(unittest.TestCase):
    def test_decided_tasks_trimmed_over_retention(self) -> None:
        """已处理任务超过保留上限后被清理，最旧的优先。"""
        async def scenario():
            queue = ReviewQueue()
            oldest_ids = []
            for i in range(205):
                task = _make_task(group_id="g1", user_id=f"u{i}")
                if i < 5:
                    task.created_at -= 1000.0  # 标记为最旧
                    oldest_ids.append(task.task_id)
                await queue.add(task)
            for task in await queue.list_all():
                await queue.approve(task.task_id, "admin")
            await queue.pending_count()  # 触发保留清理
            remaining = await queue.list_all()
            remaining_ids = {task.task_id for task in remaining}
            return len(remaining), oldest_ids, remaining_ids

        total, oldest_ids, remaining_ids = asyncio.run(scenario())
        self.assertEqual(total, 200)
        self.assertFalse(any(tid in remaining_ids for tid in oldest_ids))


class ConfigValidationTest(unittest.TestCase):
    def test_risk_threshold_range(self) -> None:
        cfg = ConfigManager({})
        ok, _ = asyncio.run(cfg.set_value("risk_threshold", "200"))
        self.assertFalse(ok)
        ok, _ = asyncio.run(cfg.set_value("risk_threshold", "70"))
        self.assertTrue(ok)
        self.assertEqual(cfg.get("risk_threshold"), 70)

    def test_temperature_range(self) -> None:
        cfg = ConfigManager({})
        ok, _ = asyncio.run(cfg.set_value("llm_temperature", "3"))
        self.assertFalse(ok)
        ok, _ = asyncio.run(cfg.set_value("llm_temperature", "0.5"))
        self.assertTrue(ok)
        self.assertEqual(cfg.get("llm_temperature"), 0.5)

    def test_push_target_enum(self) -> None:
        cfg = ConfigManager({})
        ok, _ = asyncio.run(cfg.set_value("regex_push_target", "invalid"))
        self.assertFalse(ok)
        ok, _ = asyncio.run(cfg.set_value("regex_push_target", "admin"))
        self.assertTrue(ok)
        self.assertEqual(cfg.get("regex_push_target"), "admin")

    def test_push_target_group_override(self) -> None:
        cfg = ConfigManager({})
        store = _FakeKV()
        ok, _ = asyncio.run(
            cfg.set_override(store, "g1", "regex_push_target", "off")
        )
        self.assertTrue(ok)
        self.assertEqual(cfg.effective("g1")["regex_push_target"], "off")
        self.assertEqual(cfg.effective("g2")["regex_push_target"], "group")


class SafeIntTest(unittest.TestCase):
    def test_safe_int_fallback(self) -> None:
        from _plugin_under_test.config import safe_int

        self.assertEqual(safe_int("abc", 5), 5)
        self.assertEqual(safe_int(None, 5), 5)
        self.assertEqual(safe_int("12", 5), 12)
        self.assertEqual(safe_int(3.9, 5), 3)

    def test_history_cache_tolerates_bad_config(self) -> None:
        """面板脏配置（非数值 history_count）不应击穿缓存热路径。"""
        history = HistoryCache(
            lambda gid="": {"enable_history": True, "history_count": "oops"}
        )
        history.add(ChatRecord(1.0, "a", "1", "hi", "g1"))
        self.assertEqual(len(history.get_recent("g1")), 1)

    def test_llm_client_tolerates_bad_config(self) -> None:
        provider = _StubProvider()
        cfg = {"llm_temperature": 0.5, "llm_max_concurrency": "oops"}
        client = LLMClient(_StubContext(provider), lambda gid="": cfg)
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 1)


class GroupOverrideTest(unittest.TestCase):
    def test_override_effective_and_clear(self) -> None:
        cfg = ConfigManager({})
        store = _FakeKV()
        asyncio.run(cfg.load_overrides(store))
        ok, _ = asyncio.run(
            cfg.set_override(store, "g1", "risk_threshold", "70")
        )
        self.assertTrue(ok)
        self.assertEqual(cfg.effective("g1")["risk_threshold"], 70)
        self.assertEqual(cfg.effective("g2")["risk_threshold"], 80)
        ok, _ = asyncio.run(
            cfg.clear_override(store, "g1", "risk_threshold")
        )
        self.assertTrue(ok)
        self.assertEqual(cfg.effective("g1")["risk_threshold"], 80)

    def test_overrides_persist_across_instances(self) -> None:
        store = _FakeKV()
        cfg1 = ConfigManager({})
        asyncio.run(cfg1.set_override(store, "g1", "cooldown", "120"))
        cfg2 = ConfigManager({})
        asyncio.run(cfg2.load_overrides(store))
        self.assertEqual(cfg2.effective("g1")["cooldown"], 120)


class StatsStoreTest(unittest.TestCase):
    def test_record_and_summary(self) -> None:
        async def scenario() -> StatsStore:
            store = _FakeKV()
            stats = StatsStore(store)
            await stats.load()
            await stats.record_violation("g1", "u1", "辱骂")
            await stats.record_violation("g1", "u1", "辱骂")
            await stats.record_violation("g1", "u2", "广告")
            await stats.record_decision("g1", "u1", True, "mute")
            await stats.record_decision("g1", "u1", False)
            return stats

        stats = asyncio.run(scenario())
        rows = stats.group_summary("g1")
        self.assertEqual(rows[0]["user_id"], "u1")
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["approved"], 1)
        self.assertEqual(rows[0]["rejected"], 1)
        self.assertEqual(rows[0]["types"], {"辱骂": 2})


class _Resp:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class _StubProvider:
    def __init__(
        self,
        failures: int = 0,
        temperature_error: bool = False,
        provider_id: str = "stub",
        model: str = "stub-model",
    ) -> None:
        self.calls = 0
        self.failures = failures
        self.temperature_error = temperature_error
        self.last_kwargs: dict = {}
        self._provider_id = provider_id
        self._model = model

    def meta(self):
        return types.SimpleNamespace(id=self._provider_id, model=self._model)

    async def text_chat(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.temperature_error and self.calls == 1:
            raise TypeError(
                "text_chat() got an unexpected keyword argument 'temperature'"
            )
        if self.calls <= self.failures:
            raise RuntimeError("network error")
        return _Resp("ok")


class _StubContext:
    def __init__(
        self,
        provider: _StubProvider,
        providers: list | None = None,
    ) -> None:
        self.provider = provider
        self._providers = providers if providers is not None else [provider]

    def get_using_provider(self, umo: str):
        return self.provider

    def get_all_providers(self):
        return list(self._providers)

    def get_provider_by_id(self, provider_id: str):
        for prov in self._providers:
            if prov.meta().id == provider_id:
                return prov
        return None


class LLMClientRetryTest(unittest.TestCase):
    @staticmethod
    def _make_client(provider: _StubProvider, notifier=None) -> LLMClient:
        cfg = {"llm_temperature": 0.5, "llm_max_concurrency": 3}
        context = _StubContext(provider)
        return LLMClient(
            context,
            lambda gid="": cfg,
            notifier=notifier,
            retry_times=2,
            retry_delays=(0.0, 0.0),
        )

    def test_retries_then_succeeds(self) -> None:
        provider = _StubProvider(failures=2)
        client = self._make_client(provider)
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 3)

    def test_notifies_once_after_final_failure(self) -> None:
        provider = _StubProvider(failures=99)
        notified: list[str] = []

        async def notify(message: str) -> None:
            notified.append(message)

        client = self._make_client(provider, notifier=notify)
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertIsNone(text)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(len(notified), 1)

    def test_temperature_passed_through(self) -> None:
        provider = _StubProvider()
        client = self._make_client(provider)
        asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(provider.last_kwargs.get("temperature"), 0.5)

    def test_temperature_fallback_when_unsupported(self) -> None:
        provider = _StubProvider(temperature_error=True)
        client = self._make_client(provider)
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 2)
        self.assertNotIn("temperature", provider.last_kwargs)


class LLMProviderSelectionTest(unittest.TestCase):
    @staticmethod
    def _client(
        context: _StubContext,
        cfg: dict,
    ) -> LLMClient:
        return LLMClient(
            context,
            lambda gid="": cfg,
            retry_times=0,
            retry_delays=(0.0,),
        )

    def test_uses_configured_provider_id(self) -> None:
        session = _StubProvider(provider_id="session-model")
        pinned = _StubProvider(provider_id="pinned-model")
        context = _StubContext(session, providers=[session, pinned])
        client = self._client(context, {"llm_provider_id": "pinned-model"})
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(pinned.calls, 1)
        self.assertEqual(session.calls, 0)

    def test_falls_back_when_provider_id_unknown(self) -> None:
        session = _StubProvider(provider_id="session-model")
        context = _StubContext(session, providers=[session])
        client = self._client(context, {"llm_provider_id": "ghost"})
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(session.calls, 1)

    def test_empty_provider_id_uses_session_model(self) -> None:
        session = _StubProvider(provider_id="session-model")
        context = _StubContext(session, providers=[session])
        client = self._client(context, {"llm_provider_id": ""})
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(session.calls, 1)

    def test_context_without_provider_lookup_falls_back(self) -> None:
        """旧版/精简 Context 缺少 get_provider_by_id 时仍回退会话默认。"""
        session = _StubProvider(provider_id="session-model")

        class _MinimalContext:
            def get_using_provider(self, umo: str):
                return session

        client = LLMClient(
            _MinimalContext(),
            lambda gid="": {"llm_provider_id": "pinned"},
            retry_times=0,
            retry_delays=(0.0,),
        )
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(session.calls, 1)


class PromptContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = ConfigManager({})
        self.prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": self.cfg.raw,
        )

    def test_system_contains_guardrails(self) -> None:
        system = self.prompt.build_system()
        self.assertIn("数据边界", system)
        self.assertIn("宁缺毋滥", system)
        self.assertIn("80", system)
        self.assertNotIn("{threshold}", system)

    def test_user_target_semantics(self) -> None:
        user = self.prompt.build_user([], "")
        self.assertIn("仅为该用户", user)

    def test_output_has_examples(self) -> None:
        output = self.prompt.build_output()
        self.assertIn("未违规时示例", output)
        self.assertIn("宁轻勿重", output)

    def test_rule_template_rendered(self) -> None:
        task = _make_task()
        rule_prompt = self.prompt.build_rule(task)
        self.assertIn("正则表达式", rule_prompt)
        self.assertIn("违规类型：t", rule_prompt)
        self.assertNotIn("{type}", rule_prompt)


_RULE_CFG = {
    "regex_min_hits": 3,
    "regex_min_accuracy": 0.7,
    "regex_max_rules": 200,
    "enable_regex_prefilter": True,
    "regex_sediment": True,
}


class RuleEngineTest(unittest.TestCase):
    @staticmethod
    def _make_engine(store: _FakeKV | None = None, notifier=None) -> RuleEngine:
        store = store or _FakeKV()
        return RuleEngine(store, lambda gid="": _RULE_CFG, notifier=notifier)

    def test_add_rejects_invalid_pattern(self) -> None:
        rules = self._make_engine()
        ok, msg, _ = asyncio.run(rules.add("([a-z", source="manual"))
        self.assertFalse(ok)
        self.assertIn("非法", msg)

    def test_manual_rule_active_and_match(self) -> None:
        rules = self._make_engine()
        asyncio.run(rules.add("广告|加我微信", source="manual", note="广告"))
        self.assertEqual(len(rules.match("快来加我微信")), 1)
        self.assertEqual(rules.match("正常聊天内容"), [])

    def test_duplicate_rejected(self) -> None:
        rules = self._make_engine()
        asyncio.run(rules.add("广告", source="manual"))
        ok, msg, _ = asyncio.run(rules.add("广告", source="manual"))
        self.assertFalse(ok)

    def test_observe_promotes_good_rule(self) -> None:
        rules = self._make_engine()
        asyncio.run(rules.add("广告", source="auto", note="广告"))
        rule = rules.list()[0]
        self.assertEqual(rule.status.value, "observing")
        for _ in range(3):  # 3 次判定一致 -> 激活
            asyncio.run(rules.record_observation(rule.rule_id, True))
        self.assertEqual(rules.list()[0].status.value, "active")

    def test_observe_deletes_bad_rule(self) -> None:
        rules = self._make_engine()
        asyncio.run(rules.add("广告", source="auto"))
        rule = rules.list()[0]
        for _ in range(2):  # 2 次一致 + 1 次不一致 -> 准确率不足删除
            asyncio.run(rules.record_observation(rule.rule_id, True))
        asyncio.run(rules.record_observation(rule.rule_id, False))
        self.assertEqual(rules.list(), [])

    def test_circuit_breaker_disables_rule(self) -> None:
        notified: list[str] = []

        async def notify(message: str) -> None:
            notified.append(message)

        rules = self._make_engine(notifier=notify)
        asyncio.run(rules.add("广告", source="manual"))
        rule = rules.list()[0]
        # 3 次判定中 2 次被拒绝 -> 准确率 33% < 70% -> 熔断
        asyncio.run(rules.record_decision(rule.rule_id, False))
        asyncio.run(rules.record_decision(rule.rule_id, False))
        asyncio.run(rules.record_decision(rule.rule_id, True))
        self.assertEqual(rules.list()[0].status.value, "disabled")
        self.assertEqual(len(notified), 1)

    def test_persistence_roundtrip(self) -> None:
        store = _FakeKV()
        rules1 = self._make_engine(store)
        asyncio.run(rules1.add("广告", source="auto", note="广告"))
        rule_id = rules1.list()[0].rule_id
        rules2 = self._make_engine(store)
        asyncio.run(rules2.load())
        self.assertEqual(rules2.list()[0].rule_id, rule_id)
        self.assertEqual(rules2.list()[0].status.value, "observing")

    def test_max_rules_limit(self) -> None:
        rules = RuleEngine(
            _FakeKV(),
            lambda gid="": {**_RULE_CFG, "regex_max_rules": 1},
        )
        asyncio.run(rules.add("广告", source="manual"))
        ok, msg, _ = asyncio.run(rules.add("赌博", source="manual"))
        self.assertFalse(ok)
        self.assertIn("上限", msg)


class RuleCandidateTest(unittest.TestCase):
    @staticmethod
    def _cfg(**extra) -> dict:
        cfg = dict(_RULE_CFG)
        cfg.update({"regex_candidate_ttl": 1, "regex_push_interval": 30})
        cfg.update(extra)
        return cfg

    @staticmethod
    def _collect_one(rules: RuleEngine) -> str:
        class _RuleLLM:
            async def chat(self, system, user, output, umo) -> str:
                return '{"pattern": "加我微信|广告", "note": "广告", "level": 2}'

        prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": _RULE_CFG,
        )
        task = _make_task()
        task.result = ReviewResult.from_dict(
            {
                "illegal": True,
                "risk": 92,
                "type": "广告",
                "reason": "r",
                "evidence": ["加我微信"],
            }
        )
        asyncio.run(rules.collect_candidate(_RuleLLM(), prompt, task))
        return rules.candidates()[0].candidate_id

    def test_approve_moves_to_observing(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": self._cfg())
        candidate_id = self._collect_one(rules)
        ok, message = asyncio.run(rules.approve_candidate(candidate_id))
        self.assertTrue(ok, message)
        self.assertEqual(rules.candidates(), [])
        records = rules.list()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "auto")
        self.assertEqual(records[0].status.value, "observing")

    def test_deny_discards_candidate(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": self._cfg())
        candidate_id = self._collect_one(rules)
        self.assertTrue(asyncio.run(rules.deny_candidate(candidate_id)))
        self.assertEqual(rules.candidates(), [])
        self.assertEqual(rules.list(), [])

    def test_duplicate_candidate_skipped(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": self._cfg())
        self._collect_one(rules)
        self._collect_one(rules)  # 相同 pattern 第二次被跳过
        self.assertEqual(len(rules.candidates()), 1)

    def test_purge_expired_candidates(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": self._cfg())
        candidate_id = self._collect_one(rules)
        rules._candidates[candidate_id].created_at -= 2 * 86400  # 已过期 2 天
        removed = asyncio.run(rules.purge_expired_candidates())
        self.assertEqual(removed, 1)
        self.assertEqual(rules.candidates(), [])

    def test_candidates_persist_across_instances(self) -> None:
        store = _FakeKV()
        rules1 = RuleEngine(store, lambda gid="": self._cfg())
        self._collect_one(rules1)
        rules2 = RuleEngine(store, lambda gid="": self._cfg())
        asyncio.run(rules2.load())
        self.assertEqual(len(rules2.candidates()), 1)

    def test_corrupt_kv_does_not_break_load(self) -> None:
        """脏 KV 数据（非法字段类型）不应导致 load 失败。"""
        store = _FakeKV()
        store.data["review_rules"] = {
            "bad": {"rule_id": "bad", "pattern": "x", "level": "not-a-number"}
        }
        rules = RuleEngine(store, lambda gid="": self._cfg())
        asyncio.run(rules.load())  # 不抛异常
        self.assertEqual(rules.list(), [])


class RuleWorkflowTest(unittest.TestCase):
    @classmethod
    def _make_cfg(cls) -> dict:
        cfg = {
            "enable_passive_review": True,
            "enable_history": True,
            "review_mode": "both",
            "risk_threshold": 80,
            "history_count": 50,
            "whitelist": [],
            "min_msg_len": 2,
            "cooldown": 300,
            "review_timeout": 300,
            "max_chat_chars": 3000,
            "max_msg_chars": 200,
            "max_pending_per_user": 2,
            "max_pending_total": 200,
        }
        cfg.update(_RULE_CFG)
        return cfg

    def test_active_rule_skips_llm(self) -> None:
        llm = _StubLLM()
        cfg = self._make_cfg()
        rules = RuleEngine(_FakeKV(), lambda gid="": cfg)
        asyncio.run(rules.add("广告", source="manual", note="广告"))
        history = HistoryCache(lambda gid="": cfg)
        prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": cfg,
        )
        queue = ReviewQueue()
        workflow = ReviewWorkflow(
            history,
            prompt,
            llm,
            queue,
            lambda gid="": cfg,
            rules=rules,
        )

        async def scenario():
            await workflow.on_message(
                _StubGroupEvent(content="快来加我微信，广告一波")
            )
            tasks = await queue.list_pending("g1")
            return tasks

        tasks = asyncio.run(scenario())
        self.assertEqual(llm.calls, 0)  # 规则命中跳过 LLM
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].rule_id)

    def test_no_hit_calls_llm(self) -> None:
        llm = _StubLLM()
        cfg = self._make_cfg()
        rules = RuleEngine(_FakeKV(), lambda gid="": cfg)
        asyncio.run(rules.add("广告", source="manual", note="广告"))
        history = HistoryCache(lambda gid="": cfg)
        prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": cfg,
        )
        queue = ReviewQueue()
        workflow = ReviewWorkflow(
            history,
            prompt,
            llm,
            queue,
            lambda gid="": cfg,
            rules=rules,
        )
        asyncio.run(workflow.on_message(_StubGroupEvent(content="正常聊天")))
        self.assertEqual(llm.calls, 1)  # 未命中走 LLM

    def test_collect_candidate_creates_pending(self) -> None:
        class _RuleLLM:
            async def chat(self, system, user, output, umo) -> str:
                return (
                    '{"pattern": "加我微信|广告", "note": "广告引流", "level": 2}'
                )

        cfg = self._make_cfg()
        rules = RuleEngine(_FakeKV(), lambda gid="": cfg)
        prompt = PromptManager(
            str(Path(__file__).resolve().parent.parent),
            lambda gid="": cfg,
        )
        task = _make_task()
        task.result = ReviewResult.from_dict(
            {
                "illegal": True,
                "risk": 92,
                "type": "广告",
                "reason": "r",
                "evidence": ["加我微信"],
            }
        )
        asyncio.run(rules.collect_candidate(_RuleLLM(), prompt, task))
        candidates = rules.candidates()
        self.assertEqual(len(candidates), 1)  # 进入候选池而非直接成为规则
        self.assertTrue(candidates[0].source_task_id)
        self.assertEqual(rules.list(), [])  # 规则库仍为空
        message = rules.build_push_message()
        self.assertIn("待确认", message)
        self.assertIn("approve", message)


class ReviewContextTest(unittest.TestCase):
    """日志上下文追踪：审核流程内日志应带 request_id/群/用户 前缀。"""

    def test_context_prefix_rendered(self) -> None:
        captured: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())

        logger = get_logger()
        logger.setLevel(logging.DEBUG)
        logger.logger.addHandler(handler)
        try:
            with review_context(group_id="g1", user_id="u1", task_id="t1"):
                logger.info("审核开始")
        finally:
            logger.logger.removeHandler(handler)

        self.assertEqual(len(captured), 1)
        self.assertIn("[#", captured[0])
        self.assertIn("群=g1", captured[0])
        self.assertIn("用户=u1", captured[0])
        self.assertIn("任务=t1", captured[0])
        self.assertIn("审核开始", captured[0])

    def test_no_context_no_prefix(self) -> None:
        captured: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())

        logger = get_logger()
        logger.setLevel(logging.DEBUG)
        logger.logger.addHandler(handler)
        try:
            logger.info("无上下文")
        finally:
            logger.logger.removeHandler(handler)

        self.assertEqual(captured, ["无上下文"])

    def test_context_reset_after_exit(self) -> None:
        captured: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())

        logger = get_logger()
        logger.setLevel(logging.DEBUG)
        logger.logger.addHandler(handler)
        try:
            with review_context(group_id="g1", user_id="u1"):
                pass
            logger.info("上下文已退出")
        finally:
            logger.logger.removeHandler(handler)

        self.assertEqual(captured, ["上下文已退出"])

    def test_astrbot_logqueue_handler_format_ok(self) -> None:
        """回归：AstrBot LogQueueHandler 的 Formatter 要求 plugin_tag 等字段，
        缺失会导致插件加载崩溃（KeyError/ValueError）。"""
        captured: list[str] = []
        formatter = logging.Formatter(
            "%(ansi_prefix)s[%(asctime)s.%(msecs)03d] %(plugin_tag)s "
            "[%(short_levelname)s]%(astrbot_version_tag)s "
            "[%(source_file)s:%(source_line)d]: %(message)s%(ansi_reset)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        class _AnsiFilter(logging.Filter):
            """模拟 AstrBot _QueueAnsiColorFilter（挂在 handler 上）。"""

            def filter(self, record: logging.LogRecord) -> bool:
                record.ansi_prefix = "\u001b[0m"
                record.ansi_reset = "\u001b[0m"
                return True

        handler = logging.Handler()
        handler.addFilter(_AnsiFilter())
        handler.setFormatter(formatter)
        handler.emit = lambda record: captured.append(formatter.format(record))

        logger = get_logger()
        logger.setLevel(logging.DEBUG)
        logger.logger.addHandler(handler)
        try:
            with review_context(group_id="g1", user_id="u1"):
                logger.info("审核日志")
        finally:
            logger.logger.removeHandler(handler)

        self.assertEqual(len(captured), 1)
        self.assertIn("g1", captured[0])
        self.assertIn("审核日志", captured[0])


class OutputFormatTest(unittest.TestCase):
    """命令输出格式：紧凑内联审批命令、群号前缀、detail 概要。"""

    @staticmethod
    def _make_candidates() -> list:
        from _plugin_under_test.models import RuleCandidate

        return [
            RuleCandidate(
                candidate_id="cand0000001",
                pattern="加我微信|广告",
                note="广告引流",
                level=2,
                group_id="g1",
            ),
            RuleCandidate(
                candidate_id="cand0000002",
                pattern="你个傻",
                note="辱骂",
                level=2,
                group_id="g1",
            ),
        ]

    def test_candidate_item_has_approve_and_deny(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": _RULE_CFG)
        candidate = self._make_candidates()[0]
        text = rules.format_candidate_item(candidate)
        self.assertIn("cand0000001", text)
        self.assertIn("✅ 批准：/review rule approve cand0000001", text)
        self.assertIn("❌ 拒绝：/review rule deny cand0000001", text)

    def test_push_message_has_group_prefix(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": _RULE_CFG)
        message = rules.build_push_message(self._make_candidates(), "123456789")
        self.assertIn("[群 123456789]", message)
        self.assertIn("待确认", message)

    def test_candidates_inline_compact(self) -> None:
        rules = RuleEngine(_FakeKV(), lambda gid="": _RULE_CFG)
        rules._candidates = {
            candidate.candidate_id: candidate
            for candidate in self._make_candidates()
        }
        text = ReviewCommandMixin._format_candidates(rules)
        self.assertIn("✅ /review rule approve cand0000001", text)
        self.assertIn("❌ /review rule deny cand0000001", text)
        self.assertIn("✅ /review rule approve cand0000002", text)

    def test_detail_summary_has_approve_and_reject(self) -> None:
        task = _make_task()
        text = ReviewCommandMixin._format_detail_summary(task)
        self.assertIn(f"✅ 同意：/review pass {task.task_id}", text)
        self.assertIn(f"❌ 不同意：/review reject {task.task_id}", text)
        self.assertIn("建议处罚", text)

    def test_forward_threshold_config(self) -> None:
        cfg = ConfigManager({})
        ok, _ = asyncio.run(cfg.set_value("regex_forward_threshold", "51"))
        self.assertFalse(ok)
        ok, _ = asyncio.run(cfg.set_value("regex_forward_threshold", "5"))
        self.assertTrue(ok)
        self.assertEqual(cfg.get("regex_forward_threshold"), 5)
        ok, _ = asyncio.run(cfg.set_value("regex_forward_threshold", "0"))
        self.assertTrue(ok)  # 0 = 始终文本


class _AdminStubEvent:
    """满足 _check_review_permission 最小接口的事件桩。"""

    def __init__(
        self,
        is_admin: bool = False,
        sender: str = "10001",
        group: str = "g1",
    ) -> None:
        self._is_admin = is_admin
        self._sender = sender
        self._group = group

    def is_admin(self) -> bool:
        return self._is_admin

    def get_group_id(self) -> str:
        return self._group

    def get_sender_id(self) -> str:
        return self._sender

    def get_platform_id(self) -> str:
        return "aiocqhttp"


class _FakeAdminExecutor:
    """模拟 PlatformExecutor.get_group_admins 并记录调用。"""

    def __init__(self, admins: list[str]) -> None:
        self.admins = set(admins)
        self.calls: list[str] = []

    async def get_group_admins(self, platform_id: str, group_id: str) -> list[str]:
        self.calls.append(group_id)
        return list(self.admins)


class ReviewPermissionTest(unittest.TestCase):
    """/review 命令按发送者 QQ 鉴权：本群群主/群管或 AstrBot 管理员。"""

    @staticmethod
    def _make_mixin(executor: _FakeAdminExecutor) -> ReviewCommandMixin:
        mixin = object.__new__(ReviewCommandMixin)
        mixin.executor = executor
        mixin._group_admin_cache = {}
        return mixin

    def test_astrbot_admin_always_allowed(self) -> None:
        mixin = self._make_mixin(_FakeAdminExecutor([]))
        ok, _ = asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(is_admin=True))
        )
        self.assertTrue(ok)

    def test_group_admin_allowed(self) -> None:
        mixin = self._make_mixin(_FakeAdminExecutor(["10001"]))
        ok, _ = asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(sender="10001"))
        )
        self.assertTrue(ok)

    def test_regular_member_denied(self) -> None:
        mixin = self._make_mixin(_FakeAdminExecutor(["20002"]))
        ok, message = asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(sender="10001"))
        )
        self.assertFalse(ok)
        self.assertIn("权限不足", message)

    def test_query_failure_denies_non_admin(self) -> None:
        class _FailingExecutor:
            async def get_group_admins(self, platform_id, group_id):
                raise RuntimeError("network down")

        mixin = self._make_mixin(_FailingExecutor())  # type: ignore[arg-type]
        ok, _ = asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(sender="10001"))
        )
        self.assertFalse(ok)

    def test_cache_avoids_repeat_query(self) -> None:
        executor = _FakeAdminExecutor(["10001"])
        mixin = self._make_mixin(executor)
        for _ in range(3):
            asyncio.run(
                mixin._check_review_permission(_AdminStubEvent(sender="10001"))
            )
        self.assertEqual(len(executor.calls), 1)

    def test_cache_expires_and_reloads(self) -> None:
        executor = _FakeAdminExecutor(["10001"])
        mixin = self._make_mixin(executor)
        asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(sender="10001"))
        )
        mixin._group_admin_cache["g1"] = (0.0, set())  # 强制过期
        asyncio.run(
            mixin._check_review_permission(_AdminStubEvent(sender="10001"))
        )
        self.assertEqual(len(executor.calls), 2)


if __name__ == "__main__":
    unittest.main()
