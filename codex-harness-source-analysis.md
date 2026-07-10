# Codex Harness 源码解析：JSON 如何变成 shell 执行

日期：2026-07-10  
分析版本：`openai/codex`，本地源码提交 `dc5ae37`  
源码根目录占位：`<codex-source>`

本文只讲一条核心链路：

> 模型输出 tool call，里面带 JSON 参数；Codex 如何把它路由、解析、校验、审批、沙箱化，最后变成一次真实 shell 进程执行。

## 结论

- Codex 的 harness 不是一堆 Linux 命令。模型看到的工具是 Rust 里的 `ToolSpec`，运行时是 Rust handler。
- JSON 字符串不是 shell 解析的。`ToolRouter` 先保留原始 `arguments` 字符串，具体 handler 再用 `serde_json::from_str` 反序列化。
- shell 执行有两条路径：旧 `shell_command` 和新 `exec_command` / `write_stdin`。
- 真正强的是外层 harness：turn loop、tool router、registry、hook、approval、sandbox、network approval、process manager、output truncation、history 回灌。
- Codex 源码有明确的 agent integration test harness，但不是每次代理运行都自动 TDD。是否跑测试仍取决于 agent 指令和模型决策。

## Harness 是什么

在测试领域，test harness 通常指包住被测系统的一套驱动、桩、监控和断言设施。放到 agent 里，harness 就是把裸 LLM 包成“能在仓库里行动”的运行框架：

```text
+---------------- Codex Agent Harness ----------------+
| model turn loop                                     |
| tool specs shown to model                           |
| tool call parser/router                             |
| tool registry and lifecycle hooks                   |
| approval / sandbox / network policy                 |
| process execution and output capture                |
| result -> conversation history -> next model call    |
+-----------------------------------------------------+
```

所以它不是“工具列表”那么简单，而是一个闭环执行环境。

## 总体树状图

```text
Codex turn
|
|- session/turn.rs::run_turn
|  |- build tools for this step
|  |- send prompt + tool specs to model
|  `- stream model response
|
|- stream_events_utils.rs::handle_output_item_done
|  |- ToolRouter::build_tool_call(ResponseItem)
|  `- ToolCallRuntime::handle_tool_call(...)
|
|- tools/router.rs
|  `- ResponseItem::FunctionCall { name, arguments, call_id }
|       -> ToolCall { tool_name, call_id, payload: Function { arguments } }
|
|- tools/parallel.rs
|  |- parallel gate
|  |- cancellation behavior
|  `- router.dispatch_tool_call_with_terminal_outcome(...)
|
|- tools/registry.rs
|  |- find handler by tool_name
|  |- validate payload kind
|  |- notify tool start
|  |- run pre_tool_use hooks
|  |- handler.handle(...)
|  |- run post_tool_use hooks
|  `- convert ToolOutput -> ResponseInputItem::FunctionCallOutput
|
`- concrete handler
   |- shell_command -> ShellCommandHandler
   |  |- parse JSON -> ShellCommandToolCallParams
   |  |- build ExecParams
   |  `- ToolOrchestrator -> ShellRuntime -> spawn child process
   |
   `- exec_command -> ExecCommandHandler
      |- parse JSON -> ExecCommandArgs
      |- allocate process id
      |- build argv using shell mode
      `- UnifiedExecProcessManager -> process / exec-server
```

## JSON 到工具调用的准确边界

模型流里出现的是 `ResponseItem::FunctionCall`：

```text
name      = "shell_command" or "exec_command"
arguments = "{\"command\":\"echo hi\"}" 或 "{\"cmd\":\"echo hi\"}"
call_id   = "..."
```

源码边界如下：

```text
ResponseItem::FunctionCall
|
|- ToolRouter::build_tool_call
|  `- 不 parse JSON，只包装：
|     ToolPayload::Function { arguments }
|
|- ShellCommandHandler::handle_call
|  `- parse_arguments_with_base_path::<ShellCommandToolCallParams>(&arguments, &cwd)
|
`- ExecCommandHandler::handle_call
   `- parse_arguments::<ExecCommandEnvironmentArgs>(&arguments)
      parse_arguments_with_base_path::<ExecCommandArgs>(&arguments, native_cwd)
```

真正的 JSON parse 在：

```text
<codex-source>/codex-rs/core/src/tools/handlers/mod.rs

parse_arguments<T>(arguments: &str)
  -> serde_json::from_str(arguments)
