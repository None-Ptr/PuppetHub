# PuppetHub

**搭 app 的软件**——不是 app 运行时框架，也不是 UI 库。

人用**自然语言**描述想要什么，内置 LLM 把它翻译成**命令批**，引擎校验后写回真源并渲染。
造出的 app **不含聊天框**：聊天只存在于 PuppetHub 自己的窗口里。

> **An app is an agent.** 应用本身就是会感知、决策、行动、记忆、调用工具的 agent；
> 自然语言对话只是驱动它的一种**输入方式**，不是它的输出。

## 三层分工

| 层 | 内容 |
|---|---|
| `puppet`（PyPI：`openpuppet-language`） | 语言规范 + 语义引擎 + 自证套件，零第三方依赖 |
| **`puppethub`（本仓）** | 搭建 / 运行 / 运行时定制 / 未来的融合 |
| `puppetOS` | 基于 Linux 内核的发行版，**仅设计文档，不实现** |

## 安装与运行

```bash
# 语言标准实现（本阶段：本机仓库可编辑安装）
pip install -e ../Puppet

pip install -e .
puppethub new myapp
puppethub run myapp              # 带窗口：左 app + 右驾驶舱
puppethub run myapp --no-llm     # 无 LLM 实例：stdio 控制面，驱动者即写者
puppethub remote myapp           # 远程/多客户端（TCP 无头绑定，写者=驱动者）
puppethub repl myapp             # 手写程序模式：人作驱动者的命令批 REPL
puppethub verify-ci              # 自证以退出码说话（CI/自动化用）
puppethub hub <父目录> up|down|status|list   # 多 app 编排（生命周期，不制造第二写者）
puppethub fuse <A> <B> --yes    # 融合：把 B 并入 A（审计→干跑→确认式合并→B 归档）
puppethub edit <app>            # 正式手写程序模式：编辑器整份替换（确认前先干跑校验）
```

## 设计

- **第一原则：杜绝静默失败**。任何"降级 / 丢弃 / 无效 / 上限"都必须产生诊断、日志或事件。
  判据一句话：这条路径失败时，agent 看得到吗？看不到就是 bug。
- **仅 LLM 能写**。人是纯操作者：不用编辑器改程序，只能通过对话表达写入意图。
  命令批是改 `app.puppet` 的唯一方式（它带校验、行号诊断与批末原子写回）。
- **两本账物理分离**。程序（`app.puppet` + `capabilities.py`）与状态（`.puppet/`）
  不混存；引擎只写状态，**绝不改程序**——记忆因此不可能污染程序。
- **真源是程序 IR 的打印件**。注释与排版会在每次写回时被重排，故意图的唯一载体是
  `DESIGN.md`，而不是程序里的注释。

设计依据（决议表、已推翻决议及原因、spike 结论）在本仓 `docs/`：

- `docs/design-v1-draft.md` —— V1 实现依据
- `docs/spike-1-flet-coverage.md` —— flet 四层全覆盖三档清单 + 图标别名表
- `docs/spike-2-flet-verify.md` —— conformance 可行性、能力声明策略、并发桥接
- `docs/probe-flet-fields.py` —— 渲染层依赖的 flet 字段面（重跑即校验）

## 配置与插件

`puppethub.toml`（可选的，**零配置也能跑**）：三个槽位 + 每个插件自己的选项。

```toml
llm_provider = "openai-compat"     # 单选
storage = "file"                   # 单选
prompt = ["default"]               # 多选叠加，按顺序跑

[plugins.openai-compat]
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
key_env = "OPENAI_API_KEY"         # **只存变量名**：toml 会被 git 跟踪、进 dist、被打包
context_limit = 128000             # provider 自报窗口上限（宿主据此做超限的显式省略）
```

插件放在 `~/.puppethub/plugins/*.py`，**放文件即生效**（不必安装）。一个插件长这样：

```python
NAME = "my-provider"
PROVIDES = ["llm_provider"]        # 可选槽位：llm_provider / storage / prompt

def create_llm_provider(api):      # 每个槽位一个工厂
    return MyProvider(api)
```

