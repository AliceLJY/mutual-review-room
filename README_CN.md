# Mutual Review Room

[English](README.md)

Mutual Review Room 把一个可对话的 owner 和你选定的 reviewer 放进同一个
tmux 房间。你只在左侧和 owner 对话；右侧逐窗显示 reviewer 的完整原话，
这些窗口只读，因此你可以旁观整个互审过程，不需要自己传话。

```text
┌──────────────────────────┬──────────────────────────┐
│ owner                    │ reviewer kimi            │
│                          ├──────────────────────────┤
│ 在这里对话               │ reviewer codex           │
└──────────────────────────┴──────────────────────────┘
```

每位 reviewer 都使用独立的 provider 原生会话。第一轮可以收到完全相同的任务
信封；后续 owner 可以根据各自的回答分别追问，而 reviewer 仍然记得自己前面的
对话。

## 环境要求

- 带有 `sandbox-exec` 的 macOS
- Python 3.10 或更高版本
- tmux
- 至少一个已经登录的 provider CLI：`claude`、`codex` 或 `kimi`

Python 运行时代码只使用标准库。当前 reviewer 的文件系统隔离依赖 macOS
Seatbelt；隔离能力不存在时会拒绝启动相应 reviewer，不会静默降低安全边界。

目前是对 provider CLI 版本敏感的 alpha：上游参数和事件格式变化都可能影响
adapter。

| 组件 | 已测试版本 |
| --- | --- |
| Claude Code | 2.1.251 |
| Codex CLI | 0.151.0 |
| Kimi CLI | 0.39.1 |
| tmux | 3.7b |

并不是每一种 provider 角色都跑通过真实账号。0.1.0 验收实际覆盖到的范围
（macOS 27，版本同上）：

| 角色 | 状态 | 依据 |
| --- | --- | --- |
| Kimi owner | 已验证 | 完整两轮房间，原生会话成功续接 |
| Codex owner | 已验证 | 房间启动成功，并从 owner 沙盒内部发出派发命令 |
| Kimi reviewer | 已验证 | 两轮，原生会话 ID 保持不变 |
| Codex reviewer | 已验证 | 两轮，原生会话 ID 保持不变 |
| Claude owner | 已验证 | 完整一轮：契约加载 2 秒照做、发出派发、终审 `complete`（0.1.2 实测）|
| Claude reviewer | 已验证 | 一轮真实作答：20 秒给出两条正确发现（0.1.2 实测）|

Claude adapter 有自动化测试覆盖。0.1.0 验收时该账号触发限流（HTTP 429），
两个 Claude 角色都没验成；2026-09-01 在 0.1.1 上补测时发现 reviewer 只回
「写计划等批准」的过程叙述、零审查发现，根因是 `_claude_command` 硬编码的
`--permission-mode plan`——只读性本就由 `--tools ""` 加 Seatbelt 保证，
plan 模式在 `--print` 下只让 Claude 等一个永远不会来的批准（对照重放证实：
仅去掉该参数，同一信封 24 秒内给出两条正确发现；因调用带 `--safe-mode`
禁用了用户侧全部定制，该行为是 CLI 原生行为、与测试机配置无关）。0.1.2
移除该参数后重验：Claude owner + Claude reviewer 同房完整一轮——契约加载
2 秒内按要求只回一句（此前需 2.5–4.5 分钟且出现过
`model_refusal_fallback`）、owner 发出 `dispatch-all`、reviewer 20 秒给出
两条正确发现、owner 写入 verdict 并 `complete` 关闭 job，全部正常。此前
Claude owner 配 codex/kimi 双 reviewer 的一轮、以及 codex owner 的终审也
已分别验证过。

## 安装

```bash
uv tool install git+https://github.com/AliceLJY/mutual-review-room
```

装完 `mutual-review-room` 就在 `PATH` 里，跑在独立环境中，不污染系统 Python。
习惯 pipx 的话 `pipx install git+https://github.com/AliceLJY/mutual-review-room`
等价。以后要换到新版本，同一条命令加 `--force` 重跑即可。

也可以从仓库装或直接在仓库内跑：

```bash
git clone https://github.com/AliceLJY/mutual-review-room.git
cd mutual-review-room
python3 -m pip install .          # 需在 virtualenv 里
./scripts/mutual-review-room      # 或者不装，直接就地运行
```