```

`parse_arguments_with_base_path` 只是额外设置 `AbsolutePathBufGuard`，让参数里相对路径类型能按当前 cwd 解析。它最后仍然调用 `parse_arguments`。

这就是核心点：router 不执行 JSON，handler 才把 JSON 字符串变成强类型参数。

## ToolSpec 不是可执行文件

Codex 给模型看的工具 schema 在 `shell_spec.rs` 里手写出来：

```text
exec_command:
  required: cmd
  fields: cmd, workdir, tty, yield_time_ms, max_output_tokens, shell...

shell_command:
  required: command
  fields: command, workdir, timeout_ms, login...
```

这些 schema 只是“模型可见接口”。真正执行者是 Rust struct：

```text
ShellCommandHandler implements ToolExecutor<ToolInvocation>
ExecCommandHandler  implements ToolExecutor<ToolInvocation>
```

只有进入 shell runtime 后，才会真的创建系统进程。

## shell_command 路径

```text
shell_command JSON
|
|- ShellCommandHandler::handle_call
|  |- resolve primary environment
|  |- resolve workdir from JSON
|  |- parse JSON -> ShellCommandToolCallParams
|  |- shell.derive_exec_args(command, login)
|  |    example: ["bash", "-lc", "echo hi"]
|  |- create env and inject permission profile
|  `- run_exec_like(...)
|
|- run_exec_like
|  |- apply already granted turn permissions
|  |- reject illegal escalation under wrong approval policy
|  |- intercept apply_patch when applicable
|  |- emit shell begin event
|  |- ask exec_policy for approval requirement
|  |- build ShellRequest
|  `- ToolOrchestrator::run(ShellRuntime, ShellRequest)
|
|- ToolOrchestrator
|  |- approval
|  |- choose sandbox
|  |- network approval
|  |- first attempt
|  `- optional retry / escalation after sandbox denial
|
`- ShellRuntime::run
   |- wrap command with sandbox transform
   |- configure timeout / cancellation
   |- execute_env(...)
   `- exec.rs -> spawn_child_async -> tokio::process::Command
```

底层真正接近系统调用的位置：

```text
exec.rs::exec
  -> spawn_child_async(SpawnChildRequest)

spawn.rs::spawn_child_async
  -> tokio::process::Command::new(program)
  -> cmd.args(args)
  -> cmd.current_dir(cwd)
  -> cmd.env_clear(); cmd.envs(env)
  -> stdout/stderr piped
  -> cmd.kill_on_drop(true).spawn()
```

所以 shell 工具不是“一个 Linux 命令”，而是 Codex 构造出的受控子进程。

## exec_command 路径

新版 `exec_command` 更像交互式终端执行器：

```text
exec_command JSON
|
|- ExecCommandHandler::handle_call
|  |- parse environment_id/workdir
|  |- parse JSON -> ExecCommandArgs
|  |- select shell mode
|  |- allocate process_id
|  |- get_command(...)
|  |    Direct: session shell derives argv
|  |    ZshFork: zsh -c / -lc
|  |- permission and approval checks
|  |- intercept apply_patch
|  `- UnifiedExecProcessManager::exec_command(...)
|
`- UnifiedExecProcessManager
   |- open process under sandbox
   |- emit command begin event
   |- stream output
   |- collect output until yield_time_ms
   |- keep live process if still running
   `- return ExecCommandToolOutput
```

这就是为什么 `exec_command` 能返回 `process_id`，再由 `write_stdin` 继续写入或轮询。它不是一次性 shell output，而是带进程生命周期管理。

## 动态验证

我在本机执行了一次真实 Codex CLI 非交互调用：

```bash
codex -a never -s read-only -C /tmp/codex_dynamic_probe \
  exec --json --ephemeral --skip-git-repo-check --ignore-rules \
  "请只调用一次 shell/exec 工具执行这个命令：printf codex_dynamic_probe。执行后用一句话返回工具输出，不要做其他事。"