三条边界（不是约定，是机制）：**插件只产出值**（`PluginAPI` 不给任何引擎句柄）·
**策略在宿主、机制在插件**（storage 的可写路径只有 `.puppet/` 与 `.puppethub/`）·
**失败必须可见**（加载失败、槽位写错名、插件重名、路径越界，一律报出来，且坏插件不阻止启动）。

## 当前进度

已实现：

- `new` / `run` / `remote` / `repl` / `verify-ci` 子命令；语言版本**与数据文件**不齐**拒绝启动**
- app 目录契约：`app.puppet` · `capabilities.py` · `DESIGN.md` · `assets/` ·
  `puppethub.toml` · `.puppethub/` · `.puppet/{state,memory,snapshots}`
- service 层：装载 → 命令批 → 批末整份写回真源 → **写前自动快照**
- 渲染层：程序 IR + 观察面 → flet 控件树（控件 / 属性 / 动效 / 图标四层映射）
- **共作者 LLM**：对话通道（后台线程 + 流式上屏）· 指令块协议（命令批 / 整体替换 /
  受限文件写入 / 反问）· 诊断回灌 · 卡住检测 · 危险动作两层确认 · 执行 / 讨论两档
- 插件体系：三槽位 + 目录扫描发现 + 失败隔离；内置 provider / storage / prompt
- 驾驶舱：对话 / 观察流与诊断 / 程序只读面板 + 命名快照 · 回滚 · 重载 · 重置 ·
  清记忆 · 自证 · 查看本轮 prompt；**关窗前拦脏状态**
- **自证**：`puppethub.protocol` 把产品渲染器包成 conformance 可驱动的子进程，
  `自证`按钮跑它 → **118/118 通过（跳过 0）**
- **无 LLM 实例**：`run <app> --no-llm` 进入控制面（stdio 协议），驱动者就是写者——
  走的还是"引擎校验 → 批末写回真源 → 写前自动快照"那条路
- **运行期记忆**：`.puppet/memory/`（可读可改的 JSONL）· 每次写入留诊断 · 上限 + 语义化裁剪 ·
  自动裁剪与人主动清空**可区分** · **重置保留**，清空要显式（`--wipe-memory` / 驾驶舱按钮）
- **快照 / 引擎状态走 storage 槽位**：派生数据落盘走同一条通道（白名单 / 原子写 / 写失败标脏）；
  程序资产（`app.puppet` / `capabilities.py`）走命令批与受限写入——两条通道各司其职
- **人工接管**：卡住 / 停手时驾驶舱亮接管栏（运行期开关，恢复留痕 `LLM_RESUME`）
- **确认卡细化**：待确认动作展示**将做什么 / 可逆性 / 撤销路径**，确认是判断不是点头
- **写者状态机 + 运行期自主 LLM**（`docs/design-v2-autonomous.md`）：llm / autonomous / none
  互斥切换，切换写决策流水；自主当值时共作者退化为只读提问；**自主的边界更硬**——
  危险能力默认拒绝（白名单只能人预先给 `[autonomous] allow_calls`）、批行数与频次预算、
  连续失败熔断切回无人（可选自动回滚）；每步在 `.puppethub/autonomous.jsonl` 留审计；
  驾驶舱自主面板显示写者 / 预算 / 最近决策（含被拒的）
- **热重载**：`capabilities.py` 盘上变化自动重载（可见 `CAPABILITY_RELOAD`）；
  插件热重载带护栏（流式中 / 待确认时拒绝）
- **沙箱**：`[sandbox] enabled = true` 把 storage 插件跑进子进程（内置插件诚实拒绝沙箱化并说明理由）
- **远程 / 多客户端**：`remote` 子命令 = TCP 上的新绑定，同一套协议操作，单写者不破例
- **多 app 编排**：`hub` 子命令（V3 提前落地的最小形态）——发现父目录下的 app、
  以 remote 子进程拉起（**等握手才算数**，幂等）、按账本停止、TCP hello 健康检查；
  **不制造第二个写者**。融合的语义设计见 `docs/design-v3-fusion.md`（草案）
- **融合端到端**（`fuse` 子命令）：机制做体检（依赖对照 / id 与能力审计 / 多窗口拒绝），
  LLM 出方案、人 `--yes` 确认；改名在 **IR 层**做（文本替换救不了 `#cafe` 这种
  颜色/地址歧义），窗口子树嫁接进 A 的窗口，合并先**干跑校验**再落盘；
  B 归档改名不删除，决策流水留痕
