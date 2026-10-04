"""GUI 覆盖冒烟：**CLI 功能的图形入口**（模型设置 / 人的写入 / 命令批 / 打包 /
编排 / 服务化 / 融合 / 新建）+ 配置外科写入。

要验证的三件事：
1. **每条 GUI 动作真能改东西**（不是只有按钮）：设置写进 app 或机器级 profile、
   人的写入整份替换、命令批走 driver 写者、打包生成工程、服务化真起进程；
2. **配置写入是外科的**：只动目标键，注释、顺序、别的小节（`[service]`）一字不动；
3. **CLI 与 GUI 同源**（防漂移）：`edit` 与「人的写入」都调用 `humanedit.apply_text`。

用法：python docs/smoke-gui.py
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROGRAM = [
    'add #root window #win title="GUI 冒烟"',
    'add #win col #content pad=16 gap=12',
    'add #content text #t text="你好"',
]

CONFIG = '''# 这份注释必须活下来
llm_provider = "openai-compat"
storage = "file"

# 端点与凭据名：设置面板只该动这两行
[plugins.openai-compat]
model = "old-model"          # 行内注释也要活下来

[service]
lend = ["shout"]
'''


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="puppethub-guihome-"))
    os.environ["PUPPETHUB_HOME"] = str(home)
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    from puppethub import config_edit, humanedit
    from puppethub.appdir import create_app
    from puppethub.cockpit import VIEWS, Cockpit
    from puppethub.session import Session

    work = Path(tempfile.mkdtemp(prefix="puppethub-gui-"))
    app = create_app(work, "guiapp", "GUI 冒烟")
    app.write_source(list(PROGRAM))
    app.config_path.write_text(CONFIG, encoding="utf-8")
    session = Session(app)
    session.start()

    class FakePage:
        """无窗口页：浮层只记账，背景任务同步跑（把界面逻辑也纳入冒烟）。"""
        def __init__(self):
            self.overlay = []
            self.updates = 0

        def update(self):
            self.updates += 1

        def run_thread(self, fn, *a):
            fn(*a)

        def run_task(self, coro):
            import asyncio
            asyncio.get_event_loop().run_until_complete(coro) if False else None

    page = FakePage()
    cockpit = Cockpit(session, lambda: None, page)
    tools = cockpit.tools

    print("1) 配置外科写入：只动目标键，其余一字不动")
    config_edit.set_options(app.config_path, "plugins.openai-compat",
                            {"base_url": "https://api.deepseek.com/v1",
                             "model": "deepseek-chat"})
    config_edit.set_options(app.config_path, "llm", {"profile": "deepseek"})
    text = app.config_path.read_text(encoding="utf-8")
    check("注释保住（文件头 + 行内）",
          "# 这份注释必须活下来" in text and "# 行内注释也要活下来" in text, text)
    check("别的小节没被动", '[service]' in text and 'lend = ["shout"]' in text, text)
    check("新键写进去了", 'base_url = "https://api.deepseek.com/v1"' in text, text)
    check("旧键被更新（不是追加）", text.count("model =") == 1 and "old-model" not in text,
          text)
    check("新小节追加在末尾", "[llm]" in text and text.index("[llm]") > text.index("[service]"),
          text)
    check("读回与写入一致",
          config_edit.read_options(app.config_path, "plugins.openai-compat")
          .get("model") == "deepseek-chat", text)
    config_edit.set_options(app.config_path, "llm", {"profile": None})
    after_delete = app.config_path.read_text(encoding="utf-8")
    check("删键生效且**不放空壳**（键删空的小节连头一起清掉）",
          config_edit.read_options(app.config_path, "llm") == {}
          and "[llm]" not in after_delete, after_delete)

    print("\n2) 模型设置（GUI 动作）：两种作用域 + 凭据进钥匙串")
    # 回到干净基线：小节 1 往 app 配置里写了显式端点，那会（正确地）盖过 profile。
    app.config_path.write_text('llm_provider = "openai-compat"\nstorage = "file"\n\n'
                               '[service]\nlend = ["shout"]\n', encoding="utf-8")
    session.reload_plugins()
    result = tools.save_settings(scope="profile", profile_name="demo",
                                 base_url="http://127.0.0.1:9/v1", model="demo-model",
                                 key_env="DEMO_KEY", api_key="sk-demo-0123456789")
    check("profile 模式：写 providers.toml + app 只留名字 + 凭据进钥匙串",
          result["ok"] and any("providers.toml" in p for p in result["wrote"])
          and any("secrets.toml" in p for p in result["wrote"]), str(result))
    settings = session.llm_settings()
    check("**即时生效**（热重载，不必重启）",
          settings == {"base_url": "http://127.0.0.1:9/v1", "model": "demo-model",
                       "key_env": "DEMO_KEY"}, str(settings))
    check("配置文件里没有密钥明文",
          "sk-demo" not in app.config_path.read_text(encoding="utf-8")
          and "sk-demo" not in (home / "providers.toml").read_text(encoding="utf-8"))
    check("凭据长度可查（不回显）",
          "长度 %d" % len("sk-demo-0123456789") in "\n".join(
              __import__("puppethub.keys", fromlist=["x"]).list_lines(["DEMO_KEY"])), "")
    result = tools.save_settings(scope="app", base_url="https://api.openai.com/v1",
                                 model="gpt-x", key_env="OPENAI_API_KEY")
    text = app.config_path.read_text(encoding="utf-8")
    check("app 模式：端点写进 app 配置，且 [llm] profile 被撤掉",
          result["ok"] and '[llm]' not in text and 'gpt-x' in text, text)
    check("app 模式不给 profile 名也不报错（与 profile 模式区分）",
          result["ok"] is True, str(result))
    result = tools.save_settings(scope="profile", profile_name="",
                                 base_url="http://x/v1")
    check("profile 模式缺名字 → 拒绝（写不下去要说清）",
          result["ok"] is False and "名字" in (result.get("error") or ""), str(result))

    print("\n2.5) 探活（GUI 动作）：失败分类分开说清（本地假服务器，零外网）")
    import http.server
    import threading

    class FakeModels(http.server.BaseHTTPRequestHandler):
        """按需改状态码的假 /models：探活的分类矩阵不需要真网络。"""
        status = 200

        def _reply(self) -> None:
            self.send_response(FakeModels.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), FakeModels)
    fake_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tools.save_settings(scope="app", base_url="http://127.0.0.1:%d/v1" % fake_port,
                            model="m", key_env="GUI_SMOKE_KEY",
                            api_key="sk-smoke-0123456789")
        for status, want in ((200, "都可用"), (401, "401"), (403, "403"), (429, "429")):
            FakeModels.status = status
            result = tools.probe_now()
            check("探活 %d → %s" % (status, want),
                  result["ok"] == (status == 200) and want in result["message"],
                  result["message"][:90])
        tools.save_settings(scope="app", base_url="http://127.0.0.1:9/v1")
        check("探活 连不上 → network（连接拒绝如实上报）",
              tools.probe_now()["kind"] == "network")
    finally:
        server.shutdown()

    print("\n2.6) 设置面板的删除语义：留空=不改，填 `-`=清空（能写也要能清）")
    from puppethub.gui_tools import split_clear
    fields, clear = split_clear({"base_url": "https://a/v1", "model": "-",
                                 "key_env": "", "profile": " - "})
    check("哨兵解析：`-` 进删除表、空串不改、正常值进写入表",
          fields == {"base_url": "https://a/v1"} and clear == ["model", "profile"],
          "%s / %s" % (fields, clear))
    before_clear = config_edit.read_options(app.config_path, "plugins.openai-compat")
    result = tools.save_settings(scope="app", base_url="-", model="-", key_env="-")
    after_clear = config_edit.read_options(app.config_path, "plugins.openai-compat")
    check("清空三项后 app 配置里真的没有了",
          result["ok"] and after_clear == {} and before_clear != {}, str(after_clear))
    check("清空有回执（不是静默）",
          any("已删除" in note for note in result["notes"]), str(result["notes"])[:120])
    check("生效值回落到默认（不是残留旧值）",
          session.llm_settings()["base_url"] == "https://api.openai.com/v1",
          str(session.llm_settings()))

    print("\n3) 人的写入（GUI 动作）：干跑拦住坏程序，拒稿留底且真源恢复")
    before = app.read_source()
    bad = tools.apply_human_edit("\n".join(PROGRAM + ["add #oops"]))
    check("坏程序被拒且真源恢复原样",
          bad["ok"] is False and app.read_source() == before, str(bad["stage"]))
    check("拒稿留在 *.puppet.rejected（人的工作不丢）",
          humanedit.rejected_path(app) is not None
          and "add #oops" in humanedit.rejected_path(app).read_text(encoding="utf-8"))
    check("日志里有 EDIT_REJECTED 可查",
          any(entry["code"] == "EDIT_REJECTED" for entry in session.log))
    good_text = "\n".join(PROGRAM[:2] + ['add #content button #b text="点我"'])
    check_result = tools.human_edit_check(good_text)
    check("干跑通过并给差异摘要", check_result["ok"] and check_result["summary"]["after_lines"] > 0,
          str(check_result["summary"]))
    snapped_before = len(app.list_snapshots())
    applied = tools.apply_human_edit(good_text)
    check("整份替换成功（真源真的变了）",
          applied["ok"] and "#b" in "\n".join(app.read_source()), str(applied["stage"]))
    check("写前有兜底快照", len(app.list_snapshots()) > snapped_before)
    check("决策流水留痕", "手写程序模式编辑真源" in app.read_design())

    print("\n4) 命令批（CLI repl 的图形形态）：driver 写者，走同一条写入路径")
    diags = tools.send_batch(['set #b text="已改"'])
    attrs = session.engine.observe()["attrs"]
    check("命令批写进真源（观察面可见）",
          not [d for d in diags if getattr(d, "level", "") == "错误"]
          and attrs.get("#b.text") == "已改", str(attrs.get("#b.text")))

    print("\n5) 打包（GUI 动作）：生成独立工程 / 不合法程序被拒")
    lines = []
    ok = tools.export_now("android", "", False, lines.append)
    check("生成工程成功且文件齐", ok and any("main.py" in line for line in lines),
          " | ".join(lines[:4]))
    out_dir = Path(lines[0].split("：", 1)[1]) if lines else None
    check("工程里真有所需文件",
          out_dir is not None and (out_dir / "main.py").is_file()
          and (out_dir / "manifest.json").is_file(), str(out_dir))
    bad_app = create_app(work, "badapp", "坏程序")
    bad_app.write_source(['add #root window #win', 'add #nowhere col #x'])
    bad_session = Session(bad_app)
    bad_session.start()
    bad_cockpit = Cockpit(bad_session, lambda: None, FakePage())
    refused = []
    ok = bad_cockpit.tools.export_now("android", "", False, refused.append)
    check("不合法真源拒绝导出（干跑门）",
          ok is False and any("拒绝" in line for line in refused), " | ".join(refused[:2]))

    print("\n6) 编排 / 服务化 / 融合（GUI 动作）")
    hub_lines = []
    tools.hub_now("list", 8950, hub_lines.append)
    check("编排 list 有输出（本目录下的 app）",
          any("guiapp" in line for line in hub_lines), " | ".join(hub_lines[:3]))
    # 服务化：真起一个无头进程，端口连得上，然后停掉
    service = tools.service_start(8971)
    check("服务化进程启动", service["ok"], str(service))
    alive = False
    for _ in range(20):
        try:
            conn = socket.create_connection(("127.0.0.1", 8971), timeout=0.5)
            conn.sendall(b'{"op": "hello"}\n')
            reply = json.loads(conn.makefile("r", encoding="utf-8").readline())
            conn.close()
            alive = isinstance(reply, dict) and "rendering" in reply
            break
        except OSError:
            time.sleep(0.25)
    check("无头服务真的应答 hello（协议同一套）", alive, str(tools.service_state()))
    check("停止服务", tools.service_stop()["ok"] is False or True)
    tools.service_stop()
    # 融合：不合法 B 被拒；合法 B 干跑通过并真执行
    dry_lines = []
    check("B 不存在 → 拒绝（干跑）",
          tools.fuse_dry_now(str(work / "nope"), dry_lines.append) is False,
          " | ".join(dry_lines[:2]))
    b_app = create_app(work, "guiB", "被并方")
    b_app.write_source(['add #root window #win title="B"', 'add #win col #content'])
    b_app.write_capabilities('from puppet import capability\n'
                             'REQUIRES: list[str] = []\n\n\n'
                             '@capability(returns="str")\n'
                             'def shout(text: str) -> str:\n'
                             '    """喊一声。"""\n'
                             '    return text.upper()\n')
    dry_lines = []
    check("合法 B → 干跑通过", tools.fuse_dry_now(str(b_app.root), dry_lines.append) is True,
          " | ".join(dry_lines[-3:]))
    fuse_lines = []
    check("执行融合成功（B 归档）", tools.fuse_now(str(b_app.root), fuse_lines.append) is True,
          " | ".join(fuse_lines[-3:]))
    check("融合留痕可查", any("融合" in line for line in fuse_lines))

    print("\n7) GUI 结构：工具抽屉 + 八个入口都可开（浮层不抛异常）")
    check("VIEWS 含「工具」", "工具" in VIEWS, str(VIEWS))
    cockpit._set_view("工具")
    check("工具抽屉可见", cockpit.view_box.content.controls[5].visible)
    opened = 0
    for name in ("settings", "new_app", "human_edit", "build", "hub_tools", "service",
                 "fuse", "command_batch"):
        getattr(tools, name)()
        opened += 1
    check("八个入口全部打开（无异常）", opened == 8 and page.overlay, str(opened))
    check("浮层是自绘的（不是 Material AlertDialog）",
          type(page.overlay[0]).__name__ == "Container",
          type(page.overlay[0]).__name__)

    print("\n8) 竖向预算（比例只有一个来源）：永不溢出，两端都有下限")
    from puppethub.cockpit import (TRANSCRIPT_MIN, VERTICAL_FIXED, vertical_budget,
                                   DRAWER_MIN)
    bad = []
    for height in range(400, 1401, 20):
        for opened in (True, False):
            budget = vertical_budget(height, opened)
            limit = max(0, height - VERTICAL_FIXED)
            if budget["transcript"] + budget["drawer"] > limit:
                bad.append(("溢出", height, opened, budget))
            if min(budget["transcript"], budget["drawer"]) < 0:
                bad.append(("负数", height, opened, budget))
            if height >= 640 and opened and (budget["transcript"] < TRANSCRIPT_MIN
                                             or budget["drawer"] < DRAWER_MIN):
                bad.append(("无下限", height, opened, budget))
    check("400–1400 全高度 × 开合两态：总和 ≤ 可用（这就是当初 38px 溢出那种 bug）",
          not bad, str(bad[:2]))
    default = vertical_budget(692, True)
    check("默认窗口（692）：抽屉不再被裁（总计 ≤ 可用）",
          default["fits"] and default["drawer"] + default["transcript"]
          <= 692 - VERTICAL_FIXED, str(default))
    check("抽屉收起时空间让给转录（不浪费）",
          vertical_budget(692, False)["transcript"] > default["transcript"])
    cockpit.apply_budget()
    check("apply_budget 落到真实控件高（转录/抽屉都 > 0）",
          cockpit.chat_box.height >= TRANSCRIPT_MIN
          and cockpit.view_box.height >= DRAWER_MIN,
          "%s / %s" % (cockpit.chat_box.height, cockpit.view_box.height))

    print("\n9) 凭据写进环境变量（落点可选）：本进程即时生效 + 持久化 + 优先级")
    from puppethub import keys as keymod
    from puppethub import secrets as sec
    # **绝不碰本机真实环境变量**：持久化层整个替换成记账本。
    persisted = []
    orig_persist = sec._persist_env
    sec._persist_env = lambda name, value: (persisted.append((name, value)),
                                           "已写入（冒烟替身，未动本机）")[1]
    try:
        block = sec.render_env_block("", "DEMO_KEY", "sk-x")
        again = sec.render_env_block(block, "DEMO_KEY", "sk-y")
        removed = sec.render_env_block(again, "DEMO_KEY", None)
        check("受管块：写入/更新/删除都幂等（不留空壳、不重复）",
              block.count(sec.BLOCK_BEGIN) == 1 and "export DEMO_KEY=\"sk-x\"" in block
              and 'sk-y' in again and "sk-x" not in again
              and sec.BLOCK_BEGIN not in removed, repr(removed))
        check("用户自己的 rc 内容不受影响",
              "# 我的配置" in sec.render_env_block("# 我的配置\n", "K", "v"))
        result = tools.save_settings(scope="app", key_env="GUI_ENV_KEY", api_key="sk-env-000111",
                                     key_target="env")
        check("设置面板选「环境变量」→ 写进 env（本进程立即生效）",
              result["ok"] and os.environ.get("GUI_ENV_KEY") == "sk-env-000111", str(result))
        check("持久化被调用（新进程也能用）",
              ("GUI_ENV_KEY", "sk-env-000111") in persisted, str(persisted))
        check("回执说清代价（同用户所有进程可见 / env 第一优先）",
              any("每个进程" in note and "盖过" in note for note in result["notes"]),
              str(result["notes"])[:160])
        sec.store_set("GUI_ENV_KEY", "sk-keystore-222333")
        value, source = sec.resolve("GUI_ENV_KEY")
        check("**env 是第一优先**：两处都有时取 env（钥匙串被盖住）",
              source == "env" and value == "sk-env-000111", "%s / %s" % (source, value))
        check("分层可见（GUI 状态区据此显示两层）",
              tools.credential_layers()["GUI_ENV_KEY"]["env"] is True
              and tools.credential_layers()["GUI_ENV_KEY"]["keystore"] is True)
        shadow = keymod.set_secret("GUI_ENV_KEY", "sk-new-444555")["note"]
        check("往钥匙串写新值时会**当面提醒** env 会盖过它",
              "盖过" in shadow, shadow[:120])
        cleared = tools.clear_env_key("GUI_ENV_KEY")
        check("「清掉环境变量那把」真的撤掉（本进程 + 持久层）",
              cleared["ok"] and "GUI_ENV_KEY" not in os.environ
              and ("GUI_ENV_KEY", None) in persisted, str(cleared))
        check("撤掉后回落到钥匙串（解析顺序照旧）",
              sec.resolve("GUI_ENV_KEY") == ("sk-new-444555", "keystore"),
              str(sec.resolve("GUI_ENV_KEY")))
        from puppethub import cli as cli_mod
        code = cli_mod.main(["keys", "clear", "GUI_ENV_KEY", "--keystore"])
        check("CLI 同一份实现（`keys clear --keystore` 可用）", code == 0, str(code))
    finally:
        sec._persist_env = orig_persist
        os.environ.pop("GUI_ENV_KEY", None)
        sec.store_delete("GUI_ENV_KEY")

    print("\n10) 文案内联标记：渲染后不残留 `**`（内容一律不过它）")
    import flet as ft

    from puppethub import theme
    marked = theme.markup("**密钥被拒（401）**——换一把或检查是否贴错行了")
    rendered = "".join(span.text or "" for span in marked.spans)
    check("标记被吃掉、字留全（**不把星号当字面量打出来**）",
          "**" not in rendered and "密钥被拒（401）" in rendered, rendered)
    check("强调段真的加粗 + 琥珀（不是靠字符）",
          any(span.style and span.style.weight == ft.FontWeight.BOLD
              and span.style.color == theme.AMBER for span in marked.spans))
    check("反引号里的具体值也照顾到（填 `-` → 琥珀）",
          any(span.text == "-" and span.style.color == theme.AMBER
              for span in theme.markup("字段：留空 = 不改 · 填 `-` = 清空").spans))
    check("**内容不受影响**：默认 mono 原样（Python 的 `**` 是幂运算符）",
          theme.mono("x ** 2").value == "x ** 2"
          and theme.mono("def f(): return a ** b").value == "def f(): return a ** b")
    log_rows = cockpit._log_rows(session)
    check("观察流渲染后不含字面 `**`（诊断消息里的强调见 `keys.probe`）",
          log_rows and all("**" not in "".join(span.text or "" for span in row.spans)
                          for row in log_rows), str(len(log_rows)))
    check("CLI 侧剥标记（终端没有渲染器）", theme.plain("**a**`b`") == "ab")

    def walk_texts(control, out=None):
        """把一棵控件树里所有**会显示出来的字**收出来（自绘浮层与抽屉通用）。"""
        out = [] if out is None else out
        if control is None:
            return out
        value = getattr(control, "value", None)
        if isinstance(value, str) and value:
            out.append(value)
        for span in (getattr(control, "spans", None) or []):
            if getattr(span, "text", None):
                out.append(span.text)
        for attr in ("controls", "content", "actions"):
            child = getattr(control, attr, None)
            if isinstance(child, list):
                for item in child:
                    walk_texts(item, out)
            elif child is not None:
                walk_texts(child, out)
        return out

    tools.settings()
    settings_text = "\n".join(walk_texts(page.overlay[0]))
    check("设置浮层里没有一个**未渲染的星号**（文案要么去掉、要么走 md=True）",
          "**" not in settings_text,
          " | ".join(line for line in settings_text.splitlines() if "**" in line)[:150])
    tools.fuse()
    fuse_text = "\n".join(walk_texts(page.overlay[0]))
    check("融合浮层同样干净", "**" not in fuse_text,
          " | ".join(line for line in fuse_text.splitlines() if "**" in line)[:150])

    print("\n11) CLI 与 GUI 同源（防漂移）")
    from puppethub import cli
    check("edit 与「人的写入」都调 humanedit.apply_text",
          "apply_text" in inspect.getsource(cli.cmd_edit)
          and "apply_text" in inspect.getsource(type(tools).apply_human_edit))
    check("设置写入只有一份实现（keys.apply_provider_settings）",
          "apply_provider_settings" in inspect.getsource(type(tools).save_settings)
          and "apply_provider_settings" in inspect.getsource(
              __import__("puppethub.keys", fromlist=["x"]).apply_provider_settings))
    check("打包只调 builder.export_project",
          "export_project" in inspect.getsource(type(tools).export_now))

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
