# Agent Efficiency Hooks

为 Codex、Claude Code 和 Hermes Agent 提供两个本地控制：重复读取提醒、指定验证命令的重复失败提醒。共用 Python 引擎，按平台注册不同事件；运行时不调用额外模型。

默认 `warn`：正常调用没有输出；同一范围成功读取两次且文件未变，在第三次读取前提醒一次。指定检查连续两次产生同样错误、输入未变，在下一次相同检查前提醒。不是代码缓存代理，不自动删除代码或重启会话。

## 安装

支持 macOS/Linux，Python 3.10+。Hermes 自动合并 YAML 需要一个仅安装时使用的依赖：

```bash
git clone https://github.com/JianchengSun826/agent-efficiency-hooks.git
cd agent-efficiency-hooks
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-install.txt
.venv/bin/python install.py --target all --check
```

预览、安装（将项目路径替换成 Hermes 实际在本机执行工具的目录）：

```bash
.venv/bin/python install.py --target all --scope user --project /absolute/path/to/project --dry-run
.venv/bin/python install.py --target all --scope user --project /absolute/path/to/project
```

`--dry-run` 不写文件，只显示本工具的新增注册和目的地，不显示已有配置内容。安装会复制运行代码、创建独立控制配置、备份并合并宿主配置。若任一配置无法安全解析，整批安装在写入前失败。保留 YAML 注释；不使用会丢失注释的降级写入。

| 平台 | 注册位置 |
| --- | --- |
| Codex | `~/.codex/hooks.json` |
| Claude Code | `~/.claude/settings.json` |
| Hermes | 默认 `~/.hermes/config.yaml`，或 `--hermes-profile /path/to/profile` |

单平台安装用 `--target codex`、`claude` 或 `hermes`。Codex/Claude 支持 `--scope project --project /path/to/repo`。Hermes 按 profile 安装，不支持 project scope；多个 profile 分别安装。不要同时在同一平台的项目层和用户层安装本工具，否则可能触发两次。

脚本复制到 `~/.local/share/agent-efficiency/runtime`，平台控制配置在 `~/.local/share/agent-efficiency/configs/`。安装输出会给出准确配置路径。重新安装保留已调整的控制配置。

重启对应程序/开启新会话。Codex 在 `/hooks` 审查并信任新增定义；Hermes 按本机 shell hook 首次使用同意流程确认。安装器不跳过宿主信任，不自动修改 feature flags。`--check` 只探测可执行程序/版本，不保证宿主已激活 hook；若旧版本缺少所需事件，需升级或停用不受支持注册。

## 启用重复失败监测（一次配置）

**默认没有监测命令。** 任意 shell 命令是否可安全重复无法自动判断，不能把网络失败、轮询或环境变化当成无效循环。编辑安装输出给出的平台控制 JSON，在 `verification` 中添加你项目的检查及影响结果的文件，例如：

```json
{
  "mode": "warn",
  "verification": [
    {
      "command": "python3 -m unittest tests.test_parser",
      "inputs": ["src/parser.py", "tests/test_parser.py", "pyproject.toml"],
      "complete": false
    }
  ]
}
```

保留配置文件其他字段，尤其是 `database` 和 Hermes 的 `cwd`。`inputs` 是相对实际调用目录的字面文件路径，不支持目录或 glob；未找到文件、单文件超过 1 MiB、超过 64 个文件或总量超过 4 MiB 时跳过此检查。包括测试、依赖配置、数据夹具等相关输入。若检查依赖网络、时间、数据库或未列出的文件，不要将 `complete` 改为 true。

命令必须精确匹配；切换命令是新实验。不从用户自然语言猜测检查目标，不扫描整个仓库。不同项目/检查需要相应配置；优先使用项目级配置隔离原生 agent。

## 模式与状态

- `observe`：只记录候选重复，模型没有提醒。
- `warn`：一次简短提醒，不阻断。默认模式。
- `enforce`：在第三次调用前拒绝高置信度重复；同一候选 300 秒内最多拒绝一次，下一次允许执行。失败重试只有 `complete: true` 才能拒绝，否则仍只提醒。

控制参数：`read_threshold`、`retry_threshold`（最少 2），`ttl_seconds`（默认 600），`deny_cooldown_seconds`（最少 30，默认 300）。状态按平台/会话/agent/实际目录隔离。仅保存散列、计数、时间和固定提醒，不保存文件内容或原始错误。

