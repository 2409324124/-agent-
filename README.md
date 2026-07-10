# opencode Harness 源码解析：从 Tool Call 到系统进程的执行链路

- **文档日期**：2026-06-24
- **分析版本**：`opencode v1.17.9`
- **源码根目录**：`<local-opencode>/packages/opencode`

## 关联文档

- [Codex Harness 源码解析：JSON 如何变成 shell 执行](./codex-harness-source-analysis.md)
- [Codex Handler / Loop / Goal 源码解析：agent 如何持续做事](./codex-loop-handler-goal-analysis.md)
- [Codex 后台监听机制解析：长任务为什么能挂着跑](./codex-background-exec-monitoring.md)
- [opencode 运行期开销实测与 TypeScript 高性能开发笔记](docs/opencode-runtime-performance.md)

---

## 1. 架构概述 (Executive Summary)

本文档解析 `opencode` 工具体系（Harness）的核心链路：大语言模型输出的 JSON 格式 Tool Call 如何经过反序列化、Schema 校验、权限拦截，最终转化为受控的操作系统级 Shell 进程执行。

核心结论：

1. **统一包装层**：`Tool.define` 是 TypeScript 实现的工具定义工厂与执行拦截器。
2. **抽象本质**：`bash`、`read`、`edit`、`write` 等工具本质上是 JS/TS 对象；Shell 类工具在最终阶段创建系统子进程。
3. **序列化边界**：JSON 字符串的反序列化发生在 Provider / AI SDK / Workflow Bridge 边界；进入具体工具逻辑前，由 `Tool.define` 强制执行 `Effect Schema` 校验。
4. **Shell 工具定位**：Shell 工具接收已校验的上下文对象，并提取 `command` 字段进行受控执行。
5. **三层安全闭环**：Harness 的生命周期控制由 `AI SDK Tool Envelope`、`opencode Tool Wrapper` 和具体工具执行器三层共同保障。

---

## 2. 核心链路视图

### 2.1 一张图看懂 Harness：源码级全量路径

```text
+---------------------------------------------------------------------+
| 0. 模型输出一次 tool call                                           |
|                                                                     |
|    name: "bash"                                                     |
|    arguments: "{\"command\":\"pwd\",\"description\":\"Show dir\"}"   |
+-------------------------------+-------------------------------------+
                                |
                                | JSON 字符串在 provider/AI SDK 边界解析
                                v
+---------------------------------------------------------------------+
| 1. AI SDK 工具外壳                                                  |
|                                                                     |
| src/session/llm.ts                                                  |
|   streamText({ tools: sortedTools, ... })                           |
|                                                                     |
| src/session/prompt.ts                                               |
|   tool({                                                            |
|     inputSchema: jsonSchema(schema),                                |
|     execute(args, options) { item.execute(args, ctx) }               |
|   })                                                                |
+-------------------------------+-------------------------------------+
                                |
                                | args 已经是对象：{ command, ... }
                                v
+---------------------------------------------------------------------+
| 2. opencode Harness 包装层                                          |
|                                                                     |
| src/tool/tool.ts                                                    |
|   Tool.define("bash", ...)                                          |
|     `- wrap(...)                                                    |
|        |- Schema.decodeUnknownEffect(parameters)                    |
|        |- execute(decoded, ctx)                                     |
|        `- truncate.output(...)                                      |
|                                                                     |
| ctx 里带着：sessionID / messageID / callID / ask / metadata / abort |
+-------------------------------+-------------------------------------+
                                |
                                | decoded 通过 schema 校验
                                v
+---------------------------------------------------------------------+
| 3. bash 工具自己的执行器                                            |
|                                                                     |
| src/tool/shell.ts                                                   |
|   execute(params, ctx)                                              |
|     |- resolve workdir                                              |
|     |- tree-sitter parse command                                    |
|     |- collect permission patterns                                  |
|     |- ctx.ask(...)                                                 |
|     `- run({ shell, command, cwd, env, timeout })                   |
+-------------------------------+-------------------------------------+
                                |
                                | 这里只处理 command，不解析 JSON
                                v
