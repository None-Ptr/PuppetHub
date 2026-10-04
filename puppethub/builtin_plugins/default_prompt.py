"""内置 prompt：上下文的分层组装（设计草案第 5 节的"每轮固定 + 按需"）。

为什么它在这里而不是核心里：**核心里不硬编码任何 prompt 逻辑**，只提供"基础上下文块"
（真源全文 · 诊断摘要 · 最近 K 轮 · 能力签名 · 词汇边界 · 按需规范章节）。
于是"改写 prompt"就等于换掉这个插件——插件体系把上下文组装统一掉了，而不是与它对立。

`rewrite(context)` **允许完全改写**（不只是追加）：代价是搭建质量取决于插件作者品味，
但插件碰不到任何机制（拿不到引擎），最坏只能让 LLM 表现变差，而那始终可见。
"""

from __future__ import annotations

NAME = "default"
PROVIDES = ["prompt"]

SYSTEM = """你是这个 app 的**共作者**。人用自然语言说要什么，你输出**指令块**去改变程序。

## 铁律

- 改变程序的**唯一**方式就是下面这些指令块。你没有别的入口，也别指望人手工编辑文件。
- **一次只改一件事**，让诊断能指得准。改动大了可以分几轮。
- 引擎会把**带行号的诊断**回灌给你。同一个错误连改两次是浪费时间——第二次请换一种写法。
- 真源是**程序 IR 的打印件**：`//` 注释与手工排版在每次写回时都会丢。
  所以**意图写进 DESIGN.md**，不要写在程序注释里。
- 语言故意做小（无循环、无自定义函数、无条件表达式）。复杂逻辑落进 `capabilities.py`。

## 输出协议（唯一有效的输出形式）

用围栏代码块，块外的文字是给人看的说明。

1. **增量改程序**（最常用）：
   ```puppet
   add #content text #title text="你好"
   on #go click:
       set #title text="已点击"
   ```
2. **整体替换程序**（仅在"推倒重来"时；会先自动存档、再**等人确认**）：
   ```puppet-replace
   <完整的新程序>
   ```
3. **写程序文件**（白名单：`capabilities.py` · `assets/**` · `DESIGN.md`；写前自动存档）：
   ```write capabilities.py
   <文件内容>
   ```
4. **反问**（分叉大到"猜错就做成另一个 app"时才用；反问期间**不会**有任何写入）：
   ```ask
   {"question": "要做待办列表还是看板？", "options": ["待办列表", "看板"], "default": "待办列表"}
   ```
5. **记住 / 忘掉**（运行期记忆：它是**状态**，写它不改变程序，但会影响你以后每一轮）：
   ```remember
   {"text": "用户偏好中文界面", "importance": 2, "tags": ["偏好"]}
   ```
   ```forget
   m3
   ```
   `importance` 取 1–3（默认 1）；超上限时先丢**最不重要、最久未访问**的。
   注意：记忆**人可以一键清空**，所以别把关键约定只存在记忆里——**意图要写进 `DESIGN.md`**。
6. **融合另一个 app**（两段式；推倒重来级确认式变更，执行前**等人确认**）：
   ```fuse
   {"b": "../beta"}
   ```
   意向块：只声明要融合谁。下一轮上下文会出现 **B 的结构摘要**（地址清单 /
   能力签名 / 契约）——基于摘要出方案：
   ```fuse
   {"b": "../beta", "renames": {"head": "beta_head"}, "caps": {"b_note": "keep"},
    "window_title": "a", "intent": "为什么这么合的一段话"}
   ```
   方案块字段：`renames`（解决撞名）· `caps`（B 能力的 keep / drop / rename:<新名>）·
   `window_title`（"a" 归 A / "b" 用 B 的 / 自定义字符串）· `intent`（你的理由，进决策流水）。
   **没有 drops**：B 的内容全部并入，"不想要"就用 `del` 命令批删——逐条可见可回滚。
   方案会先过机制体检（撞名 / 能力 / 多窗口 / 干跑），被拒的诊断会回灌给你修正。
   取消：`{"cancel": true}`。
7. **协作消息**（给同一 hub 网络里的其他 agent app 发消息；对方只是被**通知**，
   你永远不能写对方的程序——协作 = 说话与调用对方借出的能力，不是改它）：
   ```tell 待办 status
   我这边把提醒时间提前了 10 分钟。
   ```
   （`tell <对方app名> <topic>`，正文任意行。未接入总线时会得到可见失败——那就别再试。）
8. **设定目标**（给自主回路定方向；写进决策流水，跨会话可见）：
   ```goal
   让"完成率"卡片始终反映当天数据；发现数据源断更就先修复数据源。
   ```
9. 其它文字 = 说明。**没有指令块就等于本轮什么都不改**，所以别把代码写在说明里。

## 语言速查

- 动词：`add`（增节点）· `set`（改属性，或改状态标志 `visible`/`disabled`）· `del` · `move` · `upsert` · `data`（声明数据源）· `on <目标> <事件>:`（处理器，动作体可缩进多行）· `listen` · `call`（调能力 → 槽）· 探针（`tree`/`get`/`where`）。
- 集合原语：`append` · `remove` · `remove_where … as r where …` · `update_where … as r set …` · `clear` · `sort`。
- 属性组：盒模型 `pad margin bgcolor gradient radius border shadow opacity` · 布局 `gap justify align wrap flex scroll w h x y offset scale rotate` · 排版 `fg size weight italic font tooltip` · 内容 `text icon src fit initials value selected min max step placeholder title primary` · 引用 `source template options` · 元 `states animate duration curve`。
- 状态标志（可读可写）：`hover focus pressed error visible disabled`。
- 表达式只出现在属性值 / `when` / 动作参数 / `data` 初值里；引用是 `#id.attr`，模板行内用绑定名（如 `t.text`）。
- 模板：`add #root template #tpl as t` + 在模板内写行内容，再用 `add #lst list #L source=#tpl_data template=#tpl` 使用它。
- 消息：`#id.value`（输入/选择/进度）· `#id.selected`（下拉/分页）· `#slot.status` / `#slot.value`（能力槽）· `count(#data)`。

## 观测能力（渲染器自述——**诚实降级必须让你看见**）

{observation}

## 词汇边界（只能用这些）

{controls}

属性：{attributes}

动效：{animations}

图标：{icons}

写别的会降级（界面上少一块），而降级是你自己引入的返工。
"""


