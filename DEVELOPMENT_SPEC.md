# Codex 5h Window Keeper — 开发任务说明书

## 1. 项目概述

### 1.1 项目名称

暂定名称：

`codex-window-keeper`

可接受的备选名称：

- `codex-reset-waker`
- `codex-window-waker`
- `codex-quota-keeper`

本说明书以下统一使用 `codex-window-keeper`。

### 1.2 项目目标

开发一个运行于 Linux 服务器的轻量级工具，用于监控本地 Codex CLI 记录的 5 小时使用窗口（5h usage window）重置时间，并在窗口重置后自动执行用户预先配置的 Codex 命令。

典型用途包括：

1. 在 5h 窗口重置后自动执行一个极小请求，例如：

   ```bash
   codex exec "hello"
   ```

   使新的 5h 使用窗口立即开始，而不是等到用户数小时后再次手动使用 Codex 时才开始。

2. 在窗口恢复后继续某个 Codex 任务，例如：

   ```bash
   codex exec resume --last "继续之前尚未完成的任务"
   ```

3. 恢复指定 Codex session：

   ```bash
   codex exec resume <SESSION_ID> "继续执行之前的任务"
   ```

工具应以“读取 Codex 本地状态 → 获取真实 reset timestamp → 到点触发”的方式工作，而不是简单地每 5 小时执行一次固定 cron 任务。

---

## 2. 背景与问题定义

Codex 的 5h 使用窗口不是简单的固定时间段连续滚动。

实际使用过程中可能出现以下情况：

```text
01:40  一个 5h window 开始
06:40  window 结束

06:40 ~ 09:30 用户未使用 Codex

09:30  用户再次发送请求
       新的 5h window 才开始

14:30  新 window 重置
```

因此，如果用户睡觉或长时间离开机器，下一轮 5h window 的起始时间可能向后漂移。

本项目希望自动化以下过程：

```text
Codex 当前 window
        │
        ▼
读取 reset timestamp
        │
        ▼
等待 reset
        │
        ▼
reset + grace period
        │
        ▼
执行用户配置的 Codex command
        │
        ▼
Codex 创建新的 usage window
        │
        ▼
读取新的 reset timestamp
        │
        └───────────────循环
```

---

## 3. 设计原则

项目必须遵循以下原则。

### 3.1 读取真实 reset 时间

不要使用：

```text
last_reset + 5 hours
```

作为唯一依据。

应该优先读取 Codex 本地 session / event 日志中的真实 rate-limit 信息。

预期数据类似：

```json
{
  "rate_limits": {
    "primary": {
      "used_percent": 57.0,
      "window_minutes": 300,
      "resets_at": 1779974659
    }
  }
}
```

其中：

```text
window_minutes = 300
```

代表 5 小时窗口。

工具必须通过：

```text
window_minutes == 300
```

识别 primary 5h window，并读取：

```text
resets_at
```

作为下一次触发时间。

### 3.2 不主动绕过限额

本工具不是 rate-limit bypass 工具。

它只允许：

- 在服务端已经允许新的请求之后发送正常 Codex 请求；
- 自动化用户原本可以手动执行的 Codex CLI 命令；
- 遵守 Codex 实际返回的限额状态。

不得实现：

- 并发账号轮换；
- token/account 池绕过限制；
- 伪造 reset 时间；
- 绕过服务端 rate limit；
- 高频重试轰炸 API。

### 3.3 默认行为保守

默认模式应该只发送一个无副作用的轻量请求：

```bash
codex exec "hello"
```

自动继续真实开发任务必须由用户显式配置。

### 3.4 可长期无人值守运行

程序必须适合：

- Linux server；
- SSH 断开后继续运行；
- systemd user service；
- 运行数天或数周；
- Codex session 文件不断增长；
- 服务异常、网络错误、机器重启后的自动恢复。

---

# 4. 功能需求

## 4.1 Reset 时间检测

程序默认读取：

```text
~/.codex/sessions/
```

目录。

同时支持：

```bash
CODEX_HOME
```