+---------------------------------------------------------------------+
| 4. 系统进程层                                                       |
|                                                                     |
| ChildProcessSpawner.spawn(...)                                      |
|   `- ChildProcess.make(command, [], { shell, cwd, env })            |
|                                                                     |
| 等价理解：/bin/bash -c "pwd"                                        |
+-------------------------------+-------------------------------------+
                                |
                                | stdout/stderr
                                v
+---------------------------------------------------------------------+
| 5. 结果回流                                                         |
|                                                                     |
| shell output                                                        |
|   -> Stream.decodeText(handle.all)                                  |
|   -> preview / truncate / metadata                                  |
|   -> src/session/processor.ts                                       |
|   -> tool-result 写回 Message tool part                             |
+---------------------------------------------------------------------+
```

核心链路：

> JSON 参数在 provider / AI SDK / workflow bridge 边界解析；shell 只接收 `command` 字段执行。harness 的三层是 `streamText({ tools })`、`Tool.define`、具体工具执行器。

---

## 3. 核心源码锚点矩阵

| 层级 | 文件 | 作用 |
|---|---|---|
| LLM 调用入口 | `src/session/llm.ts` | 调 `streamText({ tools })`，把工具集合交给 AI SDK |
| 工具转 AI SDK envelope | `src/session/prompt.ts` | 把 opencode tool 包成 `tool({ inputSchema, execute })` |
| 工具定义包装层 | `src/tool/tool.ts` | `Tool.define`、schema decode、输出截断、span 观测 |
| shell 参数 schema | `src/tool/shell/prompt.ts` | 定义 `command`、`timeout`、`workdir`、`description` |
| shell 执行器 | `src/tool/shell.ts` | 权限扫描、环境注入、spawn、超时、输出截断 |
| 工具事件落盘 | `src/session/processor.ts` | 把 tool call/result 写成 message part |

---

## 4. 执行链路阶段拆解

### 4.1 Schema 暴露与 AI SDK 桥接

opencode 会把工具名和输入 schema 一起暴露给模型。

关键位置：`src/session/prompt.ts`

```ts
const schema = ProviderTransform.schema(input.model, ToolJsonSchema.fromTool(item))
tools[item.id] = tool({
  description: item.description,
  inputSchema: jsonSchema(schema),
  execute(args, options) {
    const ctx = context(args, options)
    const result = yield* item.execute(args, ctx)
    return result
  },
})
```

这里的 `tools[item.id]` 是 AI SDK 认识的工具定义。它有三个关键字段：

- `description`：给模型看的工具说明。
- `inputSchema`：给模型看的 JSON Schema。
- `execute(args, options)`：模型触发工具调用后实际跑的回调。

模型在 provider 的 tool call 协议下按 schema 生成结构化参数。

### 4.2 反序列化边界

在普通 AI SDK tool calling 路径里，JSON 参数解析通常由 provider adapter / AI SDK 处理，opencode 拿到 `execute(args)` 时已经是对象。

但源码里有一个更直观的桥接路径：DWS workflow model。

关键位置：`src/session/llm.ts`

```ts
workflowModel.toolExecutor = async (toolName, argsJson, _requestID) => {
  const t = sortedTools[toolName]
  const result = await t.execute!(JSON.parse(argsJson), {
    toolCallId: _requestID,
    messages: input.messages,
    abortSignal: input.abort,
  })
}
```

该路径明确展示了参数形态转换：

```text
argsJson: string
  -> JSON.parse(argsJson)
  -> t.execute(parsedArgs, options)
```

在“工具执行回调”边界，参数已经从 JSON 字符串变成 JS 对象。

### 4.3 AI SDK 回调进入 opencode Tool Context