class DefaultPrompt:
    def __init__(self, api=None):
        self.api = api

    def rewrite(self, context: dict) -> dict:
        vocabulary = context.get("vocabulary") or {}
        # 用 replace 而不是 format：提示词里含 `{"question": …}` 这样的示例，`{}` 不是占位符。
        system = SYSTEM
        for token, value in (
                ("{controls}", "控件：" + "、".join(vocabulary.get("controls") or [])),
                ("{attributes}", "、".join(vocabulary.get("attributes") or [])),
                ("{animations}", "、".join(vocabulary.get("animations") or [])),
                ("{icons}", "、".join(vocabulary.get("icons") or [])),
                ("{observation}", self._observation_block(context.get("observation")))):
            system = system.replace(token, value)
        messages = []
        for turn in context.get("turns") or []:
            messages.append({"role": turn.get("role", "user"),
                             "content": turn.get("text", "")})
        messages.append({"role": "user", "content": self._request(context)})
        return {"system": system, "messages": messages}

    # ------------------------------------------------------------ 本轮的固定层

    def _request(self, context: dict) -> str:
        blocks = ["【模式】%s" % context.get("mode", "执行")]
        if context.get("omitted"):
            # 超限时**显式声明省略**，绝不静默截断：让它知道自己看不到什么。
            blocks.append("【已省略（超出上下文上限，未注入）】\n- "
                          + "\n- ".join(context["omitted"]))
        blocks.append("【当前程序（真源全文；诊断里的行号就是这里的行号）】\n```\n%s\n```"
                      % (context.get("source") or "（空）"))
        if context.get("design"):
            blocks.append("【DESIGN.md（意图的唯一载体）】\n%s" % context["design"])
        if context.get("memory"):
            blocks.append("【运行期记忆（你之前记下的；要删就用 forget 块报 id）】\n%s"
                          % context["memory"])
        if context.get("fusion_brief"):
            blocks.append("【融合对象的结构摘要（有正在进行的融合意图；基于它出 fuse 方案块。"
                          "摘要是清单级——不含属性值全文，方案字段不依赖它们）】\n%s"
                          % context["fusion_brief"])
        blocks.append("【本 app 的能力】\n" + self._catalog_block(context.get("catalog")))
        blocks.append("【assets/ 里可用的文件】\n" + self._assets_block(context.get("assets")))
        diagnostics = context.get("diagnostics") or []
        if diagnostics:
            blocks.append("【上一轮以来的诊断（先修这些）】\n" + self._diag_block(diagnostics))
        for section in context.get("spec") or []:
            blocks.append("【规范片段 · %s】\n%s" % (section["title"], section["text"]))
        if context.get("stuck"):
            # 卡住时**换一种表述**，而不是把同一句再发一遍。
            blocks.append("【重复失败】%s\n请换一种完全不同的写法；若方向本身有问题，"
                          "在说明里直说并给出替代方案。" % context["stuck"])
        blocks.append("【本轮请求】\n%s" % (context.get("request") or ""))
        return "\n\n".join(blocks)

    @staticmethod
    def _observation_block(observation) -> str:
        """观测布尔 + 渲染器自述。

        措辞要**精确**：`geometry=false` 说的是"渲染器**报不出**几何（无法自证对齐/间距）"，
        不是"x/y 不生效"——x/y 在 flet 上是生效的，只是没人能核对它。把这两件事说混，
        LLM 就会错误地放弃坐标定位，或错误地相信坐标对齐已被验证。
        """
        observation = observation or {}
        lines = []
        for key, label in (("geometry", "几何观察"), ("snapshot", "视觉快照"),
                           ("interaction", "用户动作投递"), ("headless", "无 GUI 驱动")):
            value = bool(observation.get(key))
            lines.append("- %s（%s）= %s" % (key, label, "true" if value else "**false**"))
        if not observation.get("geometry"):
            lines.append("  ↑ geometry=false 只说明渲染器**报不出**几何，对齐/间距无法被核对；"
                         "x/y 仍会生效。布局尽量用 col / row / gap / flex 表达，"
                         "让**结构**承担布局语义，别把对齐押在坐标上。")
        if not observation.get("snapshot"):
            lines.append("  ↑ snapshot=false：没有视觉自检手段，界面效果只能靠你推断。")
        notes = str(observation.get("notes") or "").strip()
        if notes:
            lines.append("- 渲染器自述：%s" % notes)
        return "\n".join(lines)

    @staticmethod
    def _catalog_block(catalog) -> str:
        if not catalog:
            return "（还没有任何能力；需要外部数据或副作用时，用 write 块加一个）"
        lines = []
        for item in catalog:
            doc = (item.get("doc") or "").splitlines()
            lines.append("- %s  # %s" % (item.get("signature"), doc[0].strip() if doc else ""))
        return "\n".join(lines)

    @staticmethod
    def _assets_block(assets) -> str:
        if not assets:
            return "（空）"
        return "\n".join("- %s (%d 字节)" % (item["name"], item.get("size", 0))
                         for item in assets)

    @staticmethod
    def _diag_block(diagnostics) -> str:
        lines = []
        for diag in diagnostics[-40:]:
            where = ("第 %s 行 " % diag["line"]) if diag.get("line") else ""
            lines.append("%s%s %s: %s" % (where, diag.get("level", "info"),
                                          diag.get("code", "?"), diag.get("message", "")))
        return "\n".join(lines)


def create_prompt(api) -> DefaultPrompt:
    return DefaultPrompt(api)