环境变量。

例如：

```bash
CODEX_HOME=/data/codex
```

则 session 路径应变为：

```text
/data/codex/sessions/
```

程序需要递归查找：

```text
*.jsonl
```

文件。

### 4.1.1 性能要求

不能每次轮询都完整读取所有 session 文件。

建议实现：

1. 按 `mtime` 降序排列 session 文件；
2. 默认只检查最近 N 个文件；
3. 每个文件只读取尾部，例如 1–2 MB；
4. 从文件末尾向前寻找最新的 rate-limit event；
5. 找到有效的 `window_minutes == 300` 后即可停止继续扫描。

推荐默认值：

```text
max_session_files = 50
tail_bytes        = 2 MB
```

这些参数应允许通过配置覆盖。

---

## 4.2 JSONL 解析

程序需要兼容 Codex event 可能存在的嵌套结构。

例如：

```json
{
  "type": "event_msg",
  "payload": {
    "type": "token_count",
    "rate_limits": {
      "primary": {
        "used_percent": 50,
        "window_minutes": 300,
        "resets_at": 1779974659
      }
    }
  }
}
```

也应尽量兼容：

```json
{
  "rate_limits": {
    "primary": {
      "window_minutes": 300,
      "resets_at": 1779974659
    }
  }
}
```

解析器不应该因为：

- 单行损坏；
- 不完整 JSON；
- 未知 event；
- Codex 新增字段；

而崩溃。

无效行直接跳过。

---

# 5. 触发逻辑

## 5.1 基本状态机

推荐状态：

```text
WAIT_FOR_STATUS
      │
      ▼
WAIT_FOR_RESET
      │
      ▼
GRACE_PERIOD
      │
      ▼
EXECUTE
      │
 ┌────┴─────┐
 │          │
success   failure
 │          │
 ▼          ▼
WAIT_NEW   RETRY
RESET
```

### WAIT_FOR_STATUS

尚未发现有效 Codex 5h rate-limit 状态。

日志：

```text
No Codex 5h rate-limit information found.
Waiting for Codex activity...
```

继续轮询。

### WAIT_FOR_RESET

已经找到：

```text
resets_at
```

但当前时间尚未到 reset。

等待即可。

### GRACE_PERIOD

到达 reset 后，不应立即 0 秒执行。

默认增加：

```text
10 seconds
```

grace period。

原因：

服务端和本地 timestamp 可能存在极小同步误差。

配置：

```text
grace_seconds = 10
```

### EXECUTE

运行用户配置命令。

例如：

```bash
codex exec "hello"
```

### WAIT_NEW_RESET

命令执行成功后，不要自己计算：

```text
old_reset + 5h
```

而应继续读取 Codex session，直到检测到：

```text
new_resets_at != old_resets_at
```

然后进入新的等待周期。

---

# 6. 防重复执行

程序必须持久化状态，防止以下情况：

```text
14:30 reset
14:30:10 已执行 hello

14:31 程序崩溃
14:32 systemd restart

程序再次读取到旧 reset=14:30
```

此时不得再次执行 hello。

建议状态文件：

```text
~/.local/state/codex-window-keeper/state.json
```

兼容旧系统时也可以使用：

```text
~/.cache/codex-window-keeper/state.json
```

推荐状态内容：

```json
{
  "last_seen_reset": 1779974659,
  "last_triggered_reset": 1779974659,
  "attempts": 1,
  "last_attempt": 1779974670
}
```

判断逻辑：

```text
if reset_at == last_triggered_reset:
    不再触发
```

---

# 7. 命令执行模式

程序至少提供以下三种模式。

## 7.1 Wake 模式

用途：

只启动新的 Codex usage window。

默认命令：

```bash
codex exec "hello"
```

CLI 示例：

```bash
codex-window-keeper \
    --mode wake
```

或：

```bash
codex-window-keeper \
    --command 'codex exec "hello"'
```

---

## 7.2 Resume Last 模式

用途：

继续当前工作目录最近的 Codex session。

示例：

