"""UI 美学冒烟：让 LLM 看得见、自己改得好（`docs/design-aesthetics.md`）。

要验证的是**边界**，不是"能跑"：
- token 技能：视觉请求命中 `视觉规范`；无关请求不注入（零噪音不被破坏）；
  常驻索引**恒含**它（"知道有"与"自动给"分工）；
- 视觉能力自述：能看见 / 看不见（含原因）两条都给得出，
  **看不到时不得假装评过外观**；
- 多模态消息：截图成功时消息里**确有** `image_url`；失败时**明确不发**且留日志；
- 旧帧/未生效：`expect_change=True` 时"该变却没变"必须返回 None（不把无关图当现状）；
- 视觉迭代预算：连续靠图驱动到顶 → LLM_STUCK 停手；
- 单写者不破：截图全程**不产生任何真源写入**。

用法：python docs/smoke-aesthetics.py
"""

from __future__ import annotations

import base64
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "Puppet"))

from puppethub import skills as skill_mod                 # noqa: E402
from puppethub.appdir import create_app                   # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", name,
                         ("—— " + str(detail)[:200]) if (detail and not ok) else ""))
    if not ok:
        FAILED.append(name)


class _FakeRenderer:
    """可控的假渲染器：模拟"能截到图 / 截不到 / 该没变"三种情形。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    async def capture(self, expect_change=False):
        self.calls.append(expect_change)
        if not self.frames:
            return None
        cur = self.frames.pop(0)
        # 模拟真实 capture 的语义：expect_change 时"该变却没变"→ None
        if expect_change and getattr(self, "_last", None) == cur:
            return None
        self._last = cur
        return cur


class _NullPage:
    def run_task(self, coro):
        # 直接在当前线程跑不了 async，交给 session 的 holder 走超时分支；
        # 冒烟里我们用"同步已就绪的 future"这条路（见 main）。
        raise RuntimeError("冒烟不进事件循环：用直接注入路径")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="puppethub-smoke-aesthetics-"))
    app = create_app(work, "demo", "美学冒烟")

    print("\n1) token 技能：加载与命中（含技能组）")
    builtin, diags = skill_mod.load(str(app.root))
    names = sorted(s.name for s in builtin)
    check("内置技能含视觉规范", "视觉规范" in names, names)
    check("内置技能含美学组 5 份",
          {"视觉规范", "布局节奏", "交互反馈", "图标与文案", "深色主题"} <= set(names),
          names)
    check("内置文件零诊断", not diags, [d.message for d in diags])

    # **常驻技能**（语言速查）每轮必进且排最前，不参与触发词匹配——断言里摘掉它。
    always_names = {s.name for s in builtin if getattr(s, "always", False)}

    def auto(hits):
        """只留非常驻技能的名字（排除"（未注入）"占位）。"""
        return [h["name"] for h in hits
                if h["name"] not in always_names and h["name"] != "（未注入）"]

    # **组语义（2026-10-06 改）**：命中组内任一份 → **总纲常驻**（共同纪律）+ **只有命中的成员进**。
    # 改的理由：整组连坐让一轮请求里技能占 78%（实测），而程序全文只占 1.5%——
    # 那与"按需注入"相反。一致性改由**总纲**（组内 order 最小的 `视觉规范` 的 synopsis）保住。
    hits = skill_mod.select(builtin, "把按钮颜色调深一点", "")
    got = auto(hits)
    check("请求含'颜色'→ 命中的视觉规范在场", "视觉规范" in got, got)
    check("请求含'颜色'→ **未命中的成员不再连坐**（省 token 的关键）",
          not ({"布局节奏", "交互反馈", "图标与文案", "深色主题"} & set(got)), got)
    # 专项命中时**专项排前、总纲垫后**（总纲是背景纪律，专项才是本轮要查的）
    hits = skill_mod.select(builtin, "这个图标选得不好", "")
    got = auto(hits)
    check("专项命中 → 专项排首",
          got and got[0] == "图标与文案", got)
    check("专项命中 → 总纲（视觉规范）仍垫后在场（共同纪律不断线）",
          "视觉规范" in got, got)
    hits = skill_mod.select(builtin, "这里间距太挤了", "")
    got = auto(hits)
    # "间距"同时命中视觉规范与布局节奏（两者都管间距）——两个都在前排即可。
    check("请求含'间距'→ 命中的两份都排在前",
          set(got[:2]) == {"布局节奏", "视觉规范"}, got)
    check("请求含'间距'→ 未命中的交互反馈**不进**（只点名，可自取）",
          "交互反馈" not in got and any(
              "交互反馈" in h["text"] for h in hits if h["name"] == "美学"), got)
    hits = skill_mod.select(builtin, "帮我美化一下", "")
    got = auto(hits)
    check("请求含'美化'→ 命中的视觉规范排首", got and got[0] == "视觉规范", got)
    # 专项命中：深色主题进，但总纲（视觉规范）**始终在场**——共同纪律不断线
    hits = skill_mod.select(builtin, "做个深色的夜间主题", "")
    got = auto(hits)
    check("请求含'深色'→ 专项排首 + 总纲跟后",
          got and got[0] == "深色主题" and "视觉规范" in got, got)
    check("请求含'深色'→ 无关的美学成员不进",
          not ({"布局节奏", "交互反馈"} & set(got)), got)
    hits = skill_mod.select(builtin, "加一个删除功能", "")
    got = auto(hits)
    check("无关请求不注入美学技能（零噪音）",
          not ({"视觉规范", "布局节奏", "交互反馈", "图标与文案", "深色主题"} & set(got)), got)
    check("无关请求仍注入非美学技能（删改数据）", "删改数据" in got, got)
    check("常驻索引恒含视觉规范（'知道有'的钩子）",
          "视觉规范" in skill_mod.index_block(builtin))
    check("常驻技能不进索引（已每轮注入，不该再列进'可取'）",
          "语言速查" not in skill_mod.index_block(builtin))
    # 预算：单份正文超旧 SELECT_CHARS(1600) 却**不该**被截断（组预算覆盖）
    long_hits = skill_mod.select(builtin, "美化一下", "")
    truncated = [h["name"] for h in long_hits if "被截断" in h["text"]]
    check("组注入时单份不被腰斩（旧 1600 字预算的 bug）", not truncated, truncated)
    # 总纲要**短**于整份正文——不然这次改造等于没省
    lead = next(s for s in builtin if s.name == "视觉规范")
    check("总纲（synopsis）明显短于整份正文",
          0 < len(lead.synopsis) < len(lead.body) / 3,
          (len(lead.synopsis), len(lead.body)))

    print("\n2) 视觉能力自述：能看见 / 看不见")
    from puppethub.builtin_plugins import default_prompt
    from puppethub import context as ctx

    class _Eng:
        program = type("P", (), {"nodes": {}})()

        def program_lines(self):
            return ["add #root window #win"]

    def build(vision):
        return ctx.build(app_dir=app, engine=_Eng(), catalog=[], diagnostics=[],
                         turns=[],
                         rendering={"snapshot": True, "controls": [], "attributes": [],
                                    "animations": [], "icons": []},
                         request="帮我美化一下颜色", mode="执行", skills=[], vision=vision)

    prompt = default_prompt.DefaultPrompt()
    seen = prompt.rewrite(build({"can_see": True, "why_not": ""}))["system"]
    check("can_see=true → 明说能看见", "**能看见。**" in seen, seen[:200])
    blind = prompt.rewrite(build({"can_see": False, "why_not": "provider 未声明 vision"}))["system"]
    check("can_see=false → 明说看不见 + 给原因",
          "**看不见**" in blind and "provider 未声明 vision" in blind, blind[:300])
    check("看不见时要求：不得假装评过外观", "不要假装评价过外观" in blind, blind[:400])
    check("视觉槽位进上下文", build({"can_see": True}).get("vision") ==
          {"can_see": True, "why_not": ""}, build({"can_see": True}).get("vision"))

    print("\n3) 多模态消息：截图成功才挂图")
    from puppethub.chat import Chat
    from puppethub.session import Session

    session = Session(app)
    session.start()
    chat = session.chat
    # 造一个"能看见"的姿态
    chat.provider = type("P", (), {"supports_vision": True, "context_limit": lambda self: None})()
    session._ui_loop = object()          # 非 None 即"有窗口"

    frames = [b"\x89PNG\r\n\x1a\n" + b"A" * 100]
    renderer = _FakeRenderer(frames)
    # 用直接注入绕过事件循环：capture_for_llm 的 holder 分支在无 loop 时不可用，
    # 这里直接替换成同步实现，验证的是**下游**（消息构造）而不是桥接本身。
    session.renderer = renderer
    session.capture_for_llm = lambda expect_change=False: (
        renderer.frames.pop(0) if renderer.frames else None)

    session._visual_pending_lines = (["add #win text #t2 text=\"x\" fg=\"#94a3b8\""], [1])
    session._visual_dirty = None
    msg = chat._visual_message("批末回灌")
    check("截图成功 → 消息是多模态（text + image_url）",
          isinstance(msg, dict) and isinstance(msg.get("content"), list)
          and any(p.get("type") == "image_url" for p in msg["content"]),
          msg if not isinstance(msg, dict) else [p.get("type") for p in msg["content"]])
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        url = [p["image_url"]["url"] for p in msg["content"] if p.get("type") == "image_url"][0]
        check("图是 data URL 且 base64 可解",
              url.startswith("data:image/png;base64,")
              and len(base64.b64decode(url.split(",", 1)[1])) > 8, url[:60])
        text = [p["text"] for p in msg["content"] if p.get("type") == "text"][0]
        check("消息带增量（我这一轮改的内容）", "我这一轮改的内容" in text, text[:200])
        check("消息要求：没问题就停下（防为改而改）", "看着没问题" in text, text[:300])

    print("\n4) 截图失败可见：不发假图")
    before = len(session.log)
    renderer.frames = []                 # 截不到
    msg_none = chat._visual_message("批末回灌")
    check("截不到图 → 返回 None（不挂空图）", msg_none is None, msg_none)
    new_logs = list(session.log)[before:]
    check("截不到图 → 留下可见日志", any(e.get("code") == "SNAPSHOT" for e in new_logs),
          new_logs[-2:] if new_logs else "无日志")

    print("\n5) expect_change：该变却没变 → 不把无关图当现状")
    from puppethub.render import FletRenderer

    class _Page:
        def __init__(self, frames):
            self.frames = list(frames)

        async def take_screenshot(self):
            return self.frames.pop(0) if self.frames else None

    import asyncio
    same = b"\x89PNG\r\n\x1a\n" + b"S" * 50
    r = FletRenderer.__new__(FletRenderer)
    r.page = _Page([same, same])
    r._last_shot = None
    first = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        r.capture(expect_change=False))
    check("首次截图（expect_change=False）正常返回", first == same, first)
    second = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        r.capture(expect_change=True))
    check("声明'该变了'但字节相同 → 返回 None（渲染未生效）", second is None, second)

    r2 = FletRenderer.__new__(FletRenderer)
    changed = b"\x89PNG\r\n\x1a\n" + b"T" * 50
    r2.page = _Page([same, changed])
    r2._last_shot = None
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        r2.capture(expect_change=False))
    ok_changed = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        r2.capture(expect_change=True))
    check("声明'该变了'且确实变了 → 正常返回新帧", ok_changed == changed, ok_changed)

    print("\n6) 视觉迭代预算：到顶停手")
    session2 = Session(create_app(work, "demo2", "预算"))
    session2.start()
    chat2 = session2.chat
    chat2.fail_budget = 3
    chat2._visual_turns = 0
    ok_all = [chat2._visual_budget_ok() for _ in range(3)]
    check("预算内允许（3 次）", ok_all == [True, True, True], ok_all)
    stopped = chat2._visual_budget_ok()
    check("超出预算 → 停手", stopped is False, stopped)
    check("停手给出 LLM_STUCK 同款说明（可见）",
          "视觉迭代" in (chat2.stuck_note or ""), chat2.stuck_note)
    check("停手留下可见日志",
          any(e.get("code") == "LLM_STUCK" for e in session2.log),
          [e.get("code") for e in list(session2.log)[-5:]])

    print("\n7) 单写者不破：截图不写真源")
    src_before = app.read_source()
    snap_before = len(app.list_snapshots()) if hasattr(app, "list_snapshots") else None
    session._visual_dirty = None
    session.capture_for_llm = lambda expect_change=False: b"\x89PNG\r\n\x1a\n" + b"Z" * 10
    chat._visual_message("批末回灌")
    check("截图后真源逐字节未变", app.read_source() == src_before)

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s" % ("全部通过" if not FAILED else "失败：%s" % "、".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
