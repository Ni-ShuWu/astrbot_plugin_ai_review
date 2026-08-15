"""AstrBot LLM 调用客户端。

封装对 AstrBot 已配置大模型的唯一调用点（get_using_provider + text_chat），
不接触任何具体模型 SDK。支持并发限流与异常通知。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from .logger import get_logger, log_event

if TYPE_CHECKING:
    from astrbot.api.star import Context

from ..config import safe_int

_DEFAULT_MAX_CONCURRENCY = 3
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_RETRY_TIMES = 2
_DEFAULT_RETRY_DELAYS = (2.0, 4.0)

logger = get_logger()

Notifier = Callable[[str], Awaitable[None]]


class LLMClient:
    """AstrBot 大模型调用客户端。

    Attributes:
        max_concurrency: 同时进行的 LLM 请求数上限（支持热加载）。
    """

    def __init__(
        self,
        context: "Context",
        get_config: Callable[[], dict[str, Any]],
        notifier: Notifier | None = None,
        retry_times: int = _DEFAULT_RETRY_TIMES,
        retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
    ) -> None:
        """初始化 LLM 客户端。

        Args:
            context: AstrBot 插件 Context 对象。
            get_config: 返回当前插件配置字典的回调。
            notifier: 异常告警通知回调，可为空。
            retry_times: 网络失败后的最大重试次数。
            retry_delays: 各次重试前的等待秒数。
        """
        self._context = context
        self._get_config = get_config
        self._notifier = notifier
        self._semaphore = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)
        self._max_concurrency = _DEFAULT_MAX_CONCURRENCY
        self._temperature = _DEFAULT_TEMPERATURE
        self._retry_times = max(0, int(retry_times))
        self._retry_delays = tuple(retry_delays)
        self._last_provider_id = ""

    @property
    def last_provider_id(self) -> str:
        """最近一次实际完成调用的 Provider ID（未调用或失败为空）。"""
        return self._last_provider_id

    def _sync_config(self) -> None:
        """同步并发上限与温度配置（热加载）。"""
        config = self._get_config()
        new_limit = safe_int(
            config.get("llm_max_concurrency"), _DEFAULT_MAX_CONCURRENCY
        )
        new_limit = max(1, new_limit)
        if new_limit != self._max_concurrency:
            self._semaphore = asyncio.Semaphore(new_limit)
            self._max_concurrency = new_limit
        try:
            self._temperature = float(
                config.get("llm_temperature", _DEFAULT_TEMPERATURE)
            )
        except (TypeError, ValueError):
            self._temperature = _DEFAULT_TEMPERATURE

    async def _resolve_provider(self, umo: str, provider_id: str = "") -> Any | None:
        """解析本次审核使用的对话模型 Provider。

        优先使用显式传入的 ``provider_id``；否则使用配置的
        ``llm_provider_id``；均未配置或找不到时回退到会话当前使用的
        模型（``get_using_provider``）。

        Args:
            umo: unified_message_origin，用于获取会话默认模型。
            provider_id: 显式指定的 Provider ID（可空）。

        Returns:
            Provider 实例；解析失败时返回 None（内部已通知管理员）。
        """
        config = self._get_config()
        provider_id = (provider_id or "").strip() or str(
            config.get("llm_provider_id", "") or ""
        ).strip()
        if provider_id:
            get_provider = getattr(self._context, "get_provider_by_id", None)
            if get_provider is not None:
                try:
                    provider = get_provider(provider_id)
                except Exception as exc:
                    logger.warning(
                        "[AI审核] 获取指定 Provider(%s) 异常：%s", provider_id, exc
                    )
                    provider = None
                if provider is not None:
                    return provider
            message = (
                f"[AI审核] 配置的 llm_provider_id={provider_id} 不存在，"
                "已回退到会话默认模型。"
            )
            logger.warning(message)
            await self._notify(message)
        try:
            return self._context.get_using_provider(umo)
        except Exception as exc:
            message = f"[AI审核] 获取对话模型 Provider 失败: {exc!s}"
            logger.error(message, exc_info=True)
            await self._notify(message)
            return None

    async def _notify(self, message: str) -> None:
        """发送异常告警通知，失败不影响主流程。

        Args:
            message: 告警内容。
        """
        if self._notifier is None:
            return
        try:
            await self._notifier(message)
        except Exception:
            logger.warning("发送 AI 调用异常通知失败。", exc_info=True)

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        output_prompt: str,
        umo: str,
        provider_id: str = "",
    ) -> str | None:
        """调用模型完成一次文本生成。

        Args:
            system_prompt: 系统提示词（审核规则）。
            user_prompt: 用户提示词（聊天记录）。
            output_prompt: 输出约束提示词（JSON 格式）。
            umo: unified_message_origin，用于获取该会话使用的模型。
            provider_id: 显式指定使用的 Provider ID；留空跟随
                llm_provider_id 配置或会话默认模型。

        Returns:
            模型返回的纯文本；无可用模型或调用失败时返回 None。
        """
        self._sync_config()
        provider = await self._resolve_provider(umo, provider_id)
        if provider is None:
            return None
        prompt = f"{user_prompt}\n\n{output_prompt}"
        try:
            provider_id = provider.meta().id
        except Exception:
            provider_id = ""
        async with self._semaphore:
            response = await self._chat_with_retry(
                provider,
                prompt,
                system_prompt,
            )
        if response is None:
            log_event("llm_call_failed", provider=provider_id, umo=umo)
            logger.error("[AI审核] 模型返回为空，本次审核结束。")
            return None
        try:
            self._last_provider_id = provider.meta().id
        except Exception:
            self._last_provider_id = ""
        log_event(
            "llm_call_ok",
            provider=provider_id,
            chars=len(prompt),
            umo=umo,
        )
        completion = getattr(response, "completion_text", None)
        if completion is None and isinstance(response, str):
            completion = response
        if completion is None:
            log_event("llm_response_empty", provider=provider_id)
            logger.error("[AI审核] 模型响应缺少文本内容，本次审核结束。")
            return None
        return completion or ""

    async def _chat_with_retry(
        self,
        provider: Any,
        prompt: str,
        system_prompt: str,
    ) -> Any | None:
        """调用模型并处理重试，失败时通知管理员一次。

        网络/服务端异常按指数退避重试；Provider 不支持 temperature
        参数时自动降级（移除该参数后重试）。
        """
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "system_prompt": system_prompt,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        last_exc: Exception | None = None
        for attempt in range(self._retry_times + 1):
            try:
                return await provider.text_chat(**kwargs)
            except TypeError as exc:
                if "temperature" in str(exc) and "temperature" in kwargs:
                    logger.warning(
                        "[AI审核] 当前 Provider 不支持 temperature 参数，已降级重试。"
                    )
                    kwargs.pop("temperature")
                last_exc = exc
            except Exception as exc:
                last_exc = exc
            if attempt < self._retry_times:
                delay = self._retry_delays[
                    min(attempt, len(self._retry_delays) - 1)
                ]
                await asyncio.sleep(delay)
        message = (
            f"[AI审核] 调用模型失败（已重试 {self._retry_times} 次）: "
            f"{last_exc!s}"
        )
        logger.error(message, exc_info=last_exc)
        await self._notify(message)
        return None