```bash
codex exec resume --last "继续之前尚未完成的任务"
```

CLI：

```bash
codex-window-keeper \
    --mode resume-last \
    --workdir ~/ReleasedCode/EGGPlacer \
    --prompt "继续之前尚未完成的任务"
```

程序最终执行：

```bash
cd ~/ReleasedCode/EGGPlacer
codex exec resume --last "继续之前尚未完成的任务"
```

---

## 7.3 Resume Session 模式

用途：

恢复指定 session。

CLI：

```bash
codex-window-keeper \
    --mode resume \
    --session-id 019xxxxx-xxxx-xxxx \
    --workdir ~/ReleasedCode/EGGPlacer \
    --prompt "继续之前的任务"
```

最终执行：

```bash
codex exec resume \
    019xxxxx-xxxx-xxxx \
    "继续之前的任务"
```

---

# 8. 自定义 Command 模式

高级用户必须能够完全自定义命令：

```bash
codex-window-keeper \
    --command 'codex exec "hello"'
```

或者：

```bash
codex-window-keeper \
    --command './scripts/run-codex-task.sh'
```

命令需要在指定 workdir 中运行：

```bash
--workdir /home/user/project
```

如果没有指定 workdir：

```text
default = $HOME
```

---

# 9. 配置方式

优先级建议：

```text
CLI arguments
    >
environment variables
    >
config file
    >
built-in defaults
```

## 9.1 配置文件

默认：

```text
~/.config/codex-window-keeper/config.toml
```

示例：

```toml
mode = "wake"

codex_home = "~/.codex"
workdir = "~"

prompt = "hello"

grace_seconds = 10
poll_seconds = 20

max_session_files = 50
tail_bytes = 2097152

max_retries = 3
retry_delay_seconds = 300
```

Resume 示例：

```toml
mode = "resume-last"

workdir = "~/ReleasedCode/EGGPlacer"

prompt = """
继续之前尚未完成的任务。
先检查当前 git diff、日志和已有实现，再继续开发。
不要重复已经完成的步骤。
"""
```

---

# 10. 环境变量

至少支持：

```text
CODEX_HOME
CODEX_KEEPER_MODE
CODEX_KEEPER_COMMAND
CODEX_KEEPER_WORKDIR
CODEX_KEEPER_PROMPT
CODEX_KEEPER_SESSION_ID

CODEX_KEEPER_GRACE_SECONDS
CODEX_KEEPER_POLL_SECONDS

CODEX_KEEPER_MAX_RETRIES
CODEX_KEEPER_RETRY_DELAY
```

---

# 11. CLI 设计

推荐使用 Python：

```text
argparse
```

或者：

```text
typer
```

如果希望保持依赖最小化，优先 `argparse`。

目标 CLI：

```bash
codex-window-keeper --help
```

输出大致：

```text
usage: codex-window-keeper [options]

Monitor the Codex 5-hour usage window and execute a command
after the window resets.

Options:

  --mode MODE
      wake
      resume-last
      resume
      command

  --prompt TEXT

  --session-id ID

  --command COMMAND

  --workdir PATH

  --codex-home PATH

  --grace-seconds N

  --poll-seconds N

  --once
      Execute at most one successful trigger and exit.

  --dry-run
      Detect reset events but do not execute Codex.

  --status
      Print current detected 5h window information and exit.

  --config PATH

  -v, --verbose

  -h, --help
```

---

# 12. 必须实现的 `--status`

这是非常重要的调试功能。

执行：

```bash
codex-window-keeper --status
```

应该输出：

```text
Codex 5h Window

CODEX_HOME:
  /home/zq/.codex

Latest session:
  /home/zq/.codex/sessions/2026/09/20/xxx.jsonl

Window:
  300 minutes

Used:
  57%

Reset timestamp:
  1779974659

Reset local time:
  2026-09-20 14:30:59 +08:00

Remaining:
  02:14:37

State:
  WAIT_FOR_RESET
```

如果 reset 已经过期：

```text
State:
  READY_TO_TRIGGER
```

---

