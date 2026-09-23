请阅读仓库根目录的 DEVELOPMENT_SPEC.md，并严格按照其中定义的需求实现 codex-window-keeper。

开始编码前，先调查当前机器上的真实 Codex CLI 环境，包括 codex --version、codex exec --help、codex exec resume --help，并检查 ~/.codex/sessions/**/*.jsonl 中真实的 rate_limits、window_minutes 和 resets_at 数据结构。不要输出或记录 session 中与本任务无关的 prompt、代码或其他敏感内容。

实现时按以下顺序推进：

建立 Python 项目骨架。
优先实现 Codex 5h window detector。
实现 --status 并使用本机真实 Codex session 验证解析正确性。
为 detector 编写完整单元测试。
实现持久化状态和防重复触发。
实现 wake、resume-last、resume-session 和 custom-command 模式。
实现 --dry-run、--once、retry、grace period 和 command timeout。
实现可测试的核心状态机。
添加 systemd user service。
完成 README、安装说明和故障排查。
运行全部测试和静态检查并修复发现的问题。

不要简单通过 reset + 5 hours 推算下一轮时间。每次成功触发以后，都应等待 Codex 写入新的真实 resets_at。

不要修改或删除任何 Codex session，不要修改用户现有 Codex 配置，不要为了测试大量消耗 Codex quota，不要使用用户的手动重置卡，尽量使用比较小的模型重置limit。

除非 DEVELOPMENT_SPEC.md 与当前真实 Codex CLI 行为发生冲突，否则以该说明书为需求来源。如果发现 Codex 当前版本的数据格式或 CLI 参数已经变化，应优先适配当前实际行为，并在 README 和代码注释中记录兼容性处理。

完成后请给出：

实现的功能摘要；
最终仓库目录结构；
测试结果；
实际验证过的 Codex 数据格式；
安装与启动命令；
仍存在的已知限制。