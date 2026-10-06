"""`puppethub` 命令：**裸跑 = 首页**；`new` / `run` / `remote` / `repl` / `verify-ci`。

裸跑（不给子命令）打开**首页**——一个项目都没有时的那一屏，同时是社会层的宿主
（`docs/design-home.md`）。它**无条件**打开：想要某个 app 就 `puppethub run <app>`。

**防漂移约束**：CLI 与驾驶舱按钮必须调用同一份 service 层实现，CLI 只做参数解析
与输出格式化——两套入口各写一遍语义，迟早漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from puppet.ir import apply_stmt, new_program, validate
from puppet.lang import parse_program

from .appdir import AppDir, create_app
from .compat import SPEC_RANGE, DataFilesMissing, SpecMismatch
from .session import Session


def _validate_file(path: Path) -> list:
    """静态校验：解析 + IR 校验，**不起运行时**（`new` 用它确认骨架零诊断）。"""
    program = new_program()
    diags = []
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    stmts, parse_diags = parse_program(lines)
    diags += parse_diags
    for stmt in stmts:
        apply_stmt(program, stmt, diags)
    diags += validate(program)
    return diags


def _print_diags(diags, as_json: bool) -> None:
    if as_json:
        return
    if not diags:
        print("  零诊断")
        return
    for diag in diags:
        mark = {"error": "错误", "warning": "警告", "info": "信息"}.get(diag.level, diag.level)
        where = ("第 %d 行" % diag.line) if diag.line else "全局"
        print("  %s %s %s: %s" % (where, mark, diag.code, diag.message))


def _log_line(level: str, code: str, message: str) -> None:
    """CLI 形态的社会层 / 调度官日志（剥掉界面内联标记——这里没有渲染器）。"""
    from .theme import plain
    print("  [%s] %s %s" % (level, code, plain(message)), file=sys.stderr)


def cmd_home(args) -> int:
    """首页：**一个项目都没有时的那一屏**，同时是社会层的宿主。

    它**永不写任何 app 的真源**：手里只有 `SocietyOps`（白名单）与一个调度官。
    `--no-llm` = 没有调度官（社会台照常：键盘与按钮都在）。
    """
    from .home import HomeWindow
    from .orchestrator import (Orchestrator, build_provider, memory_store,
                               resolve_model)
    from .society import Society, SocietyOps

    society = Society(log=_log_line)
    model = resolve_model()
    # 调度官自己的长期记忆（`$PUPPETHUB_HOME/orchestrator/memory/`）——与 app 的记忆
    # 同一套策略。`--no-llm` 时也要装配：文件可读可改，不因没有模型就凭空消失。
    memory = memory_store(_log_line)
    ops = SocietyOps(society, log=_log_line, memory=memory,
                     config={"plugins": {"openai-compat": dict(model["options"])}})
    orchestrator = None
    if args.no_llm:
        print("首页（无调度官）：社会台照常可用（键盘与按钮都在）", file=sys.stderr)
    else:
        provider, error = None, model["error"]
        if not error:
            provider, error = build_provider(model["options"], log=_log_line)
        if model["note"]:
            print("  │ %s" % model["note"], file=sys.stderr)
        if error:
            # **降级必须可见**：没有调度官，但社会台照常。
            print("调度官不可用（社会台照常）：\n%s" % error, file=sys.stderr)
        orchestrator = Orchestrator(ops, log=_log_line, provider=provider,
                                    provider_error=error or "")
    print("首页：社会层宿主在本进程",
          file=sys.stderr)
    HomeWindow(society, ops, orchestrator).run()
    return 0


def cmd_new(args) -> int:
    parent = Path(args.dir)
    try:
        app = create_app(parent, args.name, args.title)
    except FileExistsError as ex:
        print("创建失败：%s" % ex, file=sys.stderr)
        return 1
    diags = _validate_file(app.source_path)
    from . import recent
    recent.record(app.root)            # 新建成功即入册——首页列表的真源是 MRU
    if args.json:
        print(json.dumps({"ok": not any(d.level == "error" for d in diags),
                          "app": str(app.root),
                          "diagnostics": [d.to_dict() for d in diags]},
                         ensure_ascii=False))
    else:
        print("已创建 app：%s" % app.root)
        print("骨架：window + navbar + 内容容器")
        print("静态校验：")
        _print_diags(diags, False)
        print("下一步：puppethub run %s" % app.root)
    return 1 if any(d.level == "error" for d in diags) else 0


def cmd_run(args) -> int:
    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    if args.no_llm:
        # 无 LLM 实例：写者是**驱动者**（stdio 控制面协议）。与正常实例共用同一份
        # service 层——`send` 走的就是"引擎校验 → 批末写回真源 → 写前自动快照"那条路。
        from .controlplane import main as control_main
        argv = [str(app.root)]
        if args.wipe_memory:
            argv.append("--wipe-memory")
        return control_main(argv)
    try:
        session = Session(app)
    except (SpecMismatch, DataFilesMissing) as ex:
        print(str(ex), file=sys.stderr)
        return 1
    diags = session.start()
    if args.hub_port:
        # 协作总线端口（首页/`hub` 编排时注入）：窗口实例**能说话**。
        # 配合 `--listen-port` 它还能**收投递**（`HubWindow(listen_port=)` 在窗口
        # 自己的进程里挂 TCP 绑定，见 docs/design-home.md §4.2）；只给 hub-port
        # 不给 listen-port 时才是"只发不收"。
        session.attach_hub(args.hub_port)
    if args.wipe_memory:
        # 清空运行期记忆**必须显式**：这是唯一会抹掉"经历"的动作，重置状态不碰它。
        session.wipe_memory(origin="user")
    if args.handoff:
        # 交棒：先打命名快照当锚，再切写者。**没有锚就不交棒**——否则 app 一旦
        # 进化偏了，人没有可回退的点（"偏离算进化"是好的，但它需要硬回退点兜着）。
        outcome = session.handoff_to_autonomous(origin="user", reason="命令行 --handoff")
        if not outcome.get("ok"):
            print("交棒失败：%s" % outcome.get("reason", "见观察流"),
                  file=sys.stderr)
            return 1
        if not outcome.get("already"):
            print("已交棒给自主回路（回退锚 = 快照 %s）。人随时可点「交回共作者」拿回。"
                  % outcome.get("snapshot", "?"))
    if args.json:
        print(json.dumps({"ok": True, "app": str(app.root),
                          "spec": session.spec_version,
                          "hello": session.hello(),
                          "diagnostics": [d.to_dict() for d in diags]},
                         ensure_ascii=False))
    else:
        print("运行 %s（语言 %s，需要 %s）" % (app.name, session.spec_version, SPEC_RANGE))
        print("装载诊断：")
        _print_diags(diags, False)
        print("窗口已打开。关窗 = app 结束；再次 run 会从 .puppet/ 恢复状态。")
    from .window import HubWindow
    HubWindow(session, listen_port=args.listen_port).run()
    return 0


def cmd_remote(args) -> int:
    """远程/多客户端（V2 阶段 4）：TCP 绑定，同一套协议操作，无头。"""
    from .remote import main as remote_main
    argv = [args.app, "--port", str(args.port)]
    if args.hub_port:
        argv += ["--hub-port", str(args.hub_port)]
    return remote_main(argv)


def cmd_repl(args) -> int:
    """手写程序模式（V2 阶段 6）：**人作驱动者**的交互 REPL。

    它没有绕过"单写者"——绕过的是"写者必须是 LLM"：这里写者是人自己，
    走的还是同一条命令批路径（引擎校验 → 批末写回 → 写前自动快照），
    每一行都标 origin=driver。这本来就是 `--no-llm` 控制面允许的事，
    REPL 只是给它一张有提示符的脸。
    """
    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    try:
        session = Session(app)
    except (SpecMismatch, DataFilesMissing) as ex:
        print(str(ex), file=sys.stderr)
        return 1
    diags = session.start()
    _print_diags(diags, False)
    print("REPL：输入命令批（可多行，空行结束一批）；q 退出。")

    def flush(pending: list) -> list:
        """把攒住的行作为一批应用。**退出前也必须应用**——人输入过的东西
        被静默丢弃就是"杜绝静默失败"要防的事。"""
        if pending:
            for diag in session.send(pending, origin="driver"):
                _print_diags([diag], False)
        return []

    pending_lines: list[str] = []
    noted_bom = False
    while True:
        try:
            line = input("puppet> " if not pending_lines else "  ...> ")
        except EOFError:
            pending_lines = flush(pending_lines)
            break
        if "\ufeff" in line:
            # Windows 管道（PowerShell → 原生进程）会强加 UTF-8 BOM——那是编码伪影，
            # 不是人输入的内容。剔除，但**说一声**：静默改写输入也是静默失败的一种。
            line = line.replace("\ufeff", "")
            if not noted_bom:
                print("（已剔除输入流里的 BOM——Windows 管道的编码伪影）")
                noted_bom = True
        if line.strip() in ("q", "quit", "exit"):
            pending_lines = flush(pending_lines)
            break
        if not line.strip():
            pending_lines = flush(pending_lines)
            continue
        pending_lines.append(line)
    session.retry_storage()
    return 0


def cmd_keys(args) -> int:
    """凭据：清单 / 录入 / 探活与泄漏自检（V5）。

    与驾驶舱里的 `密钥` / `探活` 共用同一份实现（`keys.py`）——两处各写一遍
    语义，迟早漂移。
    """
    from . import keys as _keys

    def open_session():
        if not args.app:
            return None
        app = AppDir(args.app)
        if not app.exists():
            print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
            return None
        session = Session(app)
        session.start()
        return session

    if args.action == "set":
        if not args.name:
            print("用法：puppethub keys set <变量名> [--env]", file=sys.stderr)
            return 1
        import getpass
        value = getpass.getpass("粘贴 %s 的值（不回显）：" % args.name).strip()
        if not value:
            print("值为空，未写入。", file=sys.stderr)
            return 1
        outcome = _keys.set_secret(args.name, value,
                                   target="env" if args.env else "keystore")
        if not outcome.get("ok"):
            print(outcome.get("error") or "写入失败", file=sys.stderr)
            return 1
        print(outcome["note"])
        return 0

    if args.action == "clear":
        if not args.name:
            print("用法：puppethub keys clear <变量名> [--keystore]", file=sys.stderr)
            return 1
        outcome = _keys.clear_secret(args.name,
                                     target="keystore" if args.keystore else "env")
        print(outcome["note"])
        return 0

    if args.action == "list":
        session = open_session()
        names = session.credential_names() if session else None
        for line in _keys.list_lines(names):
            print("  " + line)
        if session:
            print("  （本 app 需要：%s）" % "、".join(names))
        print("  钥匙串：%s"
              % _keys.secrets.secrets_path())
        return 0

    session = open_session()
    if session is None:
        print("check 需要 --app（要读它的 provider 配置才知道探谁）", file=sys.stderr)
        return 1
    result = session.check_credentials()
    # 终端没有渲染器：文案里的内联标记（`**重点**`）要剥掉，否则就是一堆星号。
    print(_keys.plain(result["text"]))
    return 0 if result["ok"] else 1


def cmd_verify_ci(args) -> int:
    """CI 模式（V2 阶段 6）：自证跑完以退出码说话，**不进窗口**。

    "自动化 / CI"在 V1 就留了内部接口（verify.run_conformance 本来就不 print、
    只回行）——这里只是把退出码接出来：0 = 全绿，1 = 有 FAIL 或环境不齐。
    """
    from .verify import run_conformance
    result = run_conformance(filter_text=args.filter or "")
    if args.json:
        print(json.dumps({key: result.get(key) for key in
                          ("ok", "returncode", "summary", "total")},
                         ensure_ascii=False))
    else:
        lines = result.get("lines", [])
        for line in lines:
            # 全绿时只给人看汇总；**有失败就把全部输出摊开**——CI 里只看 "FAIL"
            # 三行根本查不了原因。
            if result.get("ok") or line.strip().startswith("FAIL") or "通过" in line:
                print(line)
    if not result.get("ok"):
        print("自证未通过：%s" % (result.get("error") or result.get("summary")),
              file=sys.stderr)
        return 1
    return 0


def cmd_hub(args) -> int:
    """多 app 编排（V3 提前落地的最小形态）：生命周期管理，不制造第二个写者。"""
    from .hub import main as hub_main
    return hub_main([args.dir, args.action, "--base-port", str(args.base_port)])


def cmd_fuse(args) -> int:
    """融合（V3）：把 B 并入 A。**确认式系统动作**——机制对 plan 做体检
    （依赖对照 / id 审计 / 干跑），执行前**总是打印 plan 全文与审计结果**；
    `--plan plan.json` 可注入完整方案（对话形态的 LLM 出的 plan 也可以落成文件走这里）。"""
    import json as _json
    from .fusion import audit_plan, default_plan, fuse
    a, b = AppDir(args.a), AppDir(args.b)
    for app, label in ((a, "A"), (b, "B")):
        if not app.exists():
            print("%s 不是 app 目录（缺少 app.puppet）：%s" % (label, app.root),
                  file=sys.stderr)
            return 1

    plan = default_plan(a, b)
    if args.plan:
        try:
            with open(args.plan, "r", encoding="utf-8") as fh:
                loaded = _json.load(fh)
        except (OSError, ValueError) as ex:
            print("plan 文件读取失败（%s）：%s" % (args.plan, ex), file=sys.stderr)
            return 1
        if not isinstance(loaded, dict):
            print("plan 文件必须是 JSON 对象", file=sys.stderr)
            return 1
        plan.update(loaded)
    plan["b"] = str(b.root)

    report = audit_plan(a, b, plan)
    print("=== 融合 plan（将做什么） ===")
    print("renames:", _json.dumps(plan.get("renames") or {}, ensure_ascii=False))
    print("caps:   ", _json.dumps(plan.get("caps") or {}, ensure_ascii=False))
    print("window_title:", plan.get("window_title"))
    print("intent: ", plan.get("intent"))
    print("依赖对照：A〔%s〕← B〔%s〕" % (report["deps"]["a"] or "（未填）",
                                       report["deps"]["b"] or "（未填）"))
    for error in report["errors"]:
        print("  体检拒绝：%s" % error, file=sys.stderr)
    if not report["ok"]:
        return 1
    print("=== 审计通过 ===")
    if not args.yes:
        print("dry-run：以上是融合方案。确认无误加 --yes 执行。")
        return 0
    result = fuse(a, b, plan)
    if not result["ok"]:
        print("融合在 %s 阶段停下：%s" % (result["stage"],
              (result.get("errors") or ["见上"])[0]), file=sys.stderr)
        return 1
    print("融合完成：合并后 %d 行；B 已归档为 %s" % (result["merged_lines"],
                                                 result["archive"]))
    return 0


def cmd_edit(args) -> int:
    """正式手写程序模式（V3）：人作驱动者，**编辑器即输入法**。

    打开 `$EDITOR`（缺省 notepad）编辑 `app.puppet`，保存退出后：
    内容有变 → 确认 → 兜底快照 → 整份替换 → 重载。**绕过对话，不绕过纪律**。

    干跑校验 / 拒稿留底 / 兜底快照 / 决策流水全在 `humanedit.py` 里——驾驶舱的
    「人的写入」走**同一份实现**（两处各写一遍语义，迟早漂移）。
    """
    import os
    import subprocess
    from .humanedit import apply_text
    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    try:
        session = Session(app)
    except (SpecMismatch, DataFilesMissing) as ex:
        print(str(ex), file=sys.stderr)
        return 1
    session.start()
    before = "\n".join(app.read_source())
    editor = os.environ.get("EDITOR") or "notepad"
    subprocess.run([editor, str(app.source_path)], check=False)
    after = "\n".join(app.read_source())
    if after == before:
        print("内容未变化：什么都不做。")
        return 0
    if args.yes or input("内容已变化，确认整份替换并重载？[y/N] ").strip().lower() in ("y", "yes"):
        # **基线必须是编辑器打开前的那份**：此刻真源已被编辑器写脏，现读等于
        # 把脏内容当"原样"（`smoke-edit` 第 3 节逮过这个回归）。
        result = apply_text(app, session, after, restore_to=before.splitlines())
        if not result["ok"]:
            print("编辑后的程序没有通过静态校验，真源已恢复原样；"
                  "你的编辑留在了 %s" % os.path.basename(result["rejected"]),
                  file=sys.stderr)
            for err in result["errors"]:
                print("  %s %s: %s" % (err["level"], err["code"], err["message"]),
                      file=sys.stderr)
            return 1
        _print_diags(result["diagnostics"], False)
        print("已应用并重载（可回滚）。")
        return 0
    print("已取消：文件保持你编辑后的样子，但程序未采用它"
          "")
    return 0


def cmd_build(args) -> int:
    """设备打包（V3）：生成独立 flet 工程（纯 app 实例；写者 = 无）。

    生成可本机验证；`--run-flet-build` 才调用 `flet build`（需要 flet CLI 与
    Android SDK / 构建环境）——没有验证环境不等于不能尝试，但失败必须如实上报。
    """
    from .builder import export_project, run_flet_build
    app = AppDir(args.app)
    if not app.exists():
        print("不是 app 目录（缺少 app.puppet）：%s" % app.root, file=sys.stderr)
        return 1
    result = export_project(app, args.target,
                            Path(args.out) if args.out else None)
    if not result["ok"]:
        for error in result.get("errors") or []:
            print(error, file=sys.stderr)
        return 1
    print("已生成独立 flet 工程：%s" % result["out_dir"])
    for rel in result["files"]:
        print("  │ %s" % rel)
    for note in result.get("notes") or []:
        print("  ⚠ %s" % note)
    print("手机上没有写者：改程序回桌面改 app.puppet 重新 build。")
    if args.run_flet_build:
        outcome = run_flet_build(Path(result["out_dir"]), args.target)
        if outcome.get("output"):
            print(outcome["output"])
        if not outcome["ok"]:
            print(outcome.get("error") or "flet build 失败", file=sys.stderr)
            return 1
        print("构建完成。")
    else:
        print("构建：cd \"%s\" && flet build %s（或加 --run-flet-build 让本命令代跑；"
              "需要 flet CLI 与 Android SDK）" % (result["out_dir"], args.target))
    return 0


def main(argv=None) -> int:
    # GBK 控制台兜底：⚠/│ 这类装饰符 GBK 编不了会让整个命令炸掉——
    # 编不了的字符替换成 ? ，信息主体（中文）不受影响。绝不静默，但也不炸。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    # **冻结态垫片**（PyInstaller）：打包后本 exe 就是机器上唯一的"python 替身"，
    # 而自证链会递归调它两次——verify 用 `exe runner.py …` 拉运行器、runner 用
    # `exe -m 模块` 拉被测实现。两个垫片让这条链在没有 Python 的机器上依然成立；
    # 正常子命令（new/run/…）不经过它们。
    if argv[:1] == ["-m"]:
        import runpy
        old_argv = sys.argv
        sys.argv = [argv[1]] + argv[2:]      # 模块把自己的 main 从 sys.argv 解析参数
        try:
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        except SystemExit as ex:  # 模块的退出码就是本进程的退出码
            return int(ex.code or 0)
        finally:
            sys.argv = old_argv
        return 0
    if argv and argv[0].endswith(".py") and os.path.isfile(argv[0]):
        import runpy
        old_argv = sys.argv
        sys.argv = argv                       # 脚本期望 argv[0] 是自己
        try:
            runpy.run_path(argv[0], run_name="__main__")
        except SystemExit as ex:
            return int(ex.code or 0)
        finally:
            sys.argv = old_argv
        return 0

    ap = argparse.ArgumentParser(prog="puppethub",
                                 description="OpenPuppet 的搭建与运行软件")
    ap.add_argument("--json", action="store_true", help="输出机读结果（诊断也走这里）")
    ap.add_argument("--no-llm", action="store_true", default=None,
                    help="无 LLM：首页只开社会台；`run` 等同控制面（写者=驱动者）")
    # 不 required：**裸跑就是首页**（一个项目都没有时的那一屏）。
    sub = ap.add_subparsers(dest="cmd", required=False)

    p_new = sub.add_parser("new", help="生成最小可用骨架")
    p_new.add_argument("name", help="app 目录名")
    p_new.add_argument("--dir", default=".", help="父目录（默认当前目录）")
    p_new.add_argument("--title", default=None, help="窗口标题（默认同目录名）")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="打开窗口运行 app")
    p_run.add_argument("app", help="app 目录")
    p_run.add_argument("--no-llm", action="store_true", default=None,
                       help="控制面模式（无 LLM 实例，写者是驱动者）")
    p_run.add_argument("--hub-port", type=int, default=None,
                       help="协作总线端口（首页 open / hub 编排时注入；缺省不接入总线）")
    p_run.add_argument("--listen-port", type=int, default=None,
                       help="本实例的协作监听端口（首页 open 注入）：有它才收得到 tell")
    p_run.add_argument("--wipe-memory", action="store_true",
                       help="启动前清空运行期记忆（不给这个开关就一律保留）")
    p_run.add_argument("--handoff", action="store_true",
                       help="启动即交棒：打命名快照当回退锚，写者交给自主回路（无人在场）")
    p_run.set_defaults(func=cmd_run)

    p_remote = sub.add_parser("remote", help="远程/多客户端绑定（TCP，无头；写者=驱动者）")
    p_remote.add_argument("app", help="app 目录")
    p_remote.add_argument("--port", type=int, default=8765)
    p_remote.add_argument("--hub-port", type=int, default=None,
                          help="协作总线端口（hub 编排时传入；缺省不接入总线）")
    p_remote.set_defaults(func=cmd_remote)

    p_repl = sub.add_parser("repl", help="手写程序模式（人作驱动者的命令批 REPL）")
    p_repl.add_argument("app", help="app 目录")
    p_repl.set_defaults(func=cmd_repl)

    p_ci = sub.add_parser("verify-ci", help="自证以退出码说话（CI/自动化用，不进窗口）")
    p_ci.add_argument("--filter", default="", help="只跑名字含该串的用例")
    p_ci.set_defaults(func=cmd_verify_ci)

    p_keys = sub.add_parser("keys", help="凭据：清单 / 录入 / 撤销 / 探活与泄漏自检")
    p_keys.add_argument("action", choices=["list", "set", "clear", "check"])
    p_keys.add_argument("name", nargs="?", help="set/clear 时的变量名")
    p_keys.add_argument("--env", action="store_true",
                        help="set 时写进**环境变量**（持久；缺省写钥匙串）")
    p_keys.add_argument("--keystore", action="store_true",
                        help="clear 时删钥匙串（缺省清环境变量）")
    p_keys.add_argument("--app", default=None, help="针对某个 app（读它的 provider 配置）")
    p_keys.set_defaults(func=cmd_keys)

    p_hub = sub.add_parser("hub", help="多 app 编排（发现 / 拉起 / 停止 / 健康检查）")
    p_hub.add_argument("dir", help="父目录")
    p_hub.add_argument("action", choices=["list", "up", "down", "status", "bus"])
    p_hub.add_argument("--base-port", type=int, default=8800)
    p_hub.set_defaults(func=cmd_hub)

    p_fuse = sub.add_parser("fuse", help="融合：把 B 并入 A（确认式系统动作，plan 一等）")
    p_fuse.add_argument("a", help="保留方 app 目录")
    p_fuse.add_argument("b", help="被并方 app 目录")
    p_fuse.add_argument("--plan", default=None, metavar="plan.json",
                        help="完整方案（renames/caps/window_title/intent）；缺省用机制生成的方案")
    p_fuse.add_argument("--yes", action="store_true", help="确认执行（缺省只打印方案）")
    p_fuse.set_defaults(func=cmd_fuse)

    p_edit = sub.add_parser("edit", help="正式手写程序模式：编辑器整份替换（人作驱动者）")
    p_edit.add_argument("app", help="app 目录")
    p_edit.add_argument("--yes", action="store_true", help="跳过确认提示")
    p_edit.set_defaults(func=cmd_edit)

    p_build = sub.add_parser("build", help="设备打包：生成独立 flet 工程（android/web）")
    p_build.add_argument("app", help="app 目录")
    p_build.add_argument("--target", choices=["android", "web"], default="android")
    p_build.add_argument("--out", default=None, help="工程输出目录（缺省 <app>/build/<target>）")
    p_build.add_argument("--run-flet-build", action="store_true",
                         help="生成后代跑 flet build（需要 flet CLI 与 Android SDK）")
    p_build.set_defaults(func=cmd_build)

    args = ap.parse_args(argv)
    if args.no_llm is None:            # 全局与子命令两处都可能给，取"谁给了谁算"
        args.no_llm = False
    if args.cmd is None:
        return cmd_home(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