```

关键输出：

```json
{"type":"item.started","item":{"type":"command_execution","command":"/bin/bash -lc 'printf codex_dynamic_probe'","status":"in_progress"}}
{"type":"item.completed","item":{"type":"command_execution","command":"/bin/bash -lc 'printf codex_dynamic_probe'","aggregated_output":"codex_dynamic_probe","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"type":"agent_message","text":"工具输出是：codex_dynamic_probe。"}}
```

这次动态验证说明两件事：

- 对外 JSONL 事件里叫 `command_execution`，这是 CLI 展示层事件名。
- 实际执行命令是 `/bin/bash -lc 'printf codex_dynamic_probe'`，和源码里的 `shell.derive_exec_args(...)` 路径吻合。

## Codex 自己怎么测这条链路

官方集成测试不是调用真实模型，而是搭一个 mock Responses server：

```text
core/tests/suite/tool_harness.rs
|
|- start_mock_server()
|- first SSE response:
|  `- ev_function_call(call_id, "shell_command", {"command":"echo tool harness"})
|- submit user input
|- wait TurnComplete
`- inspect second request:
   `- function_call_output(call_id) contains:
      Exit code: 0
      Output:
      tool harness
```

这就是 Codex 的 agent test harness：用假模型输出驱动真 Codex agent loop，验证工具执行结果是否回灌到下一次模型请求。

仓库规则也明确偏向这种测试方式：

- agent 逻辑改动优先写 integration tests。
- integration tests 位于 `codex-rs/core/tests/suite`。
- 使用 `test_codex` 创建 Codex 测试实例。
- 不直接跑 `cargo test`，用 `just test`。
- UI 文本/渲染变化使用 `insta` snapshot。

本机没有 Rust 工具链、`just`、`cargo-nextest`，所以这次没有编译运行源码测试；动态验证用的是已安装的真实 Codex CLI。

## smoke / TDD 的源码层理解

Codex 源码里有 smoke 入口，例如 root `justfile` 的：

```text
just bench-smoke
just bench-e2e-smoke
```

但这不是“agent 每次自动跑 smoke test”。它们是开发/CI 使用的命令。Codex agent 是否自动执行测试，取决于：

- 用户指令；
- developer / AGENTS.md 规则；
- 模型是否决定调用 shell 工具；
- 当前 sandbox / approval 策略是否允许。

所以你之前在 opencode 里没看到“自动 TDD”，这个判断也适用于 Codex：源码提供了测试 harness 和规范，但 agent runtime 的默认能力是“可以调用测试命令”，不是“每轮强制自动 TDD”。

## 源码锚点

| 关注点 | 文件 |
|---|---|
| turn 主循环 | `<codex-source>/codex-rs/core/src/session/turn.rs` |
| model output -> tool future | `<codex-source>/codex-rs/core/src/stream_events_utils.rs` |
| ResponseItem -> ToolCall | `<codex-source>/codex-rs/core/src/tools/router.rs` |
| 并发、取消、tool future | `<codex-source>/codex-rs/core/src/tools/parallel.rs` |
| registry、hook、lifecycle | `<codex-source>/codex-rs/core/src/tools/registry.rs` |
| 工具可见性规划 | `<codex-source>/codex-rs/core/src/tools/spec_plan.rs` |
| JSON 参数解析 helper | `<codex-source>/codex-rs/core/src/tools/handlers/mod.rs` |
| shell_command handler | `<codex-source>/codex-rs/core/src/tools/handlers/shell/shell_command.rs` |
| shell 执行公共路径 | `<codex-source>/codex-rs/core/src/tools/handlers/shell.rs` |
| exec_command handler | `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs` |
| unified exec 参数/shell 模式 | `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec.rs` |
| approval/sandbox orchestrator | `<codex-source>/codex-rs/core/src/tools/orchestrator.rs` |
| shell runtime | `<codex-source>/codex-rs/core/src/tools/runtimes/shell.rs` |
| unified exec process manager | `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs` |
| 最终 spawn | `<codex-source>/codex-rs/core/src/exec.rs`, `<codex-source>/codex-rs/core/src/spawn.rs` |
| agent integration test | `<codex-source>/codex-rs/core/tests/suite/tool_harness.rs` |

## 一句话版

Codex 的核心科技不是“把 JSON 直接丢给 bash”，而是：

```text
FunctionCall JSON string
  -> ToolRouter 保留原始 arguments
  -> ToolRegistry 找到强类型 handler
  -> handler 用 serde_json 解析和校验
  -> ToolOrchestrator 统一审批/沙箱/网络策略
  -> ShellRuntime / UnifiedExecProcessManager 执行进程
  -> ToolOutput 格式化成 function_call_output
  -> 下一次模型请求继续推理
```

这整个闭环才是 harness。

## 参考

- OpenAI Codex source: https://github.com/openai/codex
- ISTQB test harness glossary: https://glossary.istqb.org/en_US/term/test-harness
- Agent harness paper: https://arxiv.org/abs/2606.10106
