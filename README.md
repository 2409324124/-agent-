# opencode Harness 源码解析：JSON 如何变成工具执行

日期：2026-06-24  
主分析版本：`opencode v1.17.9`  
源码根目录占位：`<local-opencode>/packages/opencode`

这篇文档只解释一条核心链路：

> 模型输出一个 tool call，里面带着 JSON 参数；opencode 如何把它变成一次真实工具执行，尤其是 shell 命令如何最终落到系统进程。

结论先放前面：

- `Tool.define` 不是 Linux 命令，也不是一堆可执行文件。它是 TypeScript 里的工具定义工厂和执行包装层。
- `bash`、`read`、`edit`、`write` 等工具，本质上都是 JS/TS 对象；只有 shell tool 最后会创建系统子进程。
- JSON 字符串到对象的解析发生在 provider / AI SDK / workflow bridge 边界；进入具体工具前，还会经过 `Tool.define` 的 Effect Schema 校验。
- shell tool 不负责“把 JSON 变成工具调用”。它只接收已经解析并校验过的对象，然后取 `command` 字段执行。
- opencode 强大的 harness 来自三层包装：AI SDK tool envelope、opencode `Tool.define` wrapper、具体工具自己的权限和执行逻辑。

---

## 一张图看懂 Harness

```text
+---------------------------------------------------------------------+
| 0. 模型输出的不是 Linux 命令，而是一次 tool call                    |
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

如果只记一句话：

> JSON 不是 shell 解析的；shell 只是最终执行器。真正的 harness 是 `streamText({ tools })` + `Tool.define` + 具体工具执行器这三层。

---

## 核心源码锚点

| 层级 | 文件 | 作用 |
|---|---|---|
| LLM 调用入口 | `src/session/llm.ts` | 调 `streamText({ tools })`，把工具集合交给 AI SDK |
| 工具转 AI SDK envelope | `src/session/prompt.ts` | 把 opencode tool 包成 `tool({ inputSchema, execute })` |
| 工具定义包装层 | `src/tool/tool.ts` | `Tool.define`、schema decode、输出截断、span 观测 |
| shell 参数 schema | `src/tool/shell/prompt.ts` | 定义 `command`、`timeout`、`workdir`、`description` |
| shell 执行器 | `src/tool/shell.ts` | 权限扫描、环境注入、spawn、超时、输出截断 |
| 工具事件落盘 | `src/session/processor.ts` | 把 tool call/result 写成 message part |

---

## JSON 到工具执行的源码路径

### 1. 模型看到的是 JSON Schema

opencode 不只是把工具名告诉模型，还会把每个工具的输入 schema 暴露给模型。

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

所以模型不是随便吐字符串，而是在 provider 支持 tool call 的协议下，按 schema 生成结构化参数。

### 2. JSON 字符串在哪里 parse？

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

这段代码把事情说得很直白：

```text
argsJson: string
  -> JSON.parse(argsJson)
  -> t.execute(parsedArgs, options)
```

也就是说，在“工具执行回调”边界，参数已经从 JSON 字符串变成 JS 对象。

### 3. `prompt.ts` 再把执行交给 opencode tool

AI SDK 调用 `execute(args, options)` 后，opencode 会构造自己的 `Tool.Context`：

```ts
const ctx = context(args, options)
yield* plugin.trigger("tool.execute.before", ..., { args })
const result = yield* item.execute(args, ctx)
yield* plugin.trigger("tool.execute.after", ..., output)
```

这一步很关键。`args` 还没有直接进 shell，而是进入 opencode 的 tool wrapper。

这里还塞进了 harness 需要的上下文：

- `sessionID`
- `messageID`
- `callID`
- `abort`
- `messages`
- `metadata(...)`
- `ask(...)`

这就是为什么工具不是孤立函数。它能更新 UI、申请权限、响应中断、写回 metadata。

---

## `Tool.define` 包装层到底做什么

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

所以 `Tool.define` 是一个边界层：

```text
外部 unknown args
  -> Effect Schema decode
  -> 类型正确的 decoded args
  -> 具体工具 execute
  -> 统一截断/metadata/span
```

它解决的问题不是“执行命令”，而是让所有工具都有同一套 harness 能力：

- 参数校验。
- 错误包装。
- 输出截断。
- trace span。
- metadata 更新。
- agent 相关配置读取。

这也是它强的地方：每个工具只写自己的业务逻辑，公共执行纪律由 wrapper 统一提供。

---

## Shell tool 为什么只是执行器

### 1. shell 参数 schema

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

但进入 `shell.ts` 时已经是对象，不是原始 JSON 字符串。

### 2. shell tool 的执行主线

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

这里说明 shell tool 做的是：

1. 确定工作目录。
2. 处理超时。
3. 用 tree-sitter 解析命令。
4. 扫描命令可能访问的路径和危险模式。
5. 通过 `ctx.ask(...)` 进入权限系统。
6. 注入环境变量。
7. 调用 `run(...)`。

注意，以上都不是 JSON 解析。

### 3. 这段为什么是核心科技

这段代码真正厉害的地方，是它没有把模型给的 `command` 直接扔给 shell，而是先把一次命令执行拆成 5 个受控阶段：

```text
params.command
  -> resolvePath(workdir)
  -> parse(command)
  -> collect(AST)
  -> ask(permission)
  -> run(spawn + stream + timeout + truncate)