# 13. 必须实现 `--dry-run`

执行：

```bash
codex-window-keeper --dry-run
```

程序正常：

- 解析 session；
- 检测 reset；
- 等待；
- 记录本应执行的命令；

但不实际执行 Codex。

日志：

```text
[DRY RUN] Would execute:

codex exec "hello"
```

这是首次安装时推荐的测试方式。

---

# 14. `--once` 模式

为了调试和脚本集成，支持：

```bash
codex-window-keeper --once
```

语义：

```text
等待一个 reset
→ 成功执行一次 command
→ exit 0
```

如果命令最终失败：

```text
exit non-zero
```

---

# 15. 日志系统

默认输出 stdout/stderr，以便 systemd journal 收集。

格式：

```text
2026-09-20 09:31:12 INFO  Codex Window Keeper started
2026-09-20 09:31:12 INFO  Detected 5h reset: 2026-09-20 14:30:59
2026-09-20 14:31:09 INFO  Usage window reset detected
2026-09-20 14:31:09 INFO  Executing: codex exec "hello"
2026-09-20 14:31:14 INFO  Command completed: exit=0
2026-09-20 14:31:34 INFO  New reset detected: 2026-09-20 19:31:12
```

建议使用 Python 标准：

```python
logging
```

不要引入重量级日志依赖。

敏感信息不要写日志。

---

# 16. 命令失败处理

默认：

```text
max_retries = 3
retry_delay = 300 seconds
```

例如：

```text
14:30 reset
14:30:10 第一次执行失败
14:35:10 retry
14:40:10 retry
```

注意：

如果失败原因明确是：

```text
still rate limited
```

可以等待较短时间再次检查。

但禁止无限快速 retry。

任何情况下 retry 间隔不得形成高频请求。

---

# 17. 进程控制

Command 必须支持：

```text
timeout
```

推荐默认：

```text
command_timeout = 30 minutes
```

Wake 模式可以默认更短，例如：

```text
5 minutes
```

超时后：

1. terminate child；
2. 等待数秒；
3. 必要时 kill；
4. 记录失败；
5. 按 retry policy 处理。

---

# 18. 信号处理

程序必须正确处理：

```text
SIGINT
SIGTERM
```

用于：

```bash
Ctrl+C
systemctl stop
```

收到信号后：

- 停止新任务；
- 如有 child process，合理终止；
- 保存状态；
- 正常退出。

---

# 19. systemd 集成

仓库需要提供：

```text
systemd/codex-window-keeper.service
```

模板：

```ini
[Unit]
Description=Codex 5h Window Keeper
After=network-online.target

[Service]
Type=simple

ExecStart=%h/.local/bin/codex-window-keeper

Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

README 给出：

```bash
mkdir -p ~/.config/systemd/user

cp systemd/codex-window-keeper.service \
   ~/.config/systemd/user/

systemctl --user daemon-reload

systemctl --user enable --now \
    codex-window-keeper.service
```

查看日志：

```bash
journalctl --user \
    -u codex-window-keeper \
    -f
```

对于 SSH 退出后 systemd user manager 被停止的机器，可提示：

```bash
loginctl enable-linger "$USER"
```

但不要自动执行此命令。

---

# 20. 推荐代码结构

仓库建议：

```text
codex-window-keeper/
├── README.md
├── LICENSE
├── pyproject.toml
├── .gitignore
│
├── src/
│   └── codex_window_keeper/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── detector.py
│       ├── executor.py
│       ├── state.py
│       ├── service.py
│       └── models.py
│
├── tests/
│   ├── fixtures/
│   │   ├── codex_session_normal.jsonl
│   │   ├── codex_session_corrupt.jsonl
│   │   └── codex_session_multiple_windows.jsonl
│   │
│   ├── test_detector.py
│   ├── test_state.py
│   ├── test_executor.py
│   ├── test_config.py
│   └── test_service.py
│
├── systemd/
│   └── codex-window-keeper.service
│
└── docs/
    ├── architecture.md
    └── troubleshooting.md