AI SDK 调用 `execute(args, options)` 后，opencode 会构造自己的 `Tool.Context`：

```ts
const ctx = context(args, options)
yield* plugin.trigger("tool.execute.before", ..., { args })
const result = yield* item.execute(args, ctx)
yield* plugin.trigger("tool.execute.after", ..., output)
```

这一步把 `args` 交给 opencode 的 tool wrapper，shell 还没有开始执行。

这里还塞进了 harness 需要的上下文：

- `sessionID`
- `messageID`
- `callID`
- `abort`
- `messages`
- `metadata(...)`
- `ask(...)`

工具通过这层上下文更新 UI、申请权限、响应中断、写回 metadata。

---

## 5. `Tool.define` 核心拦截机制

关键位置：`src/tool/tool.ts`

`Tool.define` 的核心结构可以简化成这样：

```ts
export function define(id, init) {
  return {
    id,
    init: wrap(id, resolved, truncate, agents)
  }
}
```

真正重要的是 `wrap(...)`：

```ts
const decode = Schema.decodeUnknownEffect(toolInfo.parameters)
const execute = toolInfo.execute

toolInfo.execute = (args, ctx) => {
  const decoded = yield* decode(args)
  const result = yield* execute(decoded, ctx)
  const truncated = yield* truncate.output(result.output, {}, agent)
  return {
    ...result,
    output: truncated.content,
    metadata: {
      ...result.metadata,
      truncated: truncated.truncated,
      outputPath: truncated.outputPath
    }
  }
}
```

实际源码还会加 span、错误格式化和 `Effect.orDie`，但主逻辑就是四步：

1. 初始化工具时读取 `parameters` schema。
2. 执行工具前用 `Schema.decodeUnknownEffect(...)` 校验 unknown args。
3. 校验成功后调用原始工具实现 `execute(decoded, ctx)`。
4. 工具输出统一走截断逻辑，并把截断信息写入 metadata。

`Tool.define` 的边界：

```text
外部 unknown args
  -> Effect Schema decode
  -> 类型正确的 decoded args
  -> 具体工具 execute
  -> 统一截断/metadata/span
```

它给所有工具补上同一套 harness 能力：

- 参数校验。
- 错误包装。
- 输出截断。
- trace span。
- metadata 更新。
- agent 相关配置读取。

每个工具只需要实现自身业务逻辑，公共执行纪律由 wrapper 统一提供。

---

## 6. Shell 工具的底层执行器

### 6.1 Shell 参数 Schema

关键位置：`src/tool/shell/prompt.ts`

```ts
export function parameterSchema(description: string) {
  return Schema.Struct({
    command: Schema.String,
    timeout: Schema.optional(PositiveInt),
    workdir: Schema.optional(Schema.String),
    description: Schema.String,
  })
}
```

shell tool 真正需要的参数只有这些：

- `command`：要执行的命令字符串。
- `timeout`：可选超时。
- `workdir`：可选工作目录。
- `description`：给 UI/metadata 用的短描述。

模型底层可能吐的是：

```json
{
  "command": "pwd",
  "description": "Show current directory"
}
```

进入 `shell.ts` 时已经是对象。

### 6.2 Shell Tool 执行主线

关键位置：`src/tool/shell.ts`

```ts
execute: (params, ctx) =>
  Effect.gen(function* () {
    const cwd = params.workdir
      ? yield* resolvePath(params.workdir, instanceCtx.directory, shell)
      : instanceCtx.directory

    const timeout = params.timeout ?? defaultTimeout
    const tree = yield* parse(params.command, ps)
    const scan = yield* collect(tree.rootNode, cwd, ps, shell, instanceCtx)
    yield* ask(ctx, scan)

    return yield* run({
      shell,
      command: params.command,
      cwd,
      env: yield* shellEnv(ctx, cwd),
      timeout,
      description: params.description,
    }, ctx)
  })
```

