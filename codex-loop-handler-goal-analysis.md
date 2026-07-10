# Codex Handler / Loop / Goal 源码解析：agent 如何持续做事

日期：2026-07-10
分析版本：`openai/codex` commit `dc5ae37`
源码根目录占位：`<codex-source>`

这篇只讲三件事：

1. `handler` 到底是什么。
2. Codex 的 loop 工程怎么把“工具调用 -> 工具结果 -> 再问模型”串起来。
3. `/goal` 为什么能让任务跨 turn 持续推进。

先说结论：

- `handler` 不是 Linux 命令，也不是一个个外部可执行文件。它是 Rust 里实现 `ToolExecutor` 的工具适配器。
- 真正的 agent loop 不在某个“神秘自我意识模块”里，而在 `run_turn` / `try_run_sampling_request` 的双层循环里。
- `needs_follow_up` 是工具闭环的关键开关。模型一旦发出 tool call，Codex 执行工具，把结果写进 history，然后再发下一次 sampling request。
- `/goal` 不是普通聊天命令那么简单。TUI 有 slash 入口，app-server 有 `thread/goal/*` 协议，state DB 持久化目标，goal extension 再把 `get_goal/create_goal/update_goal` 暴露给模型。
- “自我决策”本质是：模型决定下一步输出什么，运行时负责把状态、工具、权限、目标、结果稳稳地接回下一轮模型输入。

## 1. 一张总图

```text
+================================================================================+
|                                 Codex Agent Loop                               |
+================================================================================+

  user input / pending input / goal steering
        |
        v
  +-----------------------------+
  | session/turn.rs::run_turn   |
  | outer loop                  |
  +-------------+---------------+
                |
                | build history + advertised tools
                v
  +-----------------------------+
  | run_sampling_request        |
  | build ToolRouter            |
  | build ToolCallRuntime       |
  +-------------+---------------+
                |
                | stream Responses events
                v
  +-----------------------------+
  | try_run_sampling_request    |
  | inner stream loop           |
  +-------------+---------------+
                |
                | OutputItemDone
                v
  +-----------------------------+
  | ToolRouter::build_tool_call |
  | FunctionCall -> ToolCall    |
  +-------------+---------------+
                |
                | ToolCall { name, call_id, JSON string args }
                v
  +-----------------------------+
  | ToolCallRuntime             |
  | queue tool future           |
  +-------------+---------------+
                |
                v
  +-----------------------------+
  | ToolRegistry                |
  | find handler by tool name   |
  +-------------+---------------+
                |
        +-------+---------+------------------+
        |                 |                  |
        v                 v                  v
  ShellCommand       ExecCommand        PlanHandler / GoalTool / MCP / ...
  handler            handler
        |                 |
        | parse JSON      | parse JSON
        v                 v
  real process       unified process
        |
        v
  ToolOutput -> FunctionCallOutput -> history
        |
        v
  needs_follow_up = true
        |
        v
  run_turn continues and samples model again
```

这个图里最容易误解的点是：`ToolRouter` 不负责执行，`ToolRegistry` 不负责业务，`handler` 才是工具自己的执行入口。

## 2. Handler 是什么

源码锚点：

- `<codex-source>/codex-rs/core/src/tools/registry.rs:44`
- `<codex-source>/codex-rs/core/src/tools/context.rs:54`
- `<codex-source>/codex-rs/core/src/tools/handlers/shell/shell_command.rs:140`
- `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs:80`
- `<codex-source>/codex-rs/core/src/tools/handlers/plan.rs:48`

`handler` 可以直接理解成“工具适配器”：

```text
ToolCall
  tool_name: "exec_command"
  call_id: "call_xxx"
  payload: Function { arguments: "{\"cmd\":\"pwd\"}" }
        |
        v
ToolInvocation
  session
  turn
  step_context
  cancellation_token
  tracker
  call_id
  tool_name
  payload
        |
        v
ExecCommandHandler::handle_call(invocation)
        |
        +-- parse JSON arguments
        +-- resolve environment / cwd
        +-- apply shell mode / sandbox constraints
        +-- allocate process id
        +-- call UnifiedExecProcessManager
        `-- return ToolOutput