```

---

# 21. 模块职责

## `detector.py`

负责：

- session 文件发现；
- 文件排序；
- tail 读取；
- JSONL 解析；
- rate-limit event 提取；
- 返回最新的 5h WindowInfo。

建议：

```python
@dataclass
class WindowInfo:
    window_minutes: int
    used_percent: float | None
    resets_at: int
    source_file: Path
```

接口：

```python
def detect_latest_window(
    codex_home: Path,
    max_files: int,
    tail_bytes: int,
) -> WindowInfo | None:
    ...
```

---

## `executor.py`

负责构建并执行：

- wake；
- resume-last；
- resume；
- custom command。

接口：

```python
@dataclass
class ExecutionResult:
    returncode: int
    started_at: float
    finished_at: float
    timed_out: bool
```

---

## `state.py`

负责：

```text
load_state()
save_state()
mark_attempt()
mark_triggered()
```

状态写入必须采用：

```text
temporary file + atomic rename
```

避免机器掉电造成 JSON 损坏。

---

## `config.py`

负责：

```text
defaults
→ TOML
→ environment
→ CLI
```

合并配置。

---

## `service.py`

实现核心状态机。

不得把所有逻辑塞进 CLI 文件。

---

## `cli.py`

只负责：

- 参数解析；
- 配置加载；
- 调用 service；
- status 输出。

---

# 22. 测试要求

项目必须包含自动测试。

推荐：

```text
pytest
```

目标：

```bash
pytest
```

全部通过。

---

## 22.1 Detector 测试

至少覆盖：

### Case 1

正常 JSONL：

```text
window_minutes = 300
resets_at      = X
```

应成功检测。

### Case 2

同时存在：

```text
primary = 300 minutes
secondary = weekly
```

必须选择 300 分钟窗口。

### Case 3

存在损坏 JSON 行。

不得崩溃。

### Case 4

最新 session 没有 rate_limits，但次新 session 有。

应继续搜索。

### Case 5

同文件存在多个 reset。

必须选择最新 event。

### Case 6

巨大 session 文件。

测试 tail scanning，而不是完整读取。

---

# 23. State 测试

重点测试：

```text
old reset
已经 triggered
程序 restart
```

确保不会二次触发。

测试：

```text
reset_A triggered
reset_B detected
```

必须允许 reset_B 正常执行。

---

# 24. Executor 测试

不要在单元测试中真实调用 Codex。

使用：

- mock；
- `/bin/true`；
- `/bin/false`；
- test helper script。

覆盖：

```text
exit 0
exit != 0
timeout
working directory
SIGTERM
```

---

# 25. Service 测试

时间相关代码必须允许注入 clock。

例如不要在业务逻辑中到处直接：

```python
time.time()
time.sleep()
```

建议封装：

```python
class Clock:
    def now(self) -> float:
        ...

    def sleep(self, seconds: float):
        ...
```

测试时使用 fake clock。

否则 5 小时状态机无法高效测试。

---

# 26. 安装方式

优先支持：

```bash
pipx install .
```

然后：

```bash
codex-window-keeper --help
```

也支持：

```bash
pip install .
```

`pyproject.toml`：

```toml
[project.scripts]
codex-window-keeper = "codex_window_keeper.cli:main"
```

---

# 27. Python 版本

目标：

```text
Python >= 3.10
```

尽量只使用标准库。

允许的可选依赖：

```text
pytest
```

开发依赖可包含：

```text
ruff
mypy
pytest
```

运行时最好做到：

```text
0 external dependencies
```

如果 TOML 需要 Python 3.10：

可以：

- 使用 `tomli`；
- 或提高最低要求到 Python 3.11，使用 `tomllib`。

优先方案：

```text
Python >= 3.11
```

如果希望 Ubuntu 22.04 开箱即用，则考虑兼容 Python 3.10。

---

# 28. README 必须包含

README 至少包括：

1. 项目解决什么问题；
2. 运行原理；
3. 安装；
4. Wake 示例；
5. Resume 示例；
6. Custom command；
7. `--status`；
8. `--dry-run`；
9. systemd 安装；
10. 故障排查；
11. 安全说明；
12. Codex 日志格式发生变化时的兼容性说明。

README 第一屏应可以让用户理解：

```text
This tool does not bypass Codex rate limits.

