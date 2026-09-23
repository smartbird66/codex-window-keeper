# codex-window-keeper

> **This tool does not bypass Codex rate limits.**
>
> It waits for the actual 5-hour window reset recorded by Codex and then runs one
> normal Codex CLI command configured by the user.

`codex-window-keeper` 是一个面向 Linux 的轻量守护程序。它只读扫描本机 Codex CLI
session JSONL 中的 rate-limit 元数据，找到 `primary.window_minutes == 300` 对应的真实
`resets_at`，等待 reset 和 grace period 后执行一次命令。成功后它会等待 Codex 写入不同的
`resets_at`，**不会用旧时间加 5 小时来推算下一窗口**。

注意：codex exec 默认要求在 Git 仓库内运行。

默认 wake 命令是一个保守的小请求：

```console
codex exec "hello"
```

自动恢复真实任务、恢复指定 session 或运行 shell 命令都必须显式配置。

也可以选择固定本地时间唤醒，例如每天早上 06:00：

```console
codex-window-keeper --daily-at 06:00
```

## 工作原理

```text
scan newest session tails -> wait for actual resets_at -> grace period
        -> execute once -> persist exact triggered timestamp
        -> wait until Codex records a different resets_at -> repeat
```

- 默认扫描 `${CODEX_HOME:-~/.codex}/sessions/**/*.jsonl` 中按 mtime 排序的最近 50 个文件。
- 每个文件只读最后 2 MiB，并从后往前找最新有效事件。
- 损坏行、写入中的半行、未知事件和新增字段会被忽略。
- 状态默认原子写入 `~/.local/state/codex-window-keeper/state.json`，用于防止服务重启后重复触发。
- 配置 `daily_at` 时，按服务器本地日历每天执行一次，并以日期持久化去重。
- session 不会被修改或上传；工具不提供 telemetry 或账户轮换。

## 环境与安装

要求 Linux、Python 3.11+，以及可在 `PATH` 中运行的 Codex CLI。

推荐使用 pipx：

```bash
git clone <repository-url> codex-window-keeper
cd codex-window-keeper
pipx install .
codex-window-keeper --help
```

也可以安装到当前 Python 环境：

```bash
python3 -m pip install .
```

升级本地 checkout 后：

```bash
pipx install --force .
```

## 先检查状态

这是只读操作，不会调用 Codex API：

```bash
codex-window-keeper --status
```

输出包含数据来源文件、300 分钟窗口、使用率、Unix reset 时间、本地时间、剩余时间和状态。
常见状态包括 `WAIT_FOR_RESET`、`GRACE_PERIOD`、`READY_TO_TRIGGER`、`RETRY`、
`WAIT_NEW_RESET` 和 `RETRIES_EXHAUSTED`。

首次部署建议先执行：

```bash
codex-window-keeper --mode wake --dry-run --once
```

如果 reset 尚未到达，它仍会等待；到点后只记录本应执行的命令，不启动子进程，也不写持久状态。

## 每日定时唤醒

每天早上 06:00 发送默认的 `hello`：

```bash
codex-window-keeper --daily-at 06:00
```

`--wake-at 06:00` 是等价别名。TOML 配置为：

```toml
mode = "wake"
prompt = "hello"
daily_at = "06:00"
```

时间使用服务器本地时区。服务在线时会在 06:00 后的第一个轮询周期执行；如果服务当天晚于
06:00 才启动且尚未成功执行，会补执行一次。成功日期写入状态文件，因此当天重启不会重复发送。
次日自动恢复等待。失败仍遵循 `max_retries`、`retry_delay_seconds` 和 command timeout。

配置 `daily_at` 后，固定时间调度会替代基于 `resets_at` 的自动触发；`--status` 仍会显示真实
5 小时窗口，方便确认 06:00 请求是否产生了预期的 11:00 reset。这样不会让旧的 reset 循环
整夜持续启动窗口而破坏早晨的锚点。

定时命令不会绕过或强制重置服务端窗口：如果 06:00 时已有未到期窗口，`hello` 只会使用当前
窗口，下一次 reset 仍以 Codex 返回的真实 `resets_at` 为准。

可先做无副作用验证；若当天时间已过，该命令会立即打印一次计划动作后退出：

```bash
codex-window-keeper --daily-at 06:00 --dry-run --once
```

## 执行模式

### Wake（默认）

```bash
codex-window-keeper --mode wake
```

等价的默认 Codex argv 是 `codex exec "hello"`。可用 `--prompt` 修改提示，但提示正文不会写入
keeper 日志。

### Resume last

Codex CLI 的 `--last` 会按当前工作目录筛选最近 session，因此请配置正确的项目目录：

```bash
codex-window-keeper \
  --mode resume-last \
  --workdir ~/ReleasedCode/EGGPlacer \
  --prompt "继续之前尚未完成的任务"
```

### Resume session

`resume` 与 `resume-session` 是同一个模式：

```bash
codex-window-keeper \
  --mode resume-session \
  --session-id 019xxxxx-xxxx-xxxx \
  --workdir ~/ReleasedCode/EGGPlacer \
  --prompt "继续之前的任务"
```

当前实现按 Codex CLI 0.155.1 的实际参数顺序调用：
`codex exec resume SESSION_ID PROMPT`。`resume-last` 调用
`codex exec resume --last PROMPT`。

### Custom command

仅对可信的静态命令使用此高级模式：

```bash
codex-window-keeper \
  --command './scripts/run-codex-task.sh' \
  --workdir ~/project
```