```

#### 3.1 `resolvePath`：不鼓励模型自己 `cd`

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

#### 3.2 `parse`：先把命令变成 AST，而不是正则硬猜

源码用 `web-tree-sitter` 加载 bash / PowerShell grammar：

```ts
const tree = yield* parse(params.command, ps)
```

这一步把命令字符串变成语法树。后面的权限系统不是简单查字符串里有没有 `rm`，而是遍历 AST 里的 command 节点。

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

#### 3.3 `collect`：把命令归纳成权限请求

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

这就是为什么权限弹窗/规则不是只有粗糙的“允许 bash”，而可以具体到：

```text
permission: bash
patterns: ["git status"]
always:   ["git *"]
```

#### 3.4 `ask`：执行前卡权限，不是执行后补救

`ask(ctx, scan)` 会先处理外部目录，再处理命令本身：

```ts
yield* ctx.ask({ permission: "external_directory", patterns: globs, ... })
yield* ctx.ask({ permission: ShellID.ToolID, patterns, always, ... })
```

这一步还没 spawn 子进程。也就是说，危险命令会在系统执行前被权限层拦住。

这也是 agent harness 和普通脚本执行器的关键差别：

```text
普通执行器：command -> spawn
opencode： command -> AST scan -> permission ask -> spawn
```

#### 3.5 `shellEnv`：环境变量也走插件钩子

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

#### 3.6 `run`：真正执行，但仍然受控

`run(...)` 不是简单 `await exec(command)`。它同时管理：

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

所以这一段的核心不是“会运行 bash”，而是：

```text
先理解命令
再归纳权限
再受控执行
最后治理输出
```

### 4. 最终如何变成系统进程

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

这才是真正落到系统层的地方。

在 Linux/macOS 上可以理解为：

```text
ChildProcess.make("pwd", [], { shell: "/bin/bash", cwd: "..." })
  -> shell 执行 command
  -> 产生子进程
  -> stdout/stderr 被 opencode 收集
```

所以 shell tool 和 Linux 常用命令的关系是：

```text
opencode shell tool
  不是 pwd/ls/git 这些命令本身
  而是一个 TS 工具包装器
  它最终让系统 shell 去执行 "pwd"、"ls"、"git status" 等字符串
```

### 5. 输出如何回到模型和 UI

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

这就是为什么 opencode 能避免超大输出直接把上下文撑爆：输出不是简单拼接后无脑塞回模型，而是经过 truncation harness。

---

## 工具事件如何写回会话

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

所以完整闭环是：

```text
模型请求工具
  -> processor 创建 tool part
  -> AI SDK execute 回调运行工具
  -> 工具返回 output
  -> processor 更新 tool part
  -> 后续消息把 tool result 作为上下文继续喂给模型
```

---

## 源码级回答：JSON 字符串如何执行

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

这个路径说明：

- JSON 解析和参数校验是 harness 前半段。
- 权限和执行是具体工具中段。
- 输出截断和事件落盘是 harness 后半段。

---

## 为什么说 opencode 的 harness 强

强点不在“它会调用 shell”。很多程序都会 `spawn("bash")`。

真正强的是它把一次工具调用包成了可控生命周期：

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
- 工具执行前可以申请权限，而不是直接运行。
- 工具执行中可以持续更新 metadata，让 UI 看到进度。
- 用户 abort 或 timeout 可以杀掉进程。
- 输出过大时保存到文件，只把截断结果回传。
- tool call/result 都会被 processor 写入会话，后续模型能继续基于结果推理。
- 插件工具也能进入同一套执行生命周期。

这就是 harness：不是一个命令，而是一套“让模型安全、可观测、可中断地调用工具”的运行框架。

---

## 实测验证摘要

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
- 普通执行路径是 shell `-c "pwd"`，不是 shell 去解析 JSON。
- 子进程正常退出后由 opencode 父进程回收，输出再回到 tool result。

实测结论和源码一致：

```text
JSON parse / schema decode 在工具执行边界之前；
shell tool 拿到 command 后只负责进程执行。
```

---

## 版本差异只保留核心结论

对本文主线来说，`v1.15.1` 到 `v1.17.9` 的差异不是“架构换了”，而是局部强化：

- `Tool.define` + Effect Schema 这条主线仍然存在。
- `streamText({ tools })` 仍然是工具调用边界。
- shell tool 仍然是接收对象参数后 spawn 子进程。
- 新版强化了错误类型、事件桥接、输出截断和后台任务处理。
- 旧 Go 版更像“工具内部自己 `json.Unmarshal`”，新版把参数解析/校验前移到了 AI SDK/schema/harness 边界。

因此，理解新版 opencode 不应该从“shell 如何 parse JSON”入手，而应该从：

```text
AI SDK tool envelope
  + Tool.define schema wrapper
  + concrete tool executor
```

这三层入手。