```

`CoreToolRuntime` 是 Codex core 对本地工具 handler 的统一约束。它继承 `ToolExecutor<ToolInvocation>`，并补充这些运行时能力：

- `matches_kind`：这个 handler 接哪类 payload。
- `waits_for_runtime_cancellation`：取消时是否等 handler 做收尾。
- `pre_tool_use_payload` / `post_tool_use_payload`：给 hook 和审计用的输入输出。
- `with_updated_hook_input`：hook 如果改写输入，如何还原成新的 invocation。
- telemetry / diff consumer：观测和流式参数 diff。

所以 handler 不是“工具本身的名字”，也不是系统命令。它是这层结构：

```text
tool spec     : 告诉模型工具叫什么、参数 schema 是什么
tool router   : 把模型输出包成 ToolCall
tool registry : 根据 tool_name 找 handler
handler       : 解析参数、跑业务、返回 ToolOutput
```

举三个对比：

```text
ShellCommandHandler
  -> 最终会启动 shell 子进程

PlanHandler
  -> 不启动进程，只 parse update_plan JSON，然后发送 EventMsg::PlanUpdate

GoalToolExecutor
  -> 不启动进程，读写 goal 状态库，然后发 goal update 事件
```

这就是边界：只有 shell/exec 这类 handler 会落到系统进程。绝大多数工具只是 Rust 逻辑。

## 3. JSON 字符串在哪里变成参数

源码锚点：

- `<codex-source>/codex-rs/core/src/tools/router.rs:112`
- `<codex-source>/codex-rs/core/src/tools/handlers/shell/shell_command.rs:196`
- `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs:120`
- `<codex-source>/codex-rs/core/src/tools/handlers/plan.rs:101`

模型流里来的 `arguments` 是字符串。`ToolRouter::build_tool_call` 做的事很薄：

```text
ResponseItem::FunctionCall { name, namespace, arguments, call_id }
        |
        v
ToolCall {
  tool_name,
  call_id,
  payload: ToolPayload::Function { arguments }
}
```

注意：这里没有 `serde_json::from_str`。它只是把东西装进 `ToolCall`。

真正 parse 在具体 handler：

```text
shell_command:
  resolve_workdir_base_path(arguments, environment_cwd)
  parse_arguments_with_base_path<ShellCommandToolCallParams>(arguments, cwd)
  run_exec_like(...)

exec_command:
  let arguments = payload.function.arguments
  parse_arguments<ExecCommandArgs>(arguments)
  get_command(...)
  UnifiedExecProcessManager::exec_command(...)

update_plan:
  serde_json::from_str::<UpdatePlanArgs>(arguments)
  EventMsg::PlanUpdate(args)
```

所以可以把边界画成这样：

```text
model JSON string
     |
     v
router: keep as string
     |
     v
registry: choose handler
     |
     v
handler: serde_json::from_str<T>
     |
     v
typed params
     |
     v
tool-specific execution
```

这也解释了为什么 schema 边界重要：模型侧看到的是 tool spec / JSON schema，运行时侧最终靠 handler 的强类型反序列化兜底。schema 是“给模型看的合同”，handler parse 是“运行时真的验收”。

## 4. Loop 工程：两个循环，不是一团 while true

源码锚点：

- `<codex-source>/codex-rs/core/src/session/turn.rs:142`
- `<codex-source>/codex-rs/core/src/session/turn.rs:224`
- `<codex-source>/codex-rs/core/src/session/turn.rs:284`
- `<codex-source>/codex-rs/core/src/session/turn.rs:318`
- `<codex-source>/codex-rs/core/src/session/turn.rs:372`
- `<codex-source>/codex-rs/core/src/session/turn.rs:1112`
- `<codex-source>/codex-rs/core/src/session/turn.rs:2004`
- `<codex-source>/codex-rs/core/src/stream_events_utils.rs:318`

### 4.1 外层 loop：一个 turn 里可以多次问模型

`run_turn` 的注释已经把设计讲清楚了：

```text
如果模型请求 function call：
  执行它
  把输出放回下一次 sampling request

如果模型只发 assistant message：
  记录消息
  turn 完成
```

外层结构是：

```text
run_turn(...)
  prepare context / hooks / input

  loop {
    pending_input = maybe_drain_input_queue()
    step_context = capture_step_context()
    history = clone_history().for_prompt(...)

    (result, request_input) = run_sampling_request(history, tools, ...)

    model_needs_follow_up = result.needs_follow_up
    has_pending_input = input_queue.has_pending_input(...)

    needs_follow_up = model_needs_follow_up || has_pending_input

    if needs_follow_up && context_limit_reached:
        run_auto_compact(...)
        continue

    if !needs_follow_up:
        run_stop_hooks(...)
        break

    continue
  }
