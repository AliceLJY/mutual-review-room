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
adapter。两个开发环境均用以下版本验收：

| 组件 | 已测试版本 |
| --- | --- |
| Claude Code | 2.1.251 |
| Codex CLI | 0.151.0 |
| Kimi CLI | 0.39.1 |
| tmux | 3.7b |

## 安装

```bash
git clone https://github.com/AliceLJY/mutual-review-room.git
cd mutual-review-room
python3 -m pip install .
```

也可以在仓库内直接使用 `./scripts/mutual-review-room`。

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
`--reviewer-model` 会在创建任务前被拒绝。

## 持久状态

任务与只追加的 SQLite 账本默认保存在
`~/.mutual-review-room/review-jobs`；彼此隔离的 reviewer 工作区保存在
`~/.mutual-review-room/review-workspaces/<job>/<reviewer>`。

账本会记录原始问题、前台可见的完整回答、reviewer 身份、轮次、父请求、
provider 原生 session ID、状态和时间。即使关闭终端，重新建立 tmux 房间时仍会
读取同一份账本并续接已经绑定的原生会话。每个房间使用独立的 tmux server，
因此鼠标、历史和按键设置不会影响其他 tmux session。

常用恢复命令：

```bash
mutual-review-room status --job JOB_ID
mutual-review-room room --job JOB_ID --replace
mutual-review-room recover --job JOB_ID --token-file TOKEN_FILE
```

`recover` 只会把失去控制的请求记为已中断，不会重发提示词，也不会释放已经占用的
轮次。第 N 轮中断后只能从 N+1 轮继续；如果第 3 轮中断，就应明确保留这项不确定性
后收敛，或者新建任务。

## 互审约束

- 第一轮向所有选定 reviewer 发送同一份完整任务信封。
- 后续轮次可以针对不同 reviewer 分别追问。
- reviewer 不会自动读到 owner 对话或其他 reviewer 的完整回答。
- 右侧显示前台可见回答，不展示隐藏推理或内部工具流量。
- owner 最多进行三轮，并如实报告仍未解决的分歧。
- 某一路失败或额度不足，不会抹掉其他 reviewer 已经返回的内容。

派发依赖持久账本，不使用 tmux `send-keys`、窗口抓取或共享聊天室充当消息总线。
身份、隔离、恢复和精确一次边界见[架构说明](docs/architecture.md)。

## 高级 tmux 快捷键

日常只需要鼠标滚轮。熟悉 tmux 的用户也可以用 `Ctrl-b [` 进入复制模式，
再按 `q` 回到实时画面。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mutual_review_room
ruff check .
```

本项目采用 MIT 许可证。