- **正式手写程序模式**（`edit` 子命令）：编辑器整份替换，人作驱动者——绕过对话，
  不绕过纪律（确认前干跑校验；拒稿留底 `app.puppet.rejected`、真源恢复原样；
  兜底快照 + 决策流水）
- **CI**：`.github/workflows/ci.yml`（conformance 自证 + **机制冒烟** + 编译检查。
  设计文档不进版本库是既定决议；机制冒烟 / 探针经 `.gitignore` 白名单进库，
  flet 无头运行用 xvfb——CI 的信号从此不只 conformance，机制回归也有防线）
- **移动端前置**：触摸语义提案 `spec/proposals/touch-semantics.md`（语言仓，
  T1-T4 条款，目标 2.2）
- **产物形态**：`packaging/` 下有 PyInstaller spec 与 Dockerfile（容器跑无头绑定，
  桌面窗口需要显示服务器——不声明做不到的）。**PyInstaller 已实测**：构建成功、
  产物 `verify-ci` 118/118 全绿（含三颗雷的修复：可编辑安装的 pathex、flet 数据文件、
  冻结态自证链的 python 替身垫片——详见 spec 头注释）
- **Agent 社会（V4）**：`docs/design-v4-society.md`——app 即服务（服务清单挂在 hello、
  `call` 操作借出能力、**借出白名单缺省空 = 默认拒绝**）· 协作总线（`hub up` 自动拉起，
  消息 = 刺激不是写入，投递进对方观察流并触发其自主回路，审计落账 `hub bus`）·
  深度自主（`goal` 块定方向 + 定期反思沉淀记忆 + 同胞消息触发；目标跨会话可见）。
  铁律在协作里依然成立：**协作 = 说话与调用对方借出的能力，永远不写对方的程序**
- **移动端**：`build --target android|web` **已实现并实测**——生成独立 flet 工程
  （纯 app 实例、写者=无、`pointer=touch` 自述、真源快照内嵌、REQUIRES 进 requirements、
  **构建期解析**（缺包生成期就拒）+ **凭据 manifest**（capabilities.py 静态扫描 →
  `manifest.json`，移动容器按它注入环境变量）、干跑门），生成的工程可独立装载与交互
  （headless 子进程实测）；最终 `flet build apk` 需 Android SDK（`--run-flet-build`
  代跑，失败如实上报）。触摸语义规范见语言仓 05 §10

尚未实现（均**可见地**说明，不做假成功）：

- `flet build apk` 的实际执行（需 Android SDK / 真机——生成工程与前置全部就绪）·
  融合的 `drops`（V3.1，B 归档保证不丢）· 多写者语义（V4 方向）

## 自证脚本（都不联网，可重复跑）

```bash
python docs/probe-flet-fields.py     # 渲染层依赖的 flet 字段面
python docs/smoke-render.py          # 无 GUI：装载 → 渲染 → 命令批 → 写回 → 交互
python docs/smoke-window.py          # 真实窗口（隐藏）：界面粘合 + 后台线程跑 LLM
python docs/smoke-llm.py             # 对话回路机制（用脚本化 provider）
python docs/smoke-protocol.py --full # 协议外壳桥接 + 自证全量（分钟级）
python docs/smoke-controlplane.py    # run --no-llm：驱动者即写者
python docs/smoke-memory.py          # 运行期记忆：写入留痕 / 可读可改 / 语义化裁剪
python docs/smoke-v2.py              # V2：写者状态机 / 自主回路 / 热重载 / 沙箱 / 远程
python docs/smoke-hub.py             # 多 app 编排：发现 / 等握手拉起 / 幂等 / 停止
python docs/smoke-fusion.py          # 融合：审计停下 / IR 改名 / 干跑 / 归档 / CLI 全链
python docs/smoke-edit.py            # 手写程序模式：干跑守卫 / 拒稿留底 / 真源恢复
python docs/smoke-build.py           # 设备打包：manifest 凭据 / REQUIRES 构建期解析 / 独立装载
python docs/smoke-society.py         # V4：服务化 / 协作总线 / 深度自主，铁律不破
```