```

这里的关键变量就是 `needs_follow_up`。

```text
needs_follow_up = true
  说明 history 里新加入了东西，模型还应该再看一次：
    - 工具输出
    - pending user input
    - stop hook continuation prompt
    - end_turn=false

needs_follow_up = false
  说明模型没有要求工具，也没有新的输入，turn 可以收尾。
```

### 4.2 内层 loop：读模型流，发现工具就启动 handler

`run_sampling_request` 先构建工具系统：

```text
built_tools(...)
  -> ToolRouter

ToolCallRuntime::new(router, session, step_context, turn_diff_tracker)

build_prompt(history, router, turn_context, base_instructions)
```

然后 `try_run_sampling_request` 读 Responses stream：

```text
try_run_sampling_request(...)
  in_flight = []
  needs_follow_up = false

  loop {
    event = stream.next()

    if OutputItemDone(item):
      output = handle_output_item_done(item)

      if output.tool_future:
        in_flight.push(output.tool_future)

      needs_follow_up |= output.needs_follow_up

    if Completed { end_turn }:
      if end_turn == false:
        needs_follow_up = true
      break SamplingRequestResult { needs_follow_up }
  }

  drain_in_flight(in_flight)
  return SamplingRequestResult
```

`handle_output_item_done` 是内层 loop 和工具执行的分叉口：

```text
OutputItemDone(item)
        |
        v
ToolRouter::build_tool_call(item)
        |
        +-- Ok(Some(call))
        |     record tool call item
        |     tool_runtime.handle_tool_call(call)
        |     output.tool_future = Some(...)
        |     output.needs_follow_up = true
        |
        +-- Ok(None)
        |     finalize assistant/reasoning message
        |     last_agent_message = ...
        |
        `-- Err(RespondToModel(message))
              write synthetic FunctionCallOutput
              output.needs_follow_up = true
```

这就是“工具调用之后为什么模型会继续”的具体机制：handler 的返回值最后会写成 `FunctionCallOutput`，而 `needs_follow_up=true` 会让外层 `run_turn` 再跑一次 sampling request。

## 5. “自我决策”是怎么实现的

这里要把话说实：Codex 不是在 Rust 里写了一个“思考算法”去决定下一步做什么。下一步要发消息、调工具、继续还是停，主要来自模型的下一个 `ResponseItem`。

运行时做的是约束和闭环：

```text
           +----------------------+
           |  history + context   |
           |  tools + goal state  |
           +----------+-----------+
                      |
                      v
              model sampling
                      |
        +-------------+-------------+
        |                           |
        v                           v
 assistant message            function_call
        |                           |
        |                           v
        |                     handler executes
        |                           |
        |                           v
        |                  function_call_output
        |                           |
        +-------------+-------------+
                      |
                      v
              record into history
                      |
                      v
            needs_follow_up?
              yes -> sample again
              no  -> finish turn
```

所以所谓“自我决策”分两层：

```text
模型层：
  根据 prompt、history、tool spec、tool output 选择下一步输出。

运行时层：
  决定哪些工具可见。
  决定工具能不能跑。
  决定输出如何落盘。
  决定什么时候 follow up。
  决定什么时候压缩上下文。
  决定 goal 是否继续拉起新 turn。
```

这套工程强的地方不是某一个点，而是“每一步都能回到 transcript”。工具不是旁路执行完就结束，而是变成模型下一轮可见的事实。

## 6. `/goal` 是什么

源码锚点：

- `<codex-source>/codex-rs/tui/src/slash_command.rs:42`
- `<codex-source>/codex-rs/tui/src/slash_command.rs:122`
- `<codex-source>/codex-rs/tui/src/chatwidget/tests/slash_commands.rs:663`
- `<codex-source>/codex-rs/tui/src/app/event_dispatch.rs:798`
- `<codex-source>/codex-rs/tui/src/app/thread_goal_actions.rs:128`
- `<codex-source>/codex-rs/tui/src/app_server_session.rs:923`
- `<codex-source>/codex-rs/app-server-protocol/src/protocol/common.rs:539`
- `<codex-source>/codex-rs/app-server/src/request_processors/thread_goal_processor.rs:37`
- `<codex-source>/codex-rs/state/src/model/thread_goal.rs:12`
- `<codex-source>/codex-rs/ext/goal/src/spec.rs:9`
- `<codex-source>/codex-rs/ext/goal/src/runtime.rs:359`
- `<codex-source>/codex-rs/ext/goal/src/steering.rs:45`