Shell Tool 的执行职责包括：

1. 确定工作目录。
2. 处理超时。
3. 用 tree-sitter 解析命令。
4. 扫描命令可能访问的路径和危险模式。
5. 通过 `ctx.ask(...)` 进入权限系统。
6. 注入环境变量。
7. 调用 `run(...)`。

这段处理 shell 执行准备，不处理 JSON 解析。

### 6.3 受控执行阶段

一次命令执行会被拆成 5 个受控阶段：

```text
params.command
  -> resolvePath(workdir)
  -> parse(command)
  -> collect(AST)
  -> ask(permission)
  -> run(spawn + stream + timeout + truncate)
```

#### 6.3.1 `resolvePath`：工作目录归一

shell prompt 明确要求模型用 `workdir`，不要写 `cd xxx && command`。源码里对应的是：

```ts
const cwd = params.workdir
  ? yield* resolvePath(params.workdir, instanceCtx.directory, shell)
  : instanceCtx.directory
```

这样做的效果是：

- 工作目录由 harness 决定，不藏在命令字符串里。
- 后续权限扫描知道命令实际在哪个目录运行。
- Windows / POSIX / Cygwin 路径可以集中归一化。

#### 6.3.2 `parse`：命令转 AST

源码用 `web-tree-sitter` 加载 bash / PowerShell grammar：

```ts
const tree = yield* parse(params.command, ps)
```

这一步把命令字符串变成语法树。后面的权限系统通过遍历 AST 里的 command 节点识别命令和参数。

直观理解：

```text
"cp a.txt /tmp/x"
  -> command node
     |- command_name: cp
     |- word: a.txt
     `- word: /tmp/x
```

有 AST 后，opencode 才能相对可靠地知道：

- 命令名是什么。
- 哪些 token 是参数。
- 哪些 token 是重定向。
- 哪些参数可能是路径。

#### 6.3.3 `collect`：归纳权限请求

`collect(...)` 做两类收集：

```ts
const scan = {
  dirs: new Set<string>(),
  patterns: new Set<string>(),
  always: new Set<string>(),
}
```

第一类是外部目录权限。比如命令访问了项目外路径：

```text
cat /etc/hosts
cp file /tmp/out
```

源码会尝试从参数里解析路径：

```ts
const resolved = yield* argPath(arg, cwd, ps, shell)
if (!resolved || containsPath(resolved, instance)) continue
scan.dirs.add(dir)
```

第二类是 shell 命令权限。源码会把命令本身加入 pattern：

```ts
scan.patterns.add(source(node))
scan.always.add(BashArity.prefix(tokens).join(" ") + " *")
```

权限弹窗/规则可以具体到：

```text
permission: bash
patterns: ["git status"]
always:   ["git *"]
```

#### 6.3.4 `ask`：执行前权限拦截

`ask(ctx, scan)` 会先处理外部目录，再处理命令本身：

```ts
yield* ctx.ask({ permission: "external_directory", patterns: globs, ... })
yield* ctx.ask({ permission: ShellID.ToolID, patterns, always, ... })
```

这一步还没 spawn 子进程。危险命令会在系统执行前被权限层拦住。

agent harness 和普通脚本执行器的关键差别：

```text
普通执行器：command -> spawn
opencode： command -> AST scan -> permission ask -> spawn
```

#### 6.3.5 `shellEnv`：环境变量插件钩子

执行前还会触发：

```ts
plugin.trigger("shell.env", { cwd, sessionID, callID }, { env: {} })
```

最后环境变量是：

```ts
{
  ...process.env,
  ...extra.env,
}
```

这让插件可以给 shell 注入额外环境，但仍然经过统一入口。模型本身不直接控制这层。

#### 6.3.6 `run`：受控执行与生命周期管理

`run(...)` 同时管理：

- 子进程生命周期。
- stdout/stderr 流式读取。
- metadata 实时预览。
- 超大输出落盘。
- timeout。
- 用户 abort。
- 退出码。

核心竞争逻辑是：

```ts
const exit = yield* Effect.raceAll([
  handle.exitCode,
  abort,
  timeout,
])
```

谁先发生就按谁处理：

- 正常退出：返回 exit code。
- 用户中断：`handle.kill({ forceKillAfter: "3 seconds" })`。
- 超时：同样 kill，并在输出里写 `<shell_metadata>`。

这一段的核心流程：

```text
先理解命令
再归纳权限
再受控执行
最后治理输出
```

### 6.4 系统进程创建

关键位置：`src/tool/shell.ts`

```ts
function cmd(shell: string, command: string, cwd: string, env: NodeJS.ProcessEnv) {
  return ChildProcess.make(command, [], {
    shell,
    cwd,
    env,
    stdin: "ignore",
    detached: process.platform !== "win32",
  })
}
```

然后：

```ts
const handle = yield* spawner.spawn(cmd(input.shell, input.command, input.cwd, input.env))
```

这是命令落到系统层的入口。

在 Linux/macOS 上可以理解为：

```text
ChildProcess.make("pwd", [], { shell: "/bin/bash", cwd: "..." })
  -> shell 执行 command
  -> 产生子进程
  -> stdout/stderr 被 opencode 收集
