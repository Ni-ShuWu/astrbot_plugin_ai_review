"""插件全链路冒烟测试（本地模拟 AstrBot 运行时，不依赖真实 AstrBot）。

模拟 AstrBot 关键片段（Star 基类/Context/Provider/OneBot 客户端/消息事件），
驱动真实插件代码跑通完整链路：

装配 → initialize 恢复 → 被动审核（LLM 判定）→ 规则预筛（跳过 LLM）→
任务列表/详情（合并转发）→ 群管鉴权 → 通过任务（处罚流水线）→
规则候选沉淀 → 候选审批 → 推送（群号前缀）→ 持久化恢复 → terminate 清理

运行方式：python smoke_test.py（在插件根目录）
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent

# ---------- 命名空间包注册（与测试同模式，目录名解耦） ----------
_PKG = "_plugin_under_test"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_ROOT)]
    _pkg.__package__ = _PKG
    sys.modules[_PKG] = _pkg
sys.path.insert(0, str(_ROOT))


# ---------- fake astrbot 模块（AstrBot API 片段模拟） ----------
class _FakeStar:
    """模拟 AstrBot Star 基类：Context + 插件 KV 存储（v4.13+ PluginKVStoreMixin）。"""

    def __init__(self, context, config=None) -> None:
        self.context = context
        self.config = config or {}
        self._kv_data = getattr(context, "kv", {})

    async def put_kv_data(self, key, value) -> None:
        self._kv_data[key] = value

    async def get_kv_data(self, key, default=None):
        return self._kv_data.get(key, default)


def _install_fake_astrbot(star_cls) -> None:
    """注册 astrbot.* 模块；Star 由调用方注入（带 KV 的版本）。"""
    if "astrbot" in sys.modules:
        return
    base = types.ModuleType("astrbot")
    base.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []

    filt = types.ModuleType("astrbot.api.event.filter")
    filt.EventMessageType = SimpleNamespace(GROUP_MESSAGE="group")
    filt.PermissionType = SimpleNamespace(ADMIN="admin", MEMBER="member")
    filt.event_message_type = lambda *a, **k: (lambda fn: fn)
    filt.command = lambda *a, **k: (lambda fn: fn)
    filt.permission_type = lambda *a, **k: (lambda fn: fn)

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = filt
    # 真实 AstrBot 从 astrbot.api.event 导出 MessageChain（re-export 自 message_event_result）
    event_mod.MessageChain = type("MessageChain", (list,), {})

    class _At:
        type = "at"

        def __init__(self, qq: str = "", name: str = "", **_k) -> None:
            self.qq = qq
            self.name = name

    class _AtAll(_At):
        def __init__(self, qq: str = "all", **kw) -> None:
            super().__init__(qq=qq, **kw)

    class _Plain:
        type = "text"

        def __init__(self, text: str = "", **_k) -> None:
            self.text = text

    class _Node:
        def __init__(self, name="", uin="0", content=None, **_k) -> None:
            self.name = name
            self.uin = uin
            self.content = content or []

    class _Nodes:
        type = "Nodes"

        def __init__(self, nodes=None, **_k) -> None:
            self.nodes = nodes or []

    class _MessageChain(list):
        pass

    components = types.ModuleType("astrbot.api.message_components")
    components.At = _At
    components.AtAll = _AtAll
    components.Plain = _Plain
    components.Node = _Node
    components.Nodes = _Nodes

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = star_cls
    star.register = lambda *a, **k: (lambda cls: cls)

    sys.modules["astrbot"] = base
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.event.filter"] = filt
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api.star"] = star


_install_fake_astrbot(_FakeStar)

# ---------- 导入真实插件模块 ----------
from _plugin_under_test.main import AiReviewPlugin  # noqa: E402
from _plugin_under_test.commands.review import ReviewCommandMixin  # noqa: E402
from _plugin_under_test.models import ReviewResult  # noqa: E402


# ---------- 模拟 AstrBot 运行时组件 ----------
class StubLLM:
    """模拟 LLM 客户端（不经过 AI 提供商）：按请求内容直接返回预设判别文档。

    审核 JSON（判别文档）由测试模拟给出，用于验证插件业务链路；
    实际部署中该环节由 AstrBot Provider 完成。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.last_provider_id = "stub-model"  # 模拟 LLMClient 的判定模型追踪

    async def chat(self, system: str, user: str, output: str, umo: str) -> str:
        self.calls += 1
        text = user or ""
        if "正则表达式" in text:  # 规则提炼调用（rule.txt 模板特征）
            return (
                '{"pattern": "优惠活动|扫码", "note": "广告推广", "level": 2}'
            )
        if "加我微信" in text or "广告" in text:
            return (
                '{"illegal": true, "risk": 92, "type": "广告", '
                '"reason": "发布广告引流信息", "evidence": ["快来加我微信"], '
                '"suggestion": "mute"}'
            )
        return (
            '{"illegal": false, "risk": 0, "type": "", "reason": "", '
            '"evidence": [], "suggestion": "warn"}'
        )