### 6.1 `/goal` 的入口链路

TUI 明确注册了 `SlashCommand::Goal`，说明是“set or view the goal for a long-running task”。测试里能看到具体行为：

```text
/goal improve benchmark coverage
  -> AppEvent::SetThreadGoalDraft { draft.objective, mode: ConfirmIfExists }

/goal
  -> AppEvent::OpenThreadGoalMenu

/goal clear
  -> AppEvent::ClearThreadGoal

/goal pause
  -> AppEvent::SetThreadGoalStatus { status: Paused }

/goal resume
  -> AppEvent::SetThreadGoalStatus { status: Active }
```

然后 app 分发：

```text
AppEvent::SetThreadGoalDraft
  -> app/thread_goal_actions.rs::set_thread_goal_draft
       |- maybe thread_goal_get, 看是否要确认覆盖
       |- materialize_goal_draft, 大目标/粘贴/图片会落成附件文件
       |- replacing 时先 thread_goal_clear
       `- app_server.thread_goal_set(...)

AppEvent::SetThreadGoalStatus
  -> app/thread_goal_actions.rs::set_thread_goal_status
       `- app_server.thread_goal_set(objective=None, status=...)

AppEvent::ClearThreadGoal
  -> app/thread_goal_actions.rs::clear_thread_goal
       `- app_server.thread_goal_clear(...)
```

再往下就是 app-server typed request：

```text
TUI AppServerSession
  thread_goal_get   -> ClientRequest::ThreadGoalGet
  thread_goal_set   -> ClientRequest::ThreadGoalSet
  thread_goal_clear -> ClientRequest::ThreadGoalClear

app-server protocol
  "thread/goal/get"
  "thread/goal/set"
  "thread/goal/clear"

notifications
  "thread/goal/updated"
  "thread/goal/cleared"
```

所以 `/goal` 不是直接给模型的一句话，也不是某个 shell 命令。它先改变 thread 的持久 goal 状态。

### 6.2 goal 的状态模型

`ThreadGoal` 存在 state DB 里，字段很直白：

```text
ThreadGoal
  thread_id
  goal_id
  objective
  status
  token_budget
  tokens_used
  time_used_seconds
  created_at
  updated_at
```

状态枚举：

```text
active
paused
blocked
usage_limited
budget_limited
complete
```

DB 层有两个关键动作：

```text
replace_thread_goal(...)
  插入或替换当前 thread 的 goal

update_thread_goal(...)
  更新 objective/status/token_budget

account_thread_goal_usage(...)
  累计 time_used_seconds / tokens_used
  如果 tokens_used >= token_budget，把 active 推到 budget_limited
```

这说明 `/goal` 是有账本的，不只是 UI 上显示一个标题。

### 6.3 goal 也是模型可调用工具

goal extension 又把目标能力暴露成模型工具：

```text
get_goal
create_goal
update_goal
```

对应源码在 `<codex-source>/codex-rs/ext/goal/src/spec.rs`。

这里有一个很关键的权限边界：

```text
create_goal:
  只有用户或系统/开发者明确要求时才创建 goal。
  token_budget 也只能在明确要求时设置。

update_goal:
  模型只能把 goal 标成 complete 或 blocked。
  pause / resume / budget_limited / usage_limited 由用户或系统控制。
```

`GoalToolExecutor` 的执行树：

```text
GoalToolExecutor::handle(...)
        |
        +-- get_goal
        |     state_db.thread_goals().get_thread_goal(...)
        |     -> JSON ToolOutput
        |
        +-- create_goal
        |     parse CreateGoalRequest
        |     validate objective / token_budget
        |     insert_thread_goal(status=Active)
        |     mark_current_turn_goal_active
        |     emit thread_goal_updated
        |
        `-- update_goal
              parse UpdateGoalArgs
              allow only Complete | Blocked
              account_active_goal_progress(...)
              update_thread_goal(status=...)
              clear_current_turn_goal
              emit thread_goal_updated
```

这就是当前对话里能看到 `get_goal/create_goal/update_goal` 工具的原因。它们不是普通 core tools，而是 goal extension 提供的模型工具。

### 6.4 goal 如何驱动自动继续

核心在 `GoalRuntimeHandle`。

当外部 `/goal` 设置了 active goal：

```text
thread/goal/set
  -> GoalService::set_thread_goal
  -> GoalSetOutcome::apply_runtime_effects
  -> GoalRuntimeHandle::apply_external_goal_set
```

`apply_external_goal_set` 看到 active 状态时会做几件事：

