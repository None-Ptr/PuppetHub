"""一次性探测：模板行内 input 的运行期状态是否按行隔离。跑完即删。"""
import sys

sys.path.insert(0, r"e:\Projects\Puppet")

from puppet.engine import Engine

prog = [
    "add #root window #win",
    "add #win col #main",
    "add #main list #items source=#todos template=#tpl",
    "add #root template #tpl as t",
    "add #tpl row #r",
    'add #r input #note placeholder="行内输入"',
    'data #todos = [] of {text: str = ""}',
]
eng = Engine()
diags = eng.load(prog)
print("load errs:", [d.code for d in diags if d.level == "error"])
eng.apply_batch(['append #todos item={text: "a"}',
                 'append #todos item={text: "b"}'])
eng.fire("note", "change", row=0, value="行零的输入")
eng.fire("note", "change", row=1, value="行一的输入")
node = eng.program.nodes.get("note")
v = node.attrs.get("value")
print("模板行 input 的 attrs.value =", repr(v.value if v else None))
print("→ 同一个值服务所有行实例（最后写入者赢）" if v else "→ 无状态")