It waits for the actual 5-hour window reset and then
runs a normal Codex CLI command configured by the user.
```

---

# 29. 安全要求

## 29.1 Shell command

由于支持：

```bash
--command
```

因此这是明确的高级功能。

文档中必须说明：

```text
The command is executed with the current user's privileges.
Only configure commands you trust.
```

不要接受来自网络的动态 command。

---

## 29.2 Session 内容

Codex session 可能包含：

- prompt；
- 项目路径；
- 代码；
- 用户数据。

本工具只应解析 rate-limit metadata。

不得：

- 上传 session；
- telemetry 上传；
- 网络同步；
- 保存完整 prompt；
- 打印 session 内容。

---

# 30. 兼容性

首个版本目标：

```text
Linux
Codex CLI
local ~/.codex session storage
```

明确暂不支持：

```text
Windows native
macOS GUI automation
ChatGPT Web
multi-account rotation
```

macOS CLI 理论上可能可用，但不是 v1 验收范围。

---

# 31. Codex 数据格式兼容策略

Codex 内部 JSONL 属于可能变化的实现细节。

因此 detector 必须：

1. 使用宽松解析；
2. 忽略未知字段；
3. 搜索 `rate_limits` 而不是强依赖 event 全部字段；
4. 对格式变化给出明确 warning；
5. 不因某个文件解析失败导致服务退出。

如果连续无法检测 window，应输出：

```text
WARNING:
No valid 300-minute Codex usage window was detected.

Possible causes:
- Codex has not been used recently.
- CODEX_HOME is incorrect.
- Codex changed its local session format.

Run:
    codex-window-keeper --status --verbose
```

---

# 32. 可选增强功能

以下功能不是 MVP 必须项。

## 32.1 Desktop notification

例如：

```text
notify-send
```

reset 后提示。

---

## 32.2 Webhook

可选：

```text
Discord
Slack
Telegram
generic HTTP webhook
```

但默认关闭。

---

## 32.3 Multiple jobs

未来可以支持：

```toml
[[jobs]]

name = "wake"

mode = "wake"

[[jobs]]

name = "project-A"

mode = "resume"
session_id = "..."
```

MVP 不做。

---

## 32.4 Weekly quota

未来可以识别 Codex secondary / weekly window。

例如：

```text
5h available
weekly exhausted
```

此时不应不断尝试 wake。

MVP 可先检测并在日志中显示 weekly 状态，但不要求复杂调度。

---

# 33. MVP 范围

第一版只需要完成以下内容：

- [ ] Linux
- [ ] Python CLI
- [ ] 查找 `~/.codex/sessions/**/*.jsonl`
- [ ] 检测 `primary.window_minutes == 300`
- [ ] 获取 `resets_at`
- [ ] 等待 reset
- [ ] grace period
- [ ] 执行 command
- [ ] 防重复执行
- [ ] retry
- [ ] wake mode
- [ ] resume-last mode
- [ ] resume session mode
- [ ] custom command
- [ ] workdir
- [ ] `--status`
- [ ] `--dry-run`
- [ ] `--once`
- [ ] systemd service
- [ ] pytest
- [ ] README
- [ ] MIT License

---

# 34. MVP 验收测试

完成开发后必须人工验证以下流程。

## Test A：状态读取

执行：

```bash
codex exec "test"
```

然后：

```bash
codex-window-keeper --status
```

必须能够显示：

```text
window_minutes = 300
reset time
remaining time
```

---

## Test B：Dry Run

```bash
codex-window-keeper \
    --command 'echo WAKE' \
    --dry-run
```

程序到 trigger point 时必须：

```text
显示本应执行 echo WAKE
```

但不能真实执行。

为了避免真的等待 5 小时，测试环境应允许 fixture / fake clock。

---

## Test C：Command

使用临时 fixture 或测试参数，将 reset 设置为几秒之后。

命令：

```bash
codex-window-keeper \
    --command 'touch /tmp/codex-wake-test' \
    --once
