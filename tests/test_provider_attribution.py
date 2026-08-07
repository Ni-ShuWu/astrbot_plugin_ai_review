"""Provider 归属竞态测试（fix/provider-attribution）。

LLMClient 的 last_provider_id 是实例级共享状态：并发审核时，
某次调用完成到读取之间可能被其他调用覆盖，导致任务记录的
llm_provider / second_llm_provider 归属错误。

chat_ex 返回本次调用实际使用的 Provider ID，规避该竞态。
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


class _StubProvider:
    def __init__(self, provider_id: str, fail: bool = False) -> None:
        self.provider_id = provider_id
        self.fail = fail
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id=self.provider_id, model="m")

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network error")
        return SimpleNamespace(completion_text=f"ok-{self.provider_id}")


class _Ctx:
    def __init__(self, providers: list) -> None:
        self.providers = providers

    def get_using_provider(self, umo: str):
        return self.providers[0]

    def get_provider_by_id(self, provider_id: str):
        for prov in self.providers:
            if prov.meta().id == provider_id:
                return prov
        return None


class ProviderAttributionTest(unittest.TestCase):
    @staticmethod
    def _client(providers: list, cfg: dict | None = None) -> LLMClient:
        return LLMClient(
            _Ctx(providers),
            lambda gid="": cfg or {"llm_temperature": 0.3},
            retry_times=0,
            retry_delays=(0.0,),
        )

    def test_concurrent_chats_report_own_provider(self) -> None:
        provider_a = _StubProvider("model-a")
        provider_b = _StubProvider("model-b")
        client = self._client([provider_a, provider_b])

        async def scenario():
            first, second = await asyncio.gather(
                client.chat_ex("s", "u", "o", "umo", "model-a"),
                client.chat_ex("s", "u", "o", "umo", "model-b"),
            )
            return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first, ("ok-model-a", "model-a"))
        self.assertEqual(second, ("ok-model-b", "model-b"))
        self.assertEqual(provider_a.calls, 1)
        self.assertEqual(provider_b.calls, 1)

    def test_chat_ex_reports_provider_even_on_failure(self) -> None:
        provider = _StubProvider("model-x", fail=True)
        client = self._client([provider])
        text, used_provider = asyncio.run(
            client.chat_ex("s", "u", "o", "umo", "model-x")
        )
        self.assertIsNone(text)
        self.assertEqual(used_provider, "model-x")

    def test_chat_ex_unpinned_reports_resolved_provider(self) -> None:
        session = _StubProvider("session-model")
        client = self._client([session])
        text, used_provider = asyncio.run(client.chat_ex("s", "u", "o", "umo"))
        self.assertEqual(text, "ok-session-model")
        self.assertEqual(used_provider, "session-model")

    def test_chat_wrapper_returns_plain_text(self) -> None:
        provider = _StubProvider("model-y")
        client = self._client([provider])
        text = asyncio.run(client.chat("s", "u", "o", "umo"))
        self.assertEqual(text, "ok-model-y")


if __name__ == "__main__":
    unittest.main()