```bash
python3 ~/.local/share/agent-efficiency/runtime/hook.py --config /path/to/guard-config.json --stats
python3 ~/.local/share/agent-efficiency/runtime/hook.py --config /path/to/guard-config.json --reset
```

`--reset` 清除该数据库内全部平台的短期状态，不修改代码。统计 `events`、`repeat_candidates`、`warnings`、`denials` 不是 token 节省量；净收益需要比较宿主实际用量、耗时和任务是否完成。

## 平台差异与范围

支持 `Read`、`read_file`、`file_read` 的结构化本地读取，以及简单单命令 `cat file`、`head -n N file`、`tail -n N file`、`sed -n 'A,Bp' file`。复杂 shell、管道、展开、网络/未知工具和未完成 PTY 会话跳过。只统计有前置记录且明确成功的读取。内容在调用过程中变化时不计入已读历史。

本机 Hermes 已有 `read_file` 去重，因此默认 `hermes_monitor_read_file: false`，避免重复控制。旧 Hermes 没有此功能时可显式开启。

Hermes shell 事件中的 `cwd` 是宿主进程目录，不能代表持久 terminal 的目录。因此 terminal 监测要求明确的绝对 `workdir`，以及控制配置 `hermes_local_terminal: true`；目录未知的调用直接跳过。安装器仅在 profile 明确配置 `terminal.backend: local` 时默认开启这个标记；其他情况需先核实本地后端再开启。远端/容器后端不使用本地指纹。Hermes 的 `cwd` 控制字段只作无目录事件的后备，不覆盖工具的明确工作目录。

Hermes 后置工具事件只观察结果，提醒排队给下一次 `pre_llm_call`。本机版本在用户回合准备时触发它，因此提醒通常到下次用户回合才可见，不能宣传为每次模型推理即时纠偏；enforce 的工具前置阻断可即时生效。原生 Codex/Claude 提醒在被监测工具调用前可见。

Codex/Claude 的 SessionStart/PostCompact 重置会话状态。Hermes 的 session 生命周期和短 TTL 降低陈旧状态风险；使用不触发生命周期事件的手动上下文清空时执行 `--reset`。Hook 不是全覆盖边界，平台可能存在未经过拦截的工具路径。

Hermes 连接模型账户时，工具由 Hermes 执行，应安装 Hermes 适配器；委派给 Codex/Claude 运行时，子进程内部另需对应原生配置。三平台状态隔离，外层调用不会被当作内部读取。

## 自定义 harness 接口

`hook.py --platform harness --config ...` 接收单个 JSON，返回 `{ "action": "allow|warn|deny", "reason": "..." }`。调用方负责执行前检查 deny，并在最终完成后报告 after。字段：

```json
{
  "platform": "harness",
  "session_id": "stable-session",
  "agent_id": "worker-1",
  "call_id": "unique-call",
  "phase": "before",
  "cwd": "/absolute/project",
  "tool": "Read",
  "args": {"file_path": "src/parser.py", "offset": 1, "limit": 100},
  "status": "unknown",
  "error_signature": ""
}
```

完成时保持同一 call_id/参数，phase 改为 after，status 为 success/failure/unknown。失败检查附 error_signature，引擎仅存其散列。上下文清空时发送 phase=reset。不完整/不支持事件静默跳过；运行错误放行，stderr 仅含通用诊断。

## 卸载与验证

```bash
.venv/bin/python install.py --target all --scope user --project /absolute/project --uninstall --dry-run
.venv/bin/python install.py --target all --scope user --project /absolute/project --uninstall
.venv/bin/python -m unittest discover -s tests -v
```

使用原来的 scope/profile。卸载只删除本工具匹配的注册，保留其他 hook、独立控制配置和共享脚本（其他 profile 可能仍引用它们）。备份文件后缀为 `.agent-efficiency-backup-<timestamp>`；如需恢复整个旧配置，可在停机并确认后复制备份。

测试使用临时目录、真实 SQLite 和子进程，未对你的实时配置进行安装或启动收费模型会话。官方接口文档：[Codex](https://learn.chatgpt.com/docs/hooks)、[Claude Code](https://code.claude.com/docs/en/hooks)、[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)。