对着 Homebrew / 系统自带 Python 裸跑 `pip install .` 会被
[PEP 668](https://peps.python.org/pep-0668/) 拒绝；请用 virtualenv 或上面两个
工具安装器，不要用 `--break-system-packages` 硬来。

**本项目有意不发布到 PyPI。** 从 git 直接装已经是一条命令的事，发上去只换来
一个更短的名字，而 PyPI 的包名一旦上传就基本永久占用、收不回来。对一个这么
年轻的东西来说这笔交易不划算：它只支持 macOS、还是 alpha，而且紧贴三个
provider CLI 的具体参数和事件格式——上游随时可能改。等它积累了真实使用、
并且开始有作者之外的人想装它，再重新考虑。

## 快速开始

先查看当前能够发现哪些内置 provider：

```bash
mutual-review-room providers
```

以 Codex 为 owner，同时打开 Kimi 和 Codex 两位 reviewer：

```bash
mutual-review-room launch \
  --owner codex \
  --reviewer kimi \
  --reviewer codex \
  --cwd "$PWD"
```

打开后只在左侧说话。右侧只看不说；鼠标指向任意窗口后直接滚动滚轮，即可
翻阅该窗口保留下来的历史。每个窗口保留最多 10 万行，不再受 tmux 很小的
默认历史限制。

Codex 作为 owner 时会在正常的 `workspace-write` 沙盒里自动批准操作。启动器只对
本次调用精确标记 owner 工作目录为可信，并额外开放私有 broker 收件箱这一处可写
目录；它不会修改 Codex 全局配置，也不会停在项目是否可信的确认界面。绑定 owner
原生会话需要先真实调用一次 provider，所以 owner 窗格最长可能要等一分钟才出现。

从仓库直接运行（没有 pip 安装）也是支持的：owner 拿到的是完整写法
`python3 -m mutual_review_room.cli --root ...`，不是裸的 `mutual-review-room`，
因此控制台脚本在不在 `PATH` 里都不影响。

每增加一位 reviewer，就多写一次 `--reviewer`。代码不设置固定数量上限，
实际能看清多少主要取决于终端高度。目前内置 Claude Code、Codex CLI 和
Kimi CLI 三种 adapter。

如果确实需要同时打开同一种 provider 的两个独立会话，再给它们不同别名：

```bash
mutual-review-room launch \
  --owner codex \
  --reviewer kimi-a=kimi \
  --reviewer kimi-b=kimi
```

日常写法始终是 `--reviewer kimi`。`kimi=kimi` 这种重复写法会直接提示改正。

Kimi 目前使用 CLI 自己选择的默认模型。给 Kimi 使用 `--owner-model` 或
`--reviewer-model` 会在创建任务前被拒绝。Kimi CLI 0.39.1 本身是有 `-m/--model`
参数的；是 adapter 有意不传它，所以房间宁可直接拒绝，也不接受一个自己会忽略
的模型选项。

## reviewer 的工具隔离是量出来的，不是假定的

`mutual-review-room providers` 报告的是每个 adapter 实际达成的隔离结果，而不是
它请求的结果。这一点在 Codex 上尤其要紧：房间会请求关闭九个执行与多媒体入口，
而在 Codex CLI 0.151.0 上其中八个生效，唯独 `features.unified_exec=false` 被接受
之后**静默忽略**。`--strict-config` 拦不住它——这个键是被认识的，只是没有被遵守。

所以 `tool_access` 报的是 `sandboxed-residual` 而不是 `none`，并且点名那个仍然
开着的入口；这个测量结果会写进该任务的 reviewer 记录里。Codex reviewer 依然受
CLI 只读沙盒和 Seatbelt 配置约束——对这个入口而言，真正起作用的边界是它们，
房间如实这么说，而不是宣称工具已经不存在。

## 持久状态

任务与只追加的 SQLite 账本默认保存在
`~/.mutual-review-room/review-jobs`；彼此隔离的 reviewer 工作区保存在
`~/.mutual-review-room/review-workspaces/<job>/<reviewer>`。

账本会记录原始问题、前台可见的完整回答、reviewer 身份、轮次、父请求、
provider 原生 session ID、状态和时间。即使关闭终端，重新建立 tmux 房间时仍会
读取同一份账本并续接已经绑定的原生会话。每个房间使用独立的 tmux server，
因此鼠标、历史和按键设置不会影响其他 tmux session。

房间启动时还会建立一个不可见的 job-local broker。处在沙盒里的 owner 只把签名
请求写入私有文件信箱；broker 在 owner 沙盒之外启动 reviewer，再给每一路套上各自
的 Seatbelt 隔离。这样既避开 macOS 不允许重复嵌套沙盒的问题，也不需要每问一次
reviewer 就弹一次批准。broker 不是网络服务，reviewer 也读不到它的控制目录。

常用恢复命令：

```bash
mutual-review-room status --job JOB_ID
mutual-review-room room --job JOB_ID --replace
mutual-review-room recover --job JOB_ID --token-file TOKEN_FILE
```

`recover` 只会把失去控制的请求记为已中断，不会重发提示词，也不会释放已经占用的
轮次。第 N 轮中断后只能从 N+1 轮继续；如果第 3 轮中断，就应明确保留这项不确定性
后收敛，或者新建任务。

`status` 是纯读操作，不需要令牌。`recover` 需要 owner 权限：在 owner 窗格里用
注入的令牌就够了，只有从别的终端跑时才需要 `--token-file`。

## 互审约束

- 第一轮向所有选定 reviewer 发送同一份完整任务信封。
- 后续轮次可以针对不同 reviewer 分别追问。
- reviewer 不会自动读到 owner 对话或其他 reviewer 的完整回答。
- 右侧显示前台可见回答，不展示隐藏推理或内部工具流量。
- owner 最多进行三轮，并如实报告仍未解决的分歧。
- 某一路失败或额度不足，不会抹掉其他 reviewer 已经返回的内容。

派发依赖签名文件信箱与持久账本，不使用 tmux `send-keys`、窗口抓取、socket 或
共享聊天室充当消息总线。身份、隔离、恢复和精确一次边界见
[架构说明](docs/architecture.md)。

## 高级 tmux 快捷键

日常只需要鼠标滚轮。熟悉 tmux 的用户也可以用 `Ctrl-b [` 进入复制模式，
再按 `q` 回到实时画面。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mutual_review_room
python3 -m pip install '.[dev]'   # ruff 不是运行时依赖
ruff check .
```

lint 规则集已经钉死在 `pyproject.toml` 里。ruff 从 0.16 起扩大了默认选中的规则，
不钉的话同一条命令会在一个版本上全绿、在另一个版本上报出几十条风格问题。

本项目采用 MIT 许可证。