```

shell tool 和 Linux 常用命令的关系：

```text
opencode shell tool
  是一个 TS 工具包装器
  它最终让系统 shell 去执行 "pwd"、"ls"、"git status" 等字符串
```

### 6.5 输出回收与治理

`run(...)` 会同时处理：

- `handle.all` 输出流。
- stdout/stderr 文本解码。
- 预览 metadata 更新。
- 超大输出写入 `tool-output` 文件。
- 超时或 abort 时 kill 子进程。
- 最终返回 `{ title, metadata, output }`。

简化路径：

```text
child process output
  -> Stream.decodeText(handle.all)
  -> preview / ring buffer
  -> truncate.write(...) when too large
  -> return ExecuteResult
```

opencode 用 truncation harness 控制超大输出，避免把完整日志直接塞回模型上下文。

---

## 7. 结果回流与上下文继承

关键位置：`src/session/processor.ts`

模型流里会出现几类工具事件：

- `tool-input-start`
- `tool-call`
- `tool-result`
- `tool-error`

`tool-input-start` 会创建 pending tool part：

```ts
state: { status: "pending", input: {}, raw: "" }
```

`tool-call` 会把输入对象写进去，并标记 running：

```ts
state: {
  status: "running",
  input: value.input,
  time: { start: Date.now() }
}
```

`tool-result` 会把输出写回：

```ts
yield* completeToolCall(value.toolCallId, output)
```

`tool-error` 会写失败状态：

```ts
yield* failToolCall(value.toolCallId, value.error)
```

完整闭环：

```text
模型请求工具
  -> processor 创建 tool part
  -> AI SDK execute 回调运行工具
  -> 工具返回 output
  -> processor 更新 tool part
  -> 后续消息把 tool result 作为上下文继续喂给模型
```

---

## 8. 源码级执行路径汇总

以一次 shell tool 为例：

```json
{
  "command": "pwd",
  "description": "Show current directory"
}
```

源码路径是：

```text
1. Provider 产生 tool call
   name = "bash"
   arguments = "{\"command\":\"pwd\",\"description\":\"Show current directory\"}"

2. AI SDK / workflow bridge 解析参数
   普通路径：AI SDK/provider adapter 解析
   workflow 路径：src/session/llm.ts 中 JSON.parse(argsJson)

3. AI SDK 调用工具回调
   src/session/prompt.ts
   execute(args, options)

4. opencode 构造 Tool.Context
   context(args, options)
   里面包含 ask、metadata、abort、sessionID、messageID、callID

