"""配置集中管理。

提供插件默认配置与运行时读写，支持热加载与持久化。
所有模块通过 ConfigManager 读取配置，不直接持有 AstrBotConfig。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.core.config import AstrBotConfig

DEFAULT_CONFIG: dict[str, Any] = {
    "history_count": 50,
    "review_mode": "both",
    "enable_passive_review": True,
    "risk_threshold": 80,
    "review_timeout": 300,
    "cooldown": 300,
    "enable_blacklist": False,
    "enable_blacklist_check": False,
    "enable_history": True,
    "prompt_path": "",
    "whitelist": [],
    "min_msg_len": 2,
    "llm_max_concurrency": 3,
    "llm_provider_id": "",
    "llm_temperature": 0.3,
    "notify_throttle_seconds": 300,
    "enable_second_review": False,
    "second_review_provider_id": "",
    "mute_duration": 600,
    "admin_qq": [],
    "max_chat_chars": 3000,
    "max_msg_chars": 200,
    "punish_pipeline": {},
    "llm_temperature": 0.3,
    "max_pending_per_user": 2,
    "max_pending_total": 200,
    "enable_regex_prefilter": True,
    "regex_sediment": True,
    "regex_min_hits": 5,
    "regex_min_accuracy": 0.7,
    "regex_max_rules": 200,
    "regex_push_interval": 30,
    "regex_candidate_ttl": 3,
    "regex_approval_permission": "astrbot_admin",
    "regex_push_target": "group",
    "regex_push_admin": [],
    "regex_forward_threshold": 3,
}

# 数值型配置的合法范围（闭区间）；不在表中的键不做范围校验。
_LIMITS: dict[str, tuple[float, float]] = {
    "history_count": (1, 10000),
    "risk_threshold": (0, 100),
    "review_timeout": (1, 604800),
    "cooldown": (0, 2592000),
    "min_msg_len": (0, 10000),
    "llm_max_concurrency": (1, 64),
    "mute_duration": (0, 2592000),
    "max_chat_chars": (0, 1000000),
    "max_msg_chars": (0, 100000),
    "llm_temperature": (0.0, 2.0),
    "notify_throttle_seconds": (0, 86400),
    "max_pending_per_user": (1, 100),
    "max_pending_total": (1, 10000),
    "regex_min_hits": (1, 1000),
    "regex_min_accuracy": (0.0, 1.0),
    "regex_max_rules": (1, 10000),
    "regex_push_interval": (0, 10080),
    "regex_candidate_ttl": (1, 90),
    "regex_forward_threshold": (0, 50),
}
# 支持按群覆盖的配置项。
_OVERRIDE_KEYS = frozenset(
    {
        "risk_threshold",
        "review_mode",
        "enable_passive_review",
        "enable_history",
        "enable_blacklist_check",
        "cooldown",
        "min_msg_len",
        "punish_pipeline",
        "mute_duration",
        "max_chat_chars",
        "max_msg_chars",
        "llm_provider_id",
        "enable_second_review",
        "second_review_provider_id",
        "enable_regex_prefilter",
        "regex_sediment",
        "regex_approval_permission",
        "regex_push_target",
        "regex_push_admin",
        "regex_forward_threshold",
    }
)


def safe_int(value: Any, default: int) -> int:
    """安全整数转换：非法值回退默认。

    配置面板修改不走 /reviewconfig 的范围校验，脏数据可能直接进入
    热加载路径；各模块读取数值配置时应使用本函数兜底。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ConfigManager:
    """插件配置管理器。

    包装 AstrBotConfig，提供统一的读取与修改入口。
    修改后调用 save_config_async 持久化，其余模块经 get_config 回调即可热加载。
    """

    def __init__(self, config: "AstrBotConfig") -> None:
        """初始化配置管理器。

        Args:
            config: AstrBot 传入的插件配置对象。
        """
        self._config = config
        self._overrides: dict[str, dict[str, Any]] = {}
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value

    @property
    def overrides(self) -> dict[str, dict[str, Any]]:
        """当前按群覆盖配置的副本。"""
        return {
            str(group_id): dict(values)
            for group_id, values in self._overrides.items()
        }

    def effective(self, group_id: str = "") -> dict:
        """返回指定群生效的配置（基础配置 + 群覆盖）。

        Args:
            group_id: 群号；为空时仅返回基础配置。
        """
        if not group_id:
            return self._config
        merged = dict(self._config)
        overrides = self._overrides.get(str(group_id))
        if overrides:
            merged.update(overrides)
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        """读取单个配置项。

        Args:
            key: 配置键。
            default: 缺省值。

        Returns:
            配置值。
        """
        return self._config.get(key, default)

    def all(self) -> dict:
        """返回全部配置的副本。

        Returns:
            配置字典副本。
        """
        return dict(self._config)

    async def set_value(self, key: str, raw_value: str) -> tuple[bool, str]:
        """按默认类型转换并写入配置，然后持久化。

        Args:
            key: 配置键。
            raw_value: 字符串形式的原始值。

        Returns:
            (是否成功, 提示信息)。
        """
        if key not in DEFAULT_CONFIG:
            return False, f"未知配置项：{key}"
        try:
            value = self._convert(DEFAULT_CONFIG[key], raw_value)
        except ValueError:
            return False, f"配置项 {key} 的值类型错误。"
        if key == "review_mode" and str(value).lower() not in (
            "active",
            "passive",
            "both",
        ):
            return False, "review_mode 只能是 active / passive / both 之一。"
        if key == "regex_push_target" and str(value).lower() not in (
            "group",
            "admin",
            "off",
        ):
            return False, "regex_push_target 只能是 group / admin / off 之一。"
        if key == "regex_approval_permission" and str(value).lower() not in (
            "astrbot_admin",
            "group_admin",
        ):
            return False, (
                "regex_approval_permission 只能是 astrbot_admin / group_admin 之一。"
            )
        limits = _LIMITS.get(key)
        if limits is not None and not (limits[0] <= value <= limits[1]):
            return False, f"配置项 {key} 的值需在 {limits[0]} ~ {limits[1]} 之间。"
        self._config[key] = value
        try:
            save = getattr(self._config, "save_config_async", None)
            if save is not None:
                await save()
            elif hasattr(self._config, "save_config"):
                self._config.save_config()
        except Exception:
            return True, f"{key} = {value}（内存已生效，持久化失败）"
        return True, f"{key} = {value}"

    async def load_overrides(self, store: Any) -> None:
        """从 KV 恢复按群覆盖配置。"""
        raw = await store.get("group_overrides", {})
        if isinstance(raw, dict):
            self._overrides = {
                str(group_id): dict(values)
                for group_id, values in raw.items()
                if isinstance(values, dict)
            }

    async def _save_overrides(self, store: Any) -> None:
        await store.put("group_overrides", self._overrides)

    async def set_override(
        self,
        store: Any,
        group_id: str,
        key: str,
        raw_value: str,
    ) -> tuple[bool, str]:
        """为指定群设置覆盖配置并持久化。"""
        if key not in DEFAULT_CONFIG or key not in _OVERRIDE_KEYS:
            supported = "、".join(sorted(_OVERRIDE_KEYS))
            return False, f"该配置项不支持按群覆盖。支持：{supported}"
        try:
            value = self._convert(DEFAULT_CONFIG[key], raw_value)
        except ValueError:
            return False, f"配置项 {key} 的值类型错误。"
        if key == "review_mode" and str(value).lower() not in (
            "active",
            "passive",
            "both",
        ):
            return False, "review_mode 只能是 active / passive / both 之一。"
        if key == "regex_push_target" and str(value).lower() not in (
            "group",
            "admin",
            "off",
        ):
            return False, "regex_push_target 只能是 group / admin / off 之一。"
        if key == "regex_approval_permission" and str(value).lower() not in (
            "astrbot_admin",
            "group_admin",
        ):
            return False, (
                "regex_approval_permission 只能是 astrbot_admin / group_admin 之一。"
            )
        limits = _LIMITS.get(key)
        if limits is not None and not (limits[0] <= value <= limits[1]):
            return False, f"配置项 {key} 的值需在 {limits[0]} ~ {limits[1]} 之间。"
        group_id = str(group_id)
        self._overrides.setdefault(group_id, {})[key] = value
        await self._save_overrides(store)
        return True, f"群 {group_id} 的 {key} = {value}"

    async def clear_override(
        self,
        store: Any,
        group_id: str,
        key: str | None = None,
    ) -> tuple[bool, str]:
        """清除指定群的覆盖配置（key 为空时清除全部）。"""
        group_id = str(group_id)
        if key is None:
            if group_id not in self._overrides:
                return False, f"群 {group_id} 没有覆盖配置。"
            del self._overrides[group_id]
            await self._save_overrides(store)
            return True, f"已清除群 {group_id} 的全部覆盖配置。"
        group_overrides = self._overrides.get(group_id)
        if not group_overrides or key not in group_overrides:
            return False, f"群 {group_id} 没有 {key} 的覆盖配置。"
        del group_overrides[key]
        if not group_overrides:
            del self._overrides[group_id]
        await self._save_overrides(store)
        return True, f"已清除群 {group_id} 的 {key}。"

    @staticmethod
    def _convert(default: Any, raw: str) -> Any:
        """按默认值的类型转换原始字符串。"""
        if isinstance(default, bool):
            return str(raw).strip().lower() in ("true", "1", "yes", "on")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, list):
            return [item.strip() for item in str(raw).split(",") if item.strip()]
        if isinstance(default, dict):
            import json

            value = json.loads(str(raw))
            if not isinstance(value, dict):
                raise ValueError("配置项需要 JSON 对象")
            return value
        return str(raw)
