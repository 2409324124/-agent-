# Codex Harness 源码解析：从 JSON 到 Shell 进程的完整执行链路

**日期**：2026-07-10

**分析版本**：`openai/codex` (Commit: `dc5ae37`)

**源码根目录占位**：`<codex-source>`

**本文只聚焦一条核心链路**：模型输出携带 JSON 参数的 Tool Call 后，Codex 如何完成路由、解析、校验、审批、沙箱化，最终生成真实的 OS Shell 进程。

##  TL;DR（一句话总结）

Codex 的核心科技不是“把 JSON 直接丢给 Bash”，而是：
`FunctionCall JSON` ➔ `ToolRouter` 兜底暂存 ➔ `ToolRegistry` 匹配强类型 Handler ➔ `serde_json` 解析校验 ➔ `ToolOrchestrator` 统一接管审批/沙箱策略 ➔ `ShellRuntime` 真正拉起子进程 ➔ 返回 `ToolOutput` 喂给下一轮模型推理。

---

## 1. 核心结论

* **Harness 本质是闭环环境，而非命令集合**：模型看到的工具是 Rust 定义的 `ToolSpec`，真正在底层干活的是 Rust Handler。
* **JSON 解析边界后置**：外层 `ToolRouter` 不解析 JSON，仅保留原始 `arguments` 字符串；真正的反序列化由具体的 Handler（如 `ShellCommandHandler`）完成。
* **双擎执行路径**：分为传统的 `shell_command` 路径和带有生命周期管理的 `exec_command` / `write_stdin` 路径。
* **Agent TDD 并非强制**：源码内有完善的 Agent Integration Test 框架，但 Runtime 时是否自动跑测试，完全取决于用户的 Prompt 策略和当前 Sandbox 权限，并非默认强制触发。

## 2. 总体调用树状图

整个执行流由顶层 Turn Loop 驱动，逐层向下深入：

```text
Codex turn
├── session/turn.rs::run_turn (收发模型请求)
├── stream_events_utils.rs (捕获并分发事件)
│   └── ToolCallRuntime::handle_tool_call
├── tools/router.rs (组装 ToolCall，拦截原始 JSON)
├── tools/registry.rs (匹配 Handler，触发生命周期 Hook)
└── Concrete Handler (具体执行逻辑)
    ├── shell_command ➔ ShellCommandHandler (解析 JSON -> 构建执行参数 -> 拉起子进程)
    └── exec_command  ➔ ExecCommandHandler (解析 JSON -> 分配 PID -> 接入 Sandbox)

```

## 3. JSON 解析的真实边界

模型输出的 `ResponseItem::FunctionCall` 数据长这样：

```json
{
  "name": "exec_command",
  "arguments": "{\"cmd\":\"echo hi\"}",
  "call_id": "call_abc123"
}

```

**源码解析链路**：

1. `ToolRouter::build_tool_call`：直接透传，仅封装为 `ToolPayload::Function { arguments }`。
2. 抵达 `ExecCommandHandler::handle_call`（或 `ShellCommandHandler`）。
3. 调用底层泛型方法：`<codex-source>/codex-rs/core/src/tools/handlers/mod.rs`
```rust
// 真正的解析发生在这里
parse_arguments<T>(arguments: &str) -> serde_json::from_str(arguments)

```



## 4. 进程拉起路径对比

### 路径 A：`shell_command`（一次性命令）

该工具的核心是**构建受控的子进程**。

```text
ShellCommandHandler::handle_call
 ├── 解析 JSON 并提取工作目录 (workdir)
 ├── 派生执行参数 (例如：["bash", "-lc", "echo hi"])
 └── run_exec_like() ➔ ToolOrchestrator::run()
      ├── 触发 Sandbox 与 Network Approval
      └── ShellRuntime::run()
           └── tokio::process::Command::new(program).spawn()

```

### 路径 B：`exec_command`（持久化终端）

新版工具具备**进程生命周期管理**能力，支持后续的 stdin 交互。

```text
ExecCommandHandler::handle_call
 ├── 解析 JSON，选择 Shell 模式 (Direct / ZshFork)
 ├── 分配 process_id
 └── UnifiedExecProcessManager::exec_command()
      ├── 在 Sandbox 下拉起进程
      ├── 持续推流 Output (直到 yield_time_ms)
      └── 返回带有 process_id 的 ToolOutput

```

## 5. CLI 动态验证实例

通过本地非交互式命令绕过外层规则进行动态探针测试：

```bash
codex -a never -s read-only -C /tmp/codex_dynamic_probe \
  exec --json --ephemeral --skip-git-repo-check --ignore-rules \
  "请只调用一次 shell 工具执行 printf codex_dynamic_probe。"

```

**底层事件输出捕获**：

```json
// 1. 发起命令 (CLI 展示层事件包装为 command_execution)
{"type":"item.started","item":{"type":"command_execution","command":"/bin/bash -lc 'printf codex_dynamic_probe'"}}

// 2. 执行完毕 (Exit Code 0，捕获 stdout)
{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"codex_dynamic_probe","exit_code":0}}

```

实测证明：即使输入的是简单命令，Codex 依然会走 `shell.derive_exec_args()` 路径，将其包装为 `/bin/bash -lc '...'` 来执行。

## 6. 核心源码文件索引 (Anchors)

| 核心逻辑 | 源码路径 |
| --- | --- |
| **Turn 主循环** | `core/src/session/turn.rs` |
| **JSON 提取与路由** | `core/src/tools/router.rs` |
| **解析 JSON Helper** | `core/src/tools/handlers/mod.rs` |
| **Shell Command Handler** | `core/src/tools/handlers/shell/shell_command.rs` |
| **Exec Command Handler** | `core/src/tools/handlers/unified_exec/exec_command.rs` |
| **沙箱/审批编排器** | `core/src/tools/orchestrator.rs` |
| **底层拉起 (Spawn)** | `core/src/exec.rs`, `core/src/spawn.rs` |
| **Agent 集成测试框架** | `core/tests/suite/tool_harness.rs` |



## 参考

- OpenAI Codex source: https://github.com/openai/codex
- ISTQB test harness glossary: https://glossary.istqb.org/en_US/term/test-harness
- Agent harness paper: https://arxiv.org/abs/2606.10106
