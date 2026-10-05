"""凭据体系冒烟（V5）：三层解析 / 钥匙串 / profile / 探活 / 泄漏自检。

要验证的是"凭据这件事**没有静默失败**"：
- 解析顺序写死且可见（env → 钥匙串 → 报错说清试过哪两层）；
- 一切输出只给**名字/来源/长度**，永不回显密钥（含掩码也不给）；
- 探活把 401 / 403 / 404 / 429 / 超时 / 连不上 **分开说清**（本机假服务端逐条打）；
- 泄漏自检真去 app 内文本文件里找明文，命中只报位置。

用法：python docs/smoke-keys.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_KEY = "sk-fake-0123456789abcdef"
PROGRAM = ['add #root window #win title="凭据冒烟"',
           'add #win col #content pad=16',
           'add #content text #t text="你好"']


# ------------------------------------------------------------------ 假服务端

class _Behaviour:
    models_status = 200
    chat_status = 200
    body = "{}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音
        pass

    def _reply(self, status: int, body: str = "{}") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._reply(_Behaviour.models_status,
                        '{"data": [{"id": "fake"}]}' if _Behaviour.models_status == 200
                        else '{"error": "nope"}')
        else:
            self._reply(404, '{"error": "not found"}')

    def do_POST(self):
        if self.path.endswith("/chat/completions"):
            self._reply(_Behaviour.chat_status,
                        '{"choices": [{"message": {"content": "pong"}}]}'
                        if _Behaviour.chat_status == 200 else '{"error": "nope"}')
        else:
            self._reply(404, '{"error": "not found"}')


def main() -> int:
    from puppethub import keys as keymod
    from puppethub import secrets

    home = Path(tempfile.mkdtemp(prefix="puppethub-keys-"))
    os.environ["PUPPETHUB_HOME"] = str(home)
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % port

    print("1) 三层解析：顺序写死（env → 钥匙串 → 都没有）")
    os.environ.pop("SMOKE_KEY", None)
    keymod.set_secret("SMOKE_KEY", FAKE_KEY)
    info = secrets.secret_info("SMOKE_KEY")
    check("钥匙串层可解析", info["source"] == "keystore" and info["length"] == len(FAKE_KEY),
          str(info))
    check("只报名字/来源/长度（不回显）",
          FAKE_KEY not in json.dumps(info, ensure_ascii=False), str(info))
    os.environ["SMOKE_KEY"] = "env-wins-abcdefgh"
    info = secrets.secret_info("SMOKE_KEY")
    check("env 优先于钥匙串", info["source"] == "env", str(info))
    check("长度跟着 env 走（证明真的取到 env 的值）",
          info["length"] == len("env-wins-abcdefgh"), str(info))
    os.environ.pop("SMOKE_KEY", None)
    check("未设置时如实报", secrets.secret_info("NOPE_KEY")["present"] is False)
    check("清单行不含密钥值",
          FAKE_KEY not in "\n".join(keymod.list_lines(["SMOKE_KEY"])))

    print("\n2) 钥匙串：名字校验 / 删除 / 权限")
    try:
        keymod.set_secret("bad name!", "x")
        check("非法名字被拒", False, "竟然写进去了")
    except Exception as ex:  # noqa: BLE001
        check("非法名字被拒", "名字必须是环境变量式" in str(ex), str(ex))
    path = secrets.secrets_path()
    check("钥匙串在 app 目录之外（$PUPPETHUB_HOME）", str(path).startswith(str(home)),
          str(path))
    if os.name == "posix":
        check("权限 0600", (path.stat().st_mode & 0o777) == 0o600,
              oct(path.stat().st_mode & 0o777))
    else:
        print("      （Windows：chmod 语义有限，退化为「仅在用户目录」——手册里如实写了）")
    check("删除生效", keymod.delete_secret("SMOKE_KEY")
          and secrets.secret_info("SMOKE_KEY")["present"] is False)
    keymod.set_secret("SMOKE_KEY", FAKE_KEY)

    print("\n3) profile：机器级端点表 + app 里只留名字（app 显式键优先）")
    (home / "providers.toml").write_text(
        '[fake]\nbase_url = "%s"\nmodel = "fake-model"\nkey_env = "SMOKE_KEY"\n' % base,
        encoding="utf-8")
    from puppethub.appdir import create_app
    from puppethub.session import Session
    work = Path(tempfile.mkdtemp(prefix="puppethub-keys-app-"))
    app = create_app(work, "keysapp", "凭据冒烟")
    app.write_source(list(PROGRAM))
    app.config_path.write_text('llm_provider = "openai-compat"\nstorage = "file"\n\n'
                               '[llm]\nprofile = "fake"\n', encoding="utf-8")
    session = Session(app)
    session.start()
    settings = session.llm_settings()
    check("profile 的端点进了 provider 配置", settings["base_url"] == base, str(settings))
    check("profile 的模型与凭据名也补上了",
          settings["model"] == "fake-model" and settings["key_env"] == "SMOKE_KEY",
          str(settings))
    check("本 app 需要的凭据名可见", session.credential_names() == ["SMOKE_KEY"],
          str(session.credential_names()))
    app.config_path.write_text('llm_provider = "openai-compat"\nstorage = "file"\n\n'
                               '[llm]\nprofile = "fake"\n\n'
                               '[plugins.openai-compat]\nmodel = "app-wins"\n',
                               encoding="utf-8")
    session2 = Session(app)
    session2.start()
    check("app 里的显式键优先于 profile", session2.llm_settings()["model"] == "app-wins",
          str(session2.llm_settings()))
    (home / "providers.toml").unlink()
    session3 = Session(app)
    session3.start()
    check("profile 缺失时报错可执行（说清去哪写、现有哪些）",
          any("LLM_PROFILE" == e["code"] for e in session3.log), str(list(session3.log)[-1:]))
    (home / "providers.toml").write_text(
        '[fake]\nbase_url = "%s"\nmodel = "fake-model"\nkey_env = "SMOKE_KEY"\n' % base,
        encoding="utf-8")

    print("\n3b) 总线兜底：app 没配模型 → 用社会层（调度官）那套")
    (home / "orchestrator.toml").write_text(
        '[plugins.openai-compat]\nbase_url = "%s"\nmodel = "bus-model"\n'
        'key_env = "SMOKE_KEY"\n' % base, encoding="utf-8")
    bare_app = create_app(work, "bareapp", "没配模型的 app")
    bare_app.write_source(list(PROGRAM))
    bare_app.config_path.write_text('llm_provider = "openai-compat"\nstorage = "file"\n',
                                    encoding="utf-8")
    bare = Session(bare_app)
    bare.start()
    check("app 没配 → 回退到总线那套",
          bare.llm_settings()["model"] == "bus-model"
          and bare.llm_settings()["base_url"] == base, str(bare.llm_settings()))
    check("回退**留痕**（降级不许静默）",
          any("LLM_FALLBACK" == e["code"] for e in bare.log), str(list(bare.log)[-1:]))
    bare_app.config_path.write_text(
        'llm_provider = "openai-compat"\nstorage = "file"\n\n'
        '[plugins.openai-compat]\nmodel = "own-model"\n', encoding="utf-8")
    own = Session(bare_app)
    own.start()
    check("app 自己配了就不回退（显式键优先）",
          own.llm_settings()["model"] == "own-model", str(own.llm_settings()))
    check("自己配了就**没有**回退痕迹",
          not any("LLM_FALLBACK" == e["code"] for e in own.log),
          str(list(own.log)[-1:]))
    (home / "orchestrator.toml").unlink()   # 清掉这一节造的社会层配置，别污染后面的节

    print("\n4) 探活：六种失败分开说清（本机假服务端）")
    _Behaviour.models_status = 200
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("200 → ok", result["ok"] and result["kind"] == "ok", str(result))
    _Behaviour.models_status = 401
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("401 → 密钥被拒（不是别的）",
          result["kind"] == "auth" and "密钥被拒" in result["message"], str(result))
    _Behaviour.models_status = 403
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("403 → 无权限", result["kind"] == "forbidden", str(result))
    _Behaviour.models_status = 429
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("429 → 额度/频率（不是密钥错）",
          result["kind"] == "rate_limit" and "不是密钥错" in result["message"], str(result))
    _Behaviour.models_status = 404
    _Behaviour.chat_status = 404
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("404 → 端点不存在（提示 /v1）",
          result["kind"] == "endpoint" and "/v1" in result["message"], str(result))
    _Behaviour.chat_status = 200
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "SMOKE_KEY"})
    check("/models 404 → 退化为最小对话并成功", result["ok"], str(result))
    _Behaviour.models_status = 200
    result = keymod.probe({"base_url": base, "model": "fake-model", "key_env": "NOPE_KEY"})
    check("没有凭据 → 说清试过两层 + 给命令",
          result["kind"] == "no_key" and "keys set NOPE_KEY" in result["message"], str(result))
    result = keymod.probe({"base_url": "http://127.0.0.1:9/v1", "model": "m",
                           "key_env": "SMOKE_KEY"})
    check("连不上 → 明确是网络", result["kind"] in ("network", "timeout"), str(result))

    print("\n5) 泄漏自检：真去 app 内文本文件里找明文（命中只报位置）")
    app.write_source(list(PROGRAM) + ["// ok"])
    clean = keymod.leak_scan(app.root, names=["SMOKE_KEY"])
    check("干净时零命中", clean["hits"] == [], str(clean["hits"]))
    with open(app.root / ".puppethub" / "chat.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "user", "text": "顺手贴了 %s" % FAKE_KEY},
                            ensure_ascii=False) + "\n")
    hit = keymod.leak_scan(app.root, names=["SMOKE_KEY"])
    check("chat.jsonl 尾部命中", any("chat.jsonl" in h["file"] for h in hit["hits"]),
          str(hit["hits"]))
    report = keymod.format_leak(hit)
    check("报告只报位置，不含明文", FAKE_KEY not in report, report)
    app.write_source(list(PROGRAM) + ['// %s' % FAKE_KEY])
    hit = keymod.leak_scan(app.root, names=["SMOKE_KEY"])
    check("真源命中", any(h["file"] == "app.puppet" for h in hit["hits"]), str(hit["hits"]))
    check("扫过什么如实写", "小文件全量扫" in hit["note"] and hit["scanned"], hit["note"])

    print("\n6) 组合动作：探活 + 泄漏自检（驾驶舱「探活」走的就是这条）")
    result = keymod.check(app.root, names=["SMOKE_KEY"],
                          settings={"base_url": base, "model": "fake-model",
                                    "key_env": "SMOKE_KEY"})
    check("有泄漏时整体判失败（不能让'探活过了'掩盖泄漏）",
          result["ok"] is False and "命中" in result["text"], result["text"][:200])
    app.write_source(list(PROGRAM))
    (app.root / ".puppethub" / "chat.jsonl").write_text("", encoding="utf-8")
    result = keymod.check(app.root, names=["SMOKE_KEY"],
                          settings={"base_url": base, "model": "fake-model",
                                    "key_env": "SMOKE_KEY"})
    check("清干净后整体通过", result["ok"] is True, result["text"][:200])

    server.shutdown()
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
