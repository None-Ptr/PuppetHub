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

SYSTEM = """你在开发一个 app：人（或它自己）用自然语言说要什么，你输出**指令块**去改变程序。

> **你的身份由每轮上下文开头的【身份】块给出**（共作者 / 当值者），两者共享这套知识与
> 指令块协议，区别只在**处境**：有没有人在场、能不能等人确认。**以【身份】块为准。**

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
3. **写程序文件**（白名单：`capabilities.py` · `assets/**` · `DESIGN.md` ·
   `.puppethub/skills/*.md`；写前自动存档）：
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
9. **取技能**（自选知识）：需要某种写法（删数据 / 求和 / 调能力…）时，输出
   ```skill 名字``` 块——名字见上方【可用技能】索引。取一次全文持续注入，别重复取；
   不确定就用，比猜着写再修便宜。
10. **规划**（把目标展开成**可核验的步骤**；一步只推进一步，完成状态由**机制**按判据求值，
    **不是你自报**）：
    ```plan
    goal: 让待办能归档
    step: 给列表加归档按钮 | done: #arch.visible == true
    step: 归档后从主列表移除 | done: #list.count == 2
    ```
    `done:` 只接受 `#地址.属性 比较符 字面量`（如 `#a.b < 10`）；复杂判断先做进能力，
    再让 plan 盯能力结果。**没有 `done:` 的步骤不会被判完成**（只能由人或后续机制确认）——
    别把计划写成许愿。`drop: <某步>` 撤销做不了的步骤。计划**不增加预算**。
11. 其它文字 = 说明。**没有指令块就等于本轮什么都不改**，所以别把代码写在说明里。

{skill_index}

## 语言

完整的**语言速查**（动词 / 属性组 / 表达式与引用 / 模板 / 消息 / 三个高频坑）
在每轮的【技能 · 语言速查】块里——**写任何东西之前先看它**。

其余语言知识按需注入：命中时你会看到【技能 · 控件语义】/【技能 · 诊断速查】等块；
需要而不在时，用 ```skill 名字``` 自取（索引见上）。

{style}

## 观测能力（渲染器自述——**诚实降级必须让你看见**）

{observation}

## 你能不能看见界面

{vision}

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
                # 技能索引：常驻的"知识面"清单——LLM 据此决定自取哪份技能全文。
                ("{skill_index}", str(context.get("skill_index") or "")),
                # 风格段来自 style 槽位（宿主放进 context["style"]）：每轮常驻同一套，
                # 风格的一致性靠"常驻"而不是按需——按需会让它这轮守纪律下轮忘。
                ("{style}", str(context.get("style") or "")),
                ("{observation}", self._observation_block(context.get("observation"))),
                ("{vision}", self._vision_block(context.get("vision")))):
            system = system.replace(token, value)
        messages = []
        for turn in context.get("turns") or []:
            messages.append({"role": turn.get("role", "user"),
                             "content": turn.get("text", "")})
        messages.append({"role": "user", "content": self._request(context)})
        return {"system": system, "messages": messages}

    # ------------------------------------------------------------ 本轮的固定层

    # ------------------------------------------------------------ 身份（每轮动态）

    def _role_block(self, context: dict) -> str:
        """**身份不进 SYSTEM**——它是每轮变的处境，讲在上下文里才不会被念反。

        共享大脑（同一套知识与指令块协议），差别只有两问：**有没有人在场**、
        **能不能等人确认**。SYSTEM 写死"你是共作者"会让自主回路同时收到
        "你是共作者"和"你是当值者"两句相反的话——那不是风格问题，是提示词自相矛盾。
        """
        role = context.get("role") or "coauthor"
        if role == "operator":
            return (
                "【身份】你是这个 app 的**当值者**，**没有人在场**。\n"
                "- 你自己决定这一轮值不值得做。不值得就**只说明理由、什么都不改**——"
                "这是合法且被鼓励的结果。\n"
                "- 没人能替你确认，所以 `puppet-replace`（整体替换）与危险能力**会被直接拒绝**。"
                "别试。\n"
                "- 你可以按自己的判断改进这个 app（包括偏离当初的设计——**进化不算偏移**），"
                "但一次只做最小的一步，并说清依据。\n"
                "- 触发你的是一件具体的事。**先判断它是否值得你动**，再决定改不改。")
        return (
            "【身份】你是这个 app 的**共作者**，**人在场**并向你提出要求。\n"
            "- 人用自然语言说要什么，你把它翻译成指令块。\n"
            "- 危险动作与整体替换会**等人确认**（走 `ask` / 确认卡）；分叉大时主动反问。")

    def _request(self, context: dict) -> str:
        blocks = [self._role_block(context)]
        blocks.append("【模式】%s" % context.get("mode", "执行"))
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
        docs = context.get("capability_docs") or {}
        if docs:
            # 能力文档的按需层：只给"本轮触碰的"（程序正在调用 / 请求点名）。
            blocks.append("【触碰能力的完整说明（调它们之前先读）】\n"
                          + "\n\n".join("◆ %s\n%s" % (name, text)
                                        for name, text in sorted(docs.items())))
        blocks.append("【assets/ 里可用的文件】\n" + self._assets_block(context.get("assets")))
        diagnostics = context.get("diagnostics") or []
        if diagnostics:
            blocks.append("【上一轮以来的诊断（先修这些）】\n" + self._diag_block(diagnostics))
        for skill in context.get("skills") or []:
            blocks.append("【技能 · %s】\n%s" % (skill["name"], skill["text"]))
        for section in context.get("spec") or []:
            blocks.append("【规范片段 · %s】\n%s" % (section["title"], section["text"]))
        if context.get("stuck"):
            # 卡住时**换一种表述**，而不是把同一句再发一遍。
            blocks.append("【重复失败】%s\n请换一种完全不同的写法；若方向本身有问题，"
                          "在说明里直说并给出替代方案。" % context["stuck"])
        blocks.append("【本轮请求】\n%s" % (context.get("request") or ""))
        return "\n\n".join(blocks)

    @staticmethod
    def _vision_block(vision) -> str:
        """视觉能力的**边界自述**：能看见 / 看不见，以及看不见的原因。

        这一段存在的理由是**防赝品式自信**：渲染器声明 `snapshot=true` 只说明
        "它有截图能力"，不等于"这次对话真能收到图"（还要 provider 声明 vision）。
        不说清，LLM 会以为自己看得见，然后凭空评价外观。
        """
        vision = vision or {}
        if vision.get("can_see"):
            return ("**能看见。** 你会在改完界面后收到截图（多模态）。那是你判断外观的"
                    "唯一依据——不要凭想象评价你没有看到的东西。")
        why = str(vision.get("why_not") or "原因未知")
        return ("**看不见**（%s）。因此：**不要假装评价过外观**。"
                "你可以基于程序文本给建议（比如明显重复的颜色、缺 hover 状态），"
                "但要说明这是从代码推断的，不是看到的。" % why)

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
            return ("（还没有任何能力。需要外部数据或副作用时用 write 块写 capabilities.py："
                    "`@capability(returns=…)` + **非空 docstring** + 参数类型注解；"
                    "调用 `call 名字 with {…} into #槽`，界面绑定 `#槽.value`，"
                    "失败路径要有人接（on #槽 error）。"
                    "**纯计算不需要能力**——计数 `count(#data)`、合计 `sum(#data, \"字段\")`、"
                    "取项 at/first/last 都是内建且是活绑定；任务相关时会注入完整技能。）")
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