class FakeBot:
    """模拟 OneBot 客户端：群成员列表/处罚动作记录。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if action == "get_group_member_list":
            return [
                {"user_id": 10001, "role": "owner"},   # 群主
                {"user_id": 10002, "role": "admin"},   # 群管
                {"user_id": 20001, "role": "member"},  # 普通成员
            ]
        return None


class FakePlatform:
    def __init__(self, bot: FakeBot) -> None:
        self._bot = bot
        self._id = "aiocqhttp"

    def get_client(self) -> FakeBot:
        return self._bot

    def meta(self) -> SimpleNamespace:
        return SimpleNamespace(id=self._id)


class FakeContext:
    """模拟 AstrBot Context：平台实例/消息发送（不含 AI Provider）。"""

    def __init__(self) -> None:
        self.kv: dict = {}
        self.bot = FakeBot()
        self.platform = FakePlatform(self.bot)
        self.sent: list[tuple[str, object]] = []

    def get_platform_inst(self, platform_id):
        return self.platform

    def get_all_stars(self):
        return []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class FakeEvent:
    """模拟 AstrBot 群消息事件。"""

    def __init__(
        self,
        content: str,
        sender: str = "20001",
        group: str = "123456789",
        message_str: str = "",
        at: list | None = None,
    ) -> None:
        self.message_obj = SimpleNamespace(
            timestamp=1700000000.0,
            message=at or [],
        )
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group}"
        self.message_str = message_str
        self.role = "member"
        self._content = content
        self._sender = sender
        self._group = group

    def get_group_id(self) -> str:
        return self._group

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return f"用户{self._sender}"

    def get_message_outline(self) -> str:
        return self._content

    def get_self_id(self) -> str:
        return "bot"

    def is_admin(self) -> bool:
        return False

    def get_platform_id(self) -> str:
        return "aiocqhttp"

    def plain_result(self, text: str):
        """模拟 AstrMessageEvent.plain_result：返回带 message 的结果对象。"""
        return SimpleNamespace(message=text)


async def pump(seconds: float = 0.05) -> None:
    """让受管后台任务（被动审核/沉淀）有机会执行完成。"""
    await asyncio.sleep(seconds)


# ---------- 冒烟步骤 ----------
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


async def run() -> None:
    print("=== 冒烟测试：插件全链路 ===\n")

    # ---------- 1. 装配 ----------
    print("[1] 插件装配")
    ctx = FakeContext()
    plugin = AiReviewPlugin(ctx, {})
    # 注入 stub LLM：全程不经过 AI 提供商，判别文档由测试模拟
    stub_llm = StubLLM()
    plugin.llm = stub_llm
    plugin.workflow.llm = stub_llm
    check("模块装配完整", all(
        getattr(plugin, name) is not None
        for name in ("history", "prompt", "queue", "rules", "llm", "workflow", "punisher", "executor", "stats")
    ))

    # ---------- 2. initialize（KV 恢复 + 推送循环） ----------
    print("[2] initialize")
    await plugin.initialize()
    check("推送循环已启动", bool(plugin._bg_tasks))

    # ---------- 3. 被动审核：正常消息（判别文档=不违规） ----------
    print("[3] 被动审核（正常消息）")
    await plugin.on_group_message(FakeEvent("今天天气不错，出去走走"))
    await pump()
    check("正常消息调用 LLM", stub_llm.calls == 1)
    check("无违规不产生任务", await plugin.queue.pending_count() == 0)

    # ---------- 4. 被动审核：违规消息（判别文档=违规 → 入队） ----------
    print("[4] 被动审核（违规消息）")
    await plugin.on_group_message(FakeEvent("快来加我微信，广告一波"))
    await pump()
    check("违规消息生成任务", await plugin.queue.pending_count() == 1)

    # ---------- 5. 规则层：激活规则命中跳过 LLM ----------
    print("[5] 规则预筛")
    ok, msg, _ = await plugin.rules.add("加我微信|广告", source="manual", note="广告")
    check("手动添加规则成功", ok, msg)
    await plugin.on_group_message(FakeEvent("加我微信领红包", sender="20002"))
    await pump()
    check("规则命中跳过 LLM", stub_llm.calls == 2)  # 未新增调用
    check("规则命中生成任务", await plugin.queue.pending_count() == 2)

    # ---------- 6. 命令：列表（紧凑内联） ----------
    print("[6] /review list")
    async def cmd(*args, **kwargs):
        results = []
        async for r in plugin._cmd_review(*args, **kwargs):
            results.append(r)
        return results

    from _plugin_under_test.commands.review import ReviewCommandMixin as _RCM
    list_result = (await cmd(FakeEvent("", message_str="/review list"), "list", ""))[0]
    list_text = list_result.message if hasattr(list_result, "message") else str(list_result)
    check("列表含审批内联", "✅ /review pass" in str(list_result))

    # ---------- 7. 群管鉴权 ----------
    print("[7] 群管鉴权")
    owner_ok, _ = await plugin._check_review_permission(FakeEvent("", sender="10001"))
    check("群主可执行", owner_ok)
    member_ok, _ = await plugin._check_review_permission(FakeEvent("", sender="20001"))
    check("普通成员被拒", not member_ok)

    # ---------- 8. detail 合并转发 ----------
    print("[8] /review detail（合并转发）")
    tasks = await plugin.queue.list_pending("123456789")
    first_task = tasks[0]
    sent_before = len(ctx.sent)
    detail_result = await plugin._send_task_detail(
        FakeEvent("", message_str=f"/review detail {first_task.task_id}"), first_task
    )
    check("detail 走合并转发", len(ctx.sent) == sent_before + 1)
    check("转发含任务概要", "任务 #" in str(ctx.sent[-1][1][0].nodes[0].content[0].text))

    # ---------- 9. pass：处罚流水线 ----------
    print("[9] /review pass（处罚流水线）")
    async for _ in plugin._cmd_review(
        FakeEvent("", sender="10001", message_str=f"/review pass {first_task.task_id}"),
        "pass", first_task.task_id,
    ):
        pass
    check("处罚执行 set_group_ban", any(a == "set_group_ban" for a, _ in ctx.bot.calls))
    check("任务已通过", (await plugin.queue.get(first_task.task_id)).status.value == "approved")

    # ---------- 10. 规则候选沉淀（pass 后异步提炼） ----------
    print("[10] 规则候选沉淀")
    await asyncio.sleep(0.3)  # 等待受管后台候选收集完成
    candidates = plugin.rules.candidates()
    check("生成规则候选", len(candidates) == 1, f"实际 {len(candidates)}")
    candidate_id = candidates[0].candidate_id if candidates else ""

    # ---------- 11. 候选审批 ----------
    print("[11] 候选审批")
    if candidate_id:
        ok, msg = await plugin.rules.approve_candidate(candidate_id)
        check(
            "批准候选进入观察期",
            ok and any(r.status.value == "observing" for r in plugin.rules.list()),
            msg,
        )

    # ---------- 12. 推送（群号前缀） ----------
    print("[12] 推送（群号前缀 + 阈值转发）")
    # 构造 3 条候选触发合并转发阈值（默认 3）
    from _plugin_under_test.models import RuleCandidate
    for i in range(3):
        plugin.rules._candidates[f"cand{i}"] = RuleCandidate(
            candidate_id=f"cand{i}", pattern=f"pattern{i}", note=f"候选{i}",
            level=1, group_id="123456789", platform_id="aiocqhttp",
        )
    sent_before = len(ctx.sent)
    await plugin._push_rule_candidates()
    check("候选≥3 打包合并转发", len(ctx.sent) == sent_before + 1)
    nodes = ctx.sent[-1][1][0].nodes
    check("转发节点数=候选数", len(nodes) == 3, f"实际 {len(nodes)}")
    check("转发节点含审批命令", "approve" in nodes[0].content[0].text)
    # 清空候选，测文本推送的群号前缀
    plugin.rules._candidates.clear()
    plugin.rules._candidates["candx"] = RuleCandidate(
        candidate_id="candx", pattern="p", note="x", level=1,
        group_id="123456789", platform_id="aiocqhttp",
    )
    sent_before = len(ctx.sent)
    await plugin._push_rule_candidates()
    check("少量候选走文本且带群号", "📬 [群 123456789]" in str(ctx.sent[-1][1][0].text))

    # ---------- 13. 持久化恢复 ----------
    print("[13] 持久化恢复")
    # 推送测试直接注入的候选需显式落库（真实路径中由 collect_candidate 保存）
    await plugin.rules._save_candidates()
    ctx2 = FakeContext()
    ctx2.kv = ctx.kv  # 共享 KV（模拟重启）
    plugin2 = AiReviewPlugin(ctx2, {})
    await plugin2.initialize()
    check("队列恢复", await plugin2.queue.pending_count() == 1)
    check("规则恢复", len(plugin2.rules.list()) >= 2)  # 手动规则 + 已激活观察期规则
    check("候选恢复", len(plugin2.rules.candidates()) >= 1)

    # ---------- 14. terminate ----------
    print("[14] terminate")
    await plugin2.terminate()
    check("后台任务全部结束", all(t.done() for t in plugin2._bg_tasks))

    print(f"\n=== 冒烟测试完成：{PASS} 通过 / {FAIL} 失败 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