5. 进入 Tool.define 包装层
   src/tool/tool.ts
   Schema.decodeUnknownEffect(toolInfo.parameters)

6. 校验 shell 参数
   src/tool/shell/prompt.ts
   command 必须是 string
   timeout 必须是正整数
   workdir 可选 string
   description 必须是 string

7. 进入 shell 原始 execute
   src/tool/shell.ts
   resolve cwd
   parse command
   collect permission scan
   ctx.ask(...)

8. 创建子进程
   ChildProcessSpawner.spawn(...)
   ChildProcess.make(command, [], { shell, cwd, env })

9. 收集结果
   handle.all -> decode text -> truncate -> output metadata

10. 写回会话
    src/session/processor.ts
    tool-result -> completeToolCall(...)
```

该路径说明：

- JSON 解析和参数校验是 harness 前半段。
- 权限和执行是具体工具中段。
- 输出截断和事件落盘是 harness 后半段。

---

## 9. Harness 生命周期架构优势

`spawn("bash")` 本身并不构成 Harness 的主要价值；关键在于 opencode 将一次工具调用封装为可控生命周期：

```text
schema contract
  -> provider transform
  -> execute callback
  -> context injection
  -> permission gate
  -> abort signal
  -> metadata streaming
  -> output truncation
  -> event persistence
  -> model continuation
```

具体表现：

- 模型不能随便给参数，参数必须过 schema。
- 工具执行前先申请权限，权限通过后再运行。
- 工具执行中可以持续更新 metadata，让 UI 看到进度。
- 用户 abort 或 timeout 可以杀掉进程。
- 输出过大时保存到文件，只把截断结果回传。
- tool call/result 都会被 processor 写入会话，后续模型能继续基于结果推理。
- 插件工具也能进入同一套执行生命周期。

harness 是一套“让模型安全、可观测、可中断地调用工具”的运行框架。

---

## 10. 底层系统调用验证 (strace 探针)

实测触发一次简单 shell tool，例如 `pwd`，系统层观察到的重点是：

```text
opencode 主进程
  -> 创建 shell 子进程
  -> shell 执行 command
  -> stdout 写回 opencode
  -> 父进程 wait 回收子进程
```

本次用 `opencode 1.17.10` 重新动态复测，命令是让模型使用 bash tool 执行 `pwd`。`strace` 里能看到脱敏后的关键系统调用形态：

```text
execve("<opencode-bin>", ["opencode", "run", "..."], ...) = 0
vfork(...)
execve("<shell>", ["<shell>", "-c", "pwd"], ...) = 0
exit_group(0)
SIGCHLD si_status=0
wait4(<shell-pid>, WEXITSTATUS == 0, WNOHANG, ...) = <shell-pid>
```

这次动态复测确认了 3 件事：

- shell tool 最终确实落成一个 shell 子进程。
- 普通执行路径是 shell `-c "pwd"`；JSON 解析发生在工具执行边界之前。
- 子进程正常退出后由 opencode 父进程回收，输出再回到 tool result。

实测结论和源码一致：

```text
JSON parse / schema decode 在工具执行边界之前；
shell tool 拿到 command 后只负责进程执行。
```

---

## 11. 版本差异与阅读路径

对本文主线来说，`v1.15.1` 到 `v1.17.9` 的主线不变，变化集中在局部强化：

- `Tool.define` + Effect Schema 这条主线仍然存在。
- `streamText({ tools })` 仍然是工具调用边界。
- shell tool 仍然是接收对象参数后 spawn 子进程。
- 新版强化了错误类型、事件桥接、输出截断和后台任务处理。
- 旧 Go 版更像“工具内部自己 `json.Unmarshal`”，新版把参数解析/校验前移到了 AI SDK/schema/harness 边界。

因此，理解新版 opencode 要从这三层入手：

```text
AI SDK tool envelope
  + Tool.define schema wrapper
  + concrete tool executor
```

这三层入手。
