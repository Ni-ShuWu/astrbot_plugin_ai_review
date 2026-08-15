"""LLM 调用超时防护测试（fix/llm-call-timeout）。

覆盖：单次模型调用超过 llm_timeout 秒时被 asyncio.wait_for 中断、
超时计入重试（后续尝试可能成功）、llm_timeout=0 表示不超时、
超时最终失败时通知管理员。
"""

from __future__ import annotations

import asyncio
import sys
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

from _plugin_under_test.utils.llm import LLMClient  # noqa: E402

_RESP = SimpleNamespace(completion_text="ok")


class _HangProvider:
    """第 hang_calls 次调用永久挂起（不返回不抛错），其余正常返回。"""

    def __init__(self, hang_calls: int = 1) -> None:
        self.hang_calls = hang_calls
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id="hang", model="hang-model")

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.calls <= self.hang_calls:
            await asyncio.sleep(3600)
        return _RESP


class _SleepProvider:
    """每次调用固定延时后正常返回（用于验证不超时配置）。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id="sleep", model="sleep-model")

    async def text_chat(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return _RESP


class _Ctx:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_using_provider(self, umo: str):
        return self.provider


class LLMCallTimeoutTest(unittest.TestCase):
    @staticmethod
    def _client(provider, cfg: dict, retry_times: int = 0) -> LLMClient:
        return LLMClient(
            _Ctx(provider),
            lambda gid="": cfg,
            retry_times=retry_times,
            retry_delays=(0.0, 0.0),
        )

    def test_timeout_interrupts_hung_call_and_notifies(self) -> None:
        provider = _HangProvider(hang_calls=1)
        notified: list[str] = []

        async def notify(message: str) -> None:
            notified.append(message)

        client = self._client(
            provider,
            {"llm_timeout": 0.05},
        )
        client._notifier = notify
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertIsNone(text)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(notified), 1)
        self.assertIn("超时", notified[0])

    def test_timeout_counts_toward_retry_then_succeeds(self) -> None:
        provider = _HangProvider(hang_calls=1)
        client = self._client(
            provider,
            {"llm_timeout": 0.05},
            retry_times=1,
        )
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 2)

    def test_timeout_zero_disables_interruption(self) -> None:
        provider = _SleepProvider(delay=0.1)
        client = self._client(provider, {"llm_timeout": 0})
        text = asyncio.run(
            asyncio.wait_for(client.chat("s", "u", "o", "umo"), timeout=2)
        )
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 1)

    def test_timeout_above_call_duration_passes(self) -> None:
        provider = _SleepProvider(delay=0.05)
        client = self._client(provider, {"llm_timeout": 5})
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok")
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