```text
status = Active
  if 当前有 turn:
    mark_current_turn_goal_active(goal_id)
  else:
    mark_idle_goal_active(goal_id)

  if objective changed:
    inject_active_turn_steering(objective_updated_steering_item)

  continue_if_idle()
```

`continue_if_idle` 才是 `/goal` 能“继续干活”的关键：

```text
continue_if_idle()
  if goal tools not visible:
    clear_active_goal
    return

  read current thread goal from DB

  if goal.status != Active:
    clear_active_goal
    return

  item = continuation_steering_item(goal)

  thread.try_start_turn_if_idle(vec![item])
```

而 `continuation_steering_item` 不是普通用户文本，它会生成 `InternalContextSource("goal")` 的上下文片段：

```text
goal objective / token usage / budget
        |
        v
templates/goals/continuation.md
        |
        v
InternalModelContextFragment(source="goal")
        |
        v
作为下一轮模型输入
```

所以 `/goal` 的完整意义是：

```text
持久目标状态
  + token/time accounting
  + TUI 控制入口
  + app-server 协议
  + 模型工具 get/create/update
  + goal steering context
  + idle 时自动启动下一轮 turn
```

它并没有替代 `run_turn`。它只是把“什么时候继续开始一个新 turn、给模型塞什么目标上下文、什么时候算完成/阻塞/预算耗尽”这几件事放到了 thread goal runtime 里。

## 7. `/goal` 和 `update_plan` 的区别

这两个很容易混。

```text
update_plan
  作用：更新当前 turn 里的 TODO/checklist
  实现：PlanHandler parse JSON -> EventMsg::PlanUpdate
  生命周期：偏 UI/进度展示，不负责自动继续

/goal / create_goal / update_goal
  作用：设置 thread 级长期目标
  实现：TUI slash + app-server protocol + state DB + goal runtime + goal tools
  生命周期：跨 turn，能触发 idle continuation
```

粗暴点说：

```text
plan 是“我现在打算怎么做”
goal 是“这个线程长期要完成什么”
```

## 8. 值得继续研究的点

如果继续挖 Codex，优先看这些：

1. `ext/goal/src/accounting.rs`
   - token/time 怎样被归因到 goal。
   - `budget_limited` 什么时候触发。

2. `ext/goal/templates/goals/*.md`
   - goal continuation 给模型看的原始提示词。
   - 这里直接影响“继续干活”的语气和边界。

3. `core/src/session/turn.rs` 的 compaction 分支
   - 长任务不是只靠 goal，还靠 mid-turn auto compact 保持上下文可用。

4. `core/src/tools/registry.rs` 和 hook runtime
   - handler 前后的 pre/post hook 是权限、审计、改写输入的重要切点。

5. `core/src/tools/code_mode/*`
   - code mode 会从 JS/runtime 发嵌套工具调用，和普通 model function call 不完全一样。

6. `tui/src/chatwidget/input_queue.rs`
   - pending input、slash command、goal continuation 怎样避免互相踩。

7. `core/tests/suite` 和 `ext/goal/tests`
   - 真正验证 agent loop 的方式不是 mock handler，而是假模型响应 + 真实 runtime。

## 9. 最短源码阅读路线

按这个顺序读，最不容易迷路：

```text
1. 工具闭环
   core/src/session/turn.rs
   core/src/stream_events_utils.rs
   core/src/tools/router.rs
   core/src/tools/registry.rs

2. handler 实例
   core/src/tools/handlers/shell/shell_command.rs
   core/src/tools/handlers/unified_exec/exec_command.rs
   core/src/tools/handlers/plan.rs

3. /goal 入口
   tui/src/slash_command.rs
   tui/src/chatwidget/tests/slash_commands.rs
   tui/src/app/thread_goal_actions.rs
   tui/src/app_server_session.rs

4. goal 后端
   app-server-protocol/src/protocol/common.rs
   app-server/src/request_processors/thread_goal_processor.rs
   state/src/model/thread_goal.rs
   state/src/runtime/goals.rs

5. goal 自动继续
   ext/goal/src/spec.rs
   ext/goal/src/tool.rs
   ext/goal/src/runtime.rs
   ext/goal/src/steering.rs
```

记住这一句就够：

```text
handler 负责“怎么执行一个工具”。
run_turn loop 负责“工具结果怎样回到模型”。
/goal 负责“长期目标怎样跨 turn 保持活着，并在空闲时重新拉起 loop”。
```