```

reset 后：

```bash
test -f /tmp/codex-wake-test
```

应成功。

---

## Test D：Restart

第一次成功触发后：

```bash
systemctl --user restart codex-window-keeper
```

同一个：

```text
resets_at
```

不得再次执行。

---

## Test E：New Window

Codex 创建新的：

```text
resets_at
```

后，程序必须自动切换等待新 reset。

---

# 35. Definition of Done

项目只有同时满足以下条件才算完成：

```text
pytest
```

全部通过；

```text
ruff check .
```

无严重问题；

安装：

```bash
pipx install .
```

成功；

执行：

```bash
codex-window-keeper --status
```

能够读取真实 Codex 5h window；

执行：

```bash
codex-window-keeper --mode wake --once
```

可以在 reset 后成功触发；

systemd user service 能运行；

SSH 退出后在启用 linger 的环境中继续运行；

README 足够让一个没有阅读源码的 Linux 用户完成安装。

---

# 36. LLM 开发代理执行要求

如果本项目由 LLM coding agent 实现，请遵守以下工作方式。

## 36.1 第一阶段：调查

首先检查本机：

```bash
codex --version
codex exec --help
codex exec resume --help

echo "$CODEX_HOME"

find "${CODEX_HOME:-$HOME/.codex}/sessions" \
    -type f \
    -name '*.jsonl' \
    | tail
```

从真实 session 中确认：

```text
rate_limits
window_minutes
resets_at
```

的实际结构。

不得仅根据本文档假设格式。

不要输出 session 中的敏感 prompt 内容。

---

## 36.2 第二阶段：先实现 detector

优先完成：

```text
detector
--status
tests
```

确认真实 reset 能正确读取之后，再实现 daemon 和 command execution。

---

## 36.3 第三阶段：状态机

实现：

```text
detect
wait
trigger
persist
detect new reset
```

不要直接用一个大型 while-loop 堆叠全部逻辑。

应保持模块可测试。

---

## 36.4 第四阶段：systemd 与文档

核心测试通过以后再增加：

```text
systemd
README
installation docs
```

---

## 36.5 禁止行为

LLM agent 不应：

- 修改用户 Codex 配置；
- 删除 Codex session；
- 修改任何已有 session；
- 上传 Codex 日志；
- 自动执行 `loginctl enable-linger`；
- 自动创建网络服务；
- 自动修改项目外代码；
- 为测试真实消耗大量 Codex quota。

---

# 37. 建议的首个 Release

版本：

```text
v0.1.0
```

Release title：

```text
v0.1.0 — Initial Linux release
```

包含：

```text
Codex 5h window detection
Wake / resume automation
Persistent trigger state
Dry-run and status modes
systemd user service
```

---

# 38. 后续路线图

## v0.2

- Weekly quota awareness
- 更完善的 Codex schema compatibility
- JSON status output
- shell completion

## v0.3

- multiple jobs
- notification hooks
- webhook

## v1.0

当：

```text
Codex schema compatibility
restart reliability
systemd operation
tests
```

都经过较长时间验证之后再进入 1.0。

---

# 39. 最终预期用户体验

安装：

```bash
git clone <repo>
cd codex-window-keeper

pipx install .
```

检查：

```bash
codex-window-keeper --status
```

输出：

```text
5h window reset:
2026-09-20 14:30:00

Remaining:
03:12:18
```

测试：

```bash
codex-window-keeper \
    --mode wake \
    --dry-run
```

实际运行：

```bash
codex-window-keeper \
    --mode wake
```

或者：

```bash
codex-window-keeper \
    --mode resume-last \
    --workdir ~/ReleasedCode/EGGPlacer \
    --prompt "继续之前的任务"
```

最终通过 systemd：

```bash
systemctl --user enable --now \
    codex-window-keeper
```

实现长期无人值守运行。