`--command` 在未显式指定 mode 时自动选择 `command` 模式；`custom-command` 是其别名。
命令通过 `/bin/sh -c` 执行，并继承 keeper 当前用户的权限。**Only configure commands you
trust.** 不要把网络输入、未转义变量或秘密直接拼接进命令；custom command 只会在
dry-run 输出中按原文显示，正常执行日志会将它隐藏。

## 配置

优先级为：CLI > 环境变量 > TOML > 内建默认值。

默认配置文件是 `~/.config/codex-window-keeper/config.toml`。可从
[`config.example.toml`](config.example.toml) 开始：

```bash
mkdir -p ~/.config/codex-window-keeper
cp config.example.toml ~/.config/codex-window-keeper/config.toml
```

常用参数：

```text
--mode wake|resume-last|resume|resume-session|command|custom-command
--prompt TEXT                 --session-id ID
--command COMMAND             --workdir PATH
--codex-home PATH             --config PATH
--daily-at HH:MM              --wake-at HH:MM
--grace-seconds N             --poll-seconds N
--max-session-files N         --tail-bytes N
--max-retries N               --retry-delay-seconds N
--command-timeout SECONDS     --once --dry-run --status
```

支持的环境变量：

```text
CODEX_HOME
CODEX_KEEPER_MODE
CODEX_KEEPER_COMMAND
CODEX_KEEPER_WORKDIR
CODEX_KEEPER_PROMPT
CODEX_KEEPER_SESSION_ID
CODEX_KEEPER_GRACE_SECONDS
CODEX_KEEPER_POLL_SECONDS
CODEX_KEEPER_MAX_SESSION_FILES
CODEX_KEEPER_TAIL_BYTES
CODEX_KEEPER_MAX_RETRIES
CODEX_KEEPER_RETRY_DELAY
CODEX_KEEPER_COMMAND_TIMEOUT
CODEX_KEEPER_STATE_FILE
CODEX_KEEPER_DAILY_AT
```

`max_retries = 3` 表示第一次尝试失败后最多再重试 3 次。默认间隔 300 秒，绝不会高频重试。
默认 wake timeout 为 300 秒，其余模式为 1800 秒。超时或服务停止时先向整个子进程组发送
SIGTERM，等待后再发送 SIGKILL。

`--once` 会等待并完成一次成功触发后退出 0；所有尝试都失败则退出非零。reset 模式成功后
进入 `WAIT_NEW_RESET`，直到在 session 中读到新的真实时间戳；daily 模式则等待下一本地日期。

## systemd user service

先完成 `--status` 与 `--dry-run` 验证，并写好配置文件，再启用服务：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-window-keeper.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-window-keeper.service
```

查看状态和日志：

```bash
systemctl --user status codex-window-keeper.service
journalctl --user -u codex-window-keeper.service -f
```

某些服务器在 SSH 退出后会停止 user manager，可由管理员/用户按系统策略手动启用 linger：

```bash
loginctl enable-linger "$USER"
```

本项目不会自动执行该命令，也不会自动修改用户的 systemd 或 Codex 配置。

## 已验证的 Codex 数据格式

开发时在 Linux 上针对 `codex-cli 0.155.1`（2026-09-21）进行了只读验证。真实记录为：

```json
{
  "type": "event_msg",
  "payload": {
    "type": "token_count",
    "rate_limits": {
      "primary": {
        "used_percent": 54.0,
        "window_minutes": 300,
        "resets_at": 1789988781
      },
      "secondary": {
        "used_percent": 39.0,
        "window_minutes": 10080,
        "resets_at": 1790529878
      },
      "credits": {
        "has_credits": false,
        "unlimited": false,
        "balance": null
      }
    }
  }
}
```

以上数值只用于说明字段类型。扫描器递归寻找 `rate_limits`，只接受
`primary.window_minutes == 300` 和正整数 `resets_at`；也兼容 `rate_limits` 位于 JSON 顶层。
它不依赖完整 event schema，也不返回或打印原始记录、prompt 或代码。

Codex session JSONL 是内部格式，未来可能变化。如果 `--status` 在 Codex 已有活动后仍找不到
窗口，请参阅 [`docs/troubleshooting.md`](docs/troubleshooting.md)，并在提交兼容性报告时只提供
脱敏后的字段结构，绝不要附上完整 session。

## 开发与验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy src
```

测试不会真实调用 Codex；执行器测试使用 `/bin/true`、`/bin/false` 和短时本地子进程。

## 安全边界与已知范围

- v0.1 面向 Linux Codex CLI 本地 session；不支持 Windows native、ChatGPT Web、GUI 自动化或
  多账户轮换。
- 不判断 weekly quota 是否耗尽；服务器拒绝命令时按低频 retry policy 处理。
- Codex 只有在一次正常请求后才会写入新窗口。若命令成功但日志写入延迟，keeper 会一直停留在
  `WAIT_NEW_RESET`，不会自行猜测下一时间。
- 每日调度依赖服务器本地时钟及时区；修改系统时钟/时区可能改变下一次执行时间。跨重启仍以
  本地 ISO 日期去重。
- attempt 会在启动子进程前落盘，success 会在子进程返回后落盘；若主机恰好在命令实际成功与
  success 状态落盘之间断电，恢复后可能按 retry policy 再执行一次。custom command 应尽量可幂等。
- `resume-last` 遵循 Codex 当前的工作目录筛选行为；目录选错可能恢复不到预期 session。
- custom command 具有当前用户权限并使用 shell；配置文件与脚本的安全由用户负责。

更多设计细节见 [`docs/architecture.md`](docs/architecture.md)。许可证为 MIT。
