# opencode 工具调用链路研究

日期：2026-06-24  
分析版本：`v1.15.1`（源码静态分析）+ `v1.17.9`（源码 diff + strace 实测验证）

## 源码对照

| 版本 | 来源 | 路径 | 分析方式 |
|------|------|------|----------|
| `v1.15.1` | `anomalyco/opencode` 本地克隆 | `<local-opencode-v1.15.1>/packages/opencode` | 全量静态分析（所有工具 schema + 执行路径） |
| `v1.17.9` | `anomalyco/opencode` dev 分支最新 | `<external-workspace>/opencode/packages/opencode` | diff 分析（17 个文件差异）+ strace 实测 |
| 旧 Go 版 | 早期实现，已废弃 | `<local-old-opencode>` | 历史对照 |

> **阅读指引**：本文按逻辑分层组织。先读"结论摘要"和"版本差异总览"获取全局视角，再根据需要深入各版本的具体章节（`[v1.15.1]` / `[v1.17.9]` 标记）。

## 结论摘要

本机当前安装的 `opencode` 是 `~/.opencode/bin/opencode`，版本为 `1.17.9`，是 Bun 打包后的 ELF 可执行文件。

核心结论：

- 新版 opencode 中，shell 本身并不把 JSON 字符串"变成工具调用"。
- 模型侧的 tool/function call 先由 provider/AI SDK 转成统一事件，例如 `tool-input-start`、`tool-call`、`tool-result`。
- opencode 的 `SessionProcessor` 接收这些事件，把工具调用保存为 `MessageV2.ToolPart`。
- 工具执行入口是 AI SDK `streamText({ tools })` 中每个 tool 的 `execute` 回调。
- shell 工具只接收已解析/校验后的参数对象，其中关键字段是 `command`、`timeout`、`workdir`、`description`。
- 真正执行命令时，shell tool 调用 `ChildProcess.make(command, [], { shell, cwd, env, stdin: "ignore", detached: true })`，也就是把 `command` 交给系统 shell 执行。

## 版本差异总览

| 维度 | v1.15.1 | v1.17.9 | 变化程度 |
|------|---------|---------|----------|
| **校验错误** | 泛型 `new Error(...)` | `InvalidArgumentsError` (`Schema.TaggedErrorClass`) | 结构改进 |
| **消息类型** | `MessageV2.WithParts` | `SessionV1.WithParts` | 类型迁移 |
| **权限类型** | `Permission.Request` | `PermissionV1.Request` | 类型迁移 |
| **Provider ID** | 普通 string | `ProviderV2.ID` (branded) | 类型安全 |
| **文件系统** | `AppFileSystem.Service` | `FSUtil.Service` | 服务迁移 |
| **事件总线** | `Bus.Service` | `EventV2Bridge.Service` | 服务迁移 |
| **后台子 Agent** | 独立 `TaskStatusTool` | 合成消息注入，无独立 tool | 重大重写 |
| **Plugin ask 桥接** | 无 | `EffectBridge.make().promise()` | 新增 |
| **截断配置** | 硬编码 `MAX_LINES=2000` | `tool_output.max_lines` config | 可配置化 |
| **注册工具数** | 18 个（含 3 个实验性） | 15 个（下架 repo_clone/repo_overview/task_status） | 精简 |
| **Layer 依赖** | 17 个显式服务依赖 | 13 个（精简 Git/Reference/SessionStatus/Bus/AppFileSystem） | 精简 |
| **task.ts** | 原始实现 | 380 行重写（后台生命周期 + 中途晋升） | 重大重写 |
| **Shell 缓冲** | 内存字符串拼接 | Ring-buffer + 文件 sink fallback | 健壮性 |

## 本机进程与 PID 观察

我做了宿主机进程表检查。检查时没有发现正在运行的 opencode 主进程或 opencode 创建的 shell/pty 子进程；只看到一次与 opencode 文件名相关的文本编辑器进程，以及本次检查命令自身。

因此，本次没有可记录的活跃 opencode 工具调用 PID。

需要注意：普通沙箱内 `ps` 只能看到 Codex 沙箱 PID namespace，不能代表宿主机全局进程表。要抓运行中的 opencode 工具调用 PID，应在宿主机侧观察：

```bash
ps -eo pid,ppid,pgid,sid,stat,comm,args | rg -i 'opencode|bash|zsh|sh|pwsh|node-pty|bun-pty'
```

新版 opencode 有两类“shell 相关进程”：

1. shell tool 临时执行命令：由 `src/tool/shell.ts` 通过 `ChildProcessSpawner.spawn(...)` 创建。
2. 交互式 PTY 终端：由 `src/pty/index.ts` 创建，`Pty.Info.pid` 明确保存底层 PTY 进程 PID。

如果要抓包/追踪一次 shell tool，最佳目标不是“JSON 转工具调用”的 shell，而是：

- opencode 主进程 PID
- shell tool 的子进程 PID 或进程组
- 需要时跟踪其 `execve`、`write`、`read`：

```bash
strace -f -e trace=process,execve,read,write -p <opencode-pid>
```

## 新版 TypeScript/Bun 链路 [v1.15.1]

> 以下源码分析基于 `<local-opencode-v1.15.1>/packages/opencode` (v1.15.1)。核心架构（Tool.define、Effect Schema、wrap 链路）在 v1.17.9 中延续，具体差异见后文"v1.17.9 版本变化对比"章节。

主要源码位置：

- `src/session/prompt.ts`：会话主循环，解析历史消息、选择模型、解析工具集合。
- `src/session/llm.ts`：调用 AI SDK `streamText`，传入 `tools`。
- `src/session/processor.ts`：消费 LLM 流事件，维护 assistant 消息与工具 part。
- `src/tool/registry.ts`：注册内置工具、插件工具，并向模型暴露描述和参数 schema。
- `src/tool/tool.ts`：统一包装工具执行，执行前用 Effect Schema 校验参数。
- `src/tool/shell.ts`：shell tool 的权限扫描、执行、输出截断。
- `src/tool/shell/prompt.ts`：shell tool 的模型可见说明和参数 schema。
- `src/shell/shell.ts`：选择可接受 shell，并为登录 shell 构造参数。
- `src/pty/index.ts`：交互式 PTY 进程管理，记录 `pid`。

### 1. 工具注册

`ToolRegistry` 初始化内置工具，包括：

- `bash` / shell tool
- `read`
- `glob`
- `grep`
- `edit`
- `write`
- `task`
- `fetch`
- `todo`
- `patch`
- plugin tools

每个工具最终是 `Tool.Def`：

```ts
{
  id,
  description,
  parameters,
  jsonSchema?,
  execute(args, ctx)
}
```

插件工具如果使用 Zod schema，会在 registry 边界转成 JSON Schema 给模型；内置工具主要使用 Effect Schema。

### 2. 模型调用

`src/session/llm.ts` 调用：

```ts
streamText({
  messages,
  tools: sortedTools,
  activeTools,
  toolChoice,
  model,
  abortSignal,
})
```

这一步是模型工具调用的核心边界。模型 provider 返回 tool/function call 后，由 AI SDK/provider 适配层把它统一成 `fullStream` 事件。

从打包二进制字符串可以看到 OpenAI-compatible 路径中的典型中间形态：

```js
tool_calls: [
  {
    id,
    type: "function",
    function: {
      name,
      arguments: JSON.stringify(input)
    }
  }
]
```

也就是说，底层 API 常见形态仍是 JSON 字符串 `arguments`。但进入 opencode 工具执行层时，参数已经由 AI SDK/工具 schema 边界转为对象，再传给 `execute(args, ctx)`。

### 3. 会话处理器如何记录工具调用

`src/session/processor.ts` 处理几个关键事件：

- `tool-input-start`：创建 pending 的 tool part。
- `tool-call`：把工具名和输入写入 part，并把状态改成 running。
- `tool-result`：把输出写回 part，状态改成 completed。
- `tool-error`：把错误写回 part，状态改成 error。

工具输入在新版消息结构里是对象：

```ts
state: {
  status: "running",
  input: value.input,
  time: { start: Date.now() }
}
```

`MessageV2` schema 中 tool part 的 `input` 是 `Record<string, any>`，不是旧 Go 版那种原始 JSON 字符串。

### 4. Tool.define 的校验边界

`src/tool/tool.ts` 中 `Tool.define` 会包装工具：

1. 编译 Effect Schema decoder。
2. 执行时先 `decode(args)`。
3. 校验失败则返回“工具参数无效”的错误。
4. 校验成功后调用具体工具的 `execute(decoded, ctx)`。
5. 输出再走截断逻辑。

所以新版中“JSON 到工具参数”的实质位置是：

```text
provider raw function arguments
  -> AI SDK/tool-call event
  -> Tool.define schema decode
  -> concrete tool execute(args, ctx)
```

## shell tool 具体链路 [v1.15.1]

shell tool 的参数 schema 在 `src/tool/shell/prompt.ts`：

```ts
{
  command: string,
  timeout?: positive int,
  workdir?: string,
  description: string
}
```

执行路径在 `src/tool/shell.ts`：

1. 解析 `workdir`，默认当前项目目录。
2. 用 tree-sitter 解析 shell 命令。
3. 扫描命令中可能访问外部目录或需要权限的模式。
4. 调用 `ctx.ask(...)` 触发权限系统。
5. 组装环境变量，触发插件 `shell.env`。
6. 调用 `run(...)`。
7. `run(...)` 内部使用 `ChildProcessSpawner.spawn(...)`。
8. 输出通过 stream 收集，超过限制时写入 `tool-output` 文件。
9. 超时或用户 abort 时 kill 进程。

关键执行代码等价于：

```ts
ChildProcess.make(command, [], {
  shell,
  cwd,
  env,
  stdin: "ignore",
  detached: process.platform !== "win32",
})
```

这说明新版 shell tool 不是持久 shell 会话，而是每次 shell tool 调用生成一次 child process。文案里仍有“persistent shell session”的残留描述，但实际源码路径是 child process spawn。

### `eval JSON.stringify(command)` 的含义

`src/shell/shell.ts` 中存在：

```ts
eval ${JSON.stringify(command)}
```

它只用于 bash/zsh 登录 shell 包装：

- 先启动登录 shell。
- source 用户 shell rc。
- `cd -- "$1"` 到目标目录。
- 再 `eval` 已 JSON.stringify 转义过的命令字符串。

这不是 JSON 工具调用解析逻辑。它的作用是安全地把一段命令字符串嵌入 shell wrapper，避免普通引号拼接问题。

## 权限系统 [v1.15.1]

新版权限在 `src/permission/index.ts`：

- 默认规则是 ask。
- 匹配 allow 则直接执行。
- 匹配 deny 则抛 `PermissionDeniedError`。
- ask 会发布 `permission.asked` 事件并等待回复。
- 用户选择 always 后会把规则加入 approved ruleset。

shell tool 会发起两类权限：

- `external_directory`：命令访问项目外路径时。
- `bash`：命令本身需要执行权限时。

权限匹配使用 wildcard，逻辑在 `src/permission/evaluate.ts`。

## 旧 Go 实现对照

`<local-old-opencode>` 是旧 Go 实现，与当前安装版 CLI 不匹配，但能展示早期工具调用风格：

- provider 把工具调用归一化成 `message.ToolCall{ID, Name, Input string}`。
- `Input` 是 JSON 字符串。
- agent 根据 `Name` 在线性工具表中找实现。
- 调用 `tool.Run(ctx, ToolCall{Input: toolCall.Input})`。
- 每个工具自己 `json.Unmarshal([]byte(call.Input), &params)`。

旧 Go shell tool 的执行路径更接近“持久 shell”：

- 启动 shell：`exec.Command(shellPath, shellArgs...)`
- 默认 shell args 是 `-l`
- stdin 保持打开
- 每次命令写入一段 wrapper：

```bash
eval '<command>' < /dev/null > <stdout-file> 2> <stderr-file>
EXEC_EXIT_CODE=$?
pwd > <cwd-file>
echo $EXEC_EXIT_CODE > <status-file>
```

旧 Go 版里，工具 JSON 字符串确实是在具体工具内部 `json.Unmarshal` 成参数对象；新版则前移到了 AI SDK/schema 执行边界。

## 一句话回答"shell 如何将 JSON 字符转变为工具调用" [v1.15.1]

准确说：shell 没有做这件事。

在当前新版 opencode 中，JSON 工具参数由模型 provider/AI SDK 和 `Tool.define` schema 边界解析、校验，然后以对象形式传给具体工具。shell tool 只是其中一个工具实现，它拿到 `command` 字段后创建子进程交给系统 shell 执行。

在旧 Go 版中，模型 provider 把 function call arguments 保存在 `ToolCall.Input` 字符串里，再由具体工具，例如 bash tool，调用 `json.Unmarshal` 解析成 `BashParams`。

## 后续抓包建议 [v1.15.1]

若要动态确认一次真实工具调用：

1. 启动 opencode 并保持运行。
2. 记录 opencode 主进程 PID。
3. 触发一次简单 shell tool，例如 `pwd`。
4. 同时运行：

```bash
ps -eo pid,ppid,pgid,sid,stat,comm,args | rg -i 'opencode|bash|sh'
```

5. 对主进程或子进程跟踪：

```bash
strace -f -e trace=process,execve,read,write -p <pid>
```

重点看：

- 是否出现新的 shell 子进程。
- `execve` 的 shell 路径。
- `-c` 或 shell wrapper 参数。
- 子进程 PID/PGID。
- stdout/stderr 如何回流到 opencode。

---

# 补充：Tool.define 内部机制详析 [v1.15.1]

以下基于对 `src/tool/tool.ts`、`src/tool/registry.ts`、`src/tool/truncate.ts`、`src/tool/json-schema.ts`、`src/tool/schema.ts` 的深入解析。

## Tool.define 完整类型签名

```ts
function define<Parameters, Result extends Metadata, R, ID extends string = string>(
  id: ID,
  init: Effect.Effect<Init<Parameters, Result>, never, R>,
): Effect.Effect<Info<Parameters, Result>, never, R | Truncate.Service | Agent.Service> & { id: ID }
```

**泛型含义：**

| 泛型 | 含义 |
|------|------|
| `Parameters` | `Schema.Decoder<unknown>` — 工具的入参 Effect Schema，解码后的类型为 `Schema.Schema.Type<Parameters>` |
| `Result extends Metadata` | 工具元数据类型（`Record<string, any>` 的子类型），决定 `ExecuteResult.metadata` 的类型 |
| `R` | `init` Effect 所需的服务依赖（如 `FileSystem`、`Config`） |
| `ID extends string` | 工具 ID 的字面量类型，允许在类型层面区分工具 |

**返回值**是 `Effect.Effect<Info> & { id: ID }` — 一个 Effect **交叉**了一个同步属性 `id`。这意味着可以在不运行 Effect 的情况下读取工具 ID：

```ts
const MyTool = Tool.define("my_tool", Effect.succeed({ ... }))
MyTool.id  // "my_tool" — 在定义时即可访问（同步）
```

## Info vs Def 的延迟加载模式

### Info<Parameters, M> — 延迟工具描述符

```ts
interface Info<Parameters, M> {
  id: string
  init: () => Effect.Effect<DefWithoutID<Parameters, M>>
}
```

`Info` 存储 `id` 和一个零参数函数 `init()`。调用 `init()` 才会执行 `wrap()` 产生的闭包，生成完整的 `DefWithoutID`。

### Def<Parameters, M> — 完整工具定义

```ts
interface Def<Parameters, M> {
  id: string
  description: string            // 模型可见的自然语言描述
  parameters: Parameters         // Effect Schema decoder — 入参校验
  jsonSchema?: JSONSchema7        // 可选的预计算 JSON Schema（绕过自动转换）
  execute(args: Schema.Schema.Type<Parameters>, ctx: Context): Effect.Effect<ExecuteResult<M>>
  formatValidationError?(error: unknown): string  // 自定义校验错误格式化
}
```

### ExecuteResult<M> 返回结构

```ts
interface ExecuteResult<M> {
  title: string       // 工具结果的可展示标题
  metadata: M         // 任意元数据
  output: string      // 返回给 LLM 的文本输出
  attachments?: Omit<MessageV2.FilePart, "id" | "sessionID" | "messageID">[]
}
```

### Context<M> 工具执行上下文

| 字段 | 类型 | 用途 |
|------|------|------|
| `sessionID` | `SessionID` | 当前会话 ID（branded string） |
| `messageID` | `MessageID` | 当前消息 ID |
| `agent` | `string` | 调用本工具的 Agent 名称 |
| `abort` | `AbortSignal` | 取消信号 |
| `callID?` | `string` | 工具调用关联 ID（来自 AI SDK） |
| `extra?` | `{ [key: string]: unknown }` | 扩展数据 |
| `messages` | `MessageV2.WithParts[]` | 完整对话历史（含 parts） |
| `metadata()` | `(input) => Effect<void>` | 副作用：执行中更新工具 part 元数据 |
| `ask()` | `(input) => Effect<void>` | 副作用：请求用户权限 |

## wrap() — 核心执行包装链路

`wrap()` (tool.ts:79–130) 是将原始工具定义包装为具备校验、截断、追踪的完整执行管道的核心函数。

**执行流程：**

```
1. 物化 init
   └─ typeof init === "function" → yield* init()
   └─ 否则 → 结构克隆 { ...init }

2. 编译 Schema 校验闭包（只执行一次！）
   └─ const decode = Schema.decodeUnknownEffect(toolInfo.parameters)

3. 替换原始 execute → 包装后的 execute
   └─ 每次工具调用：
       ├─ decode(args)               // 校验入参
       │   └─ 失败 → formatValidationError 或默认错误信息
       ├─ execute(decoded, ctx)      // 调用原始工具逻辑
       ├─ truncate.output(result.output, {}, agent)  // 输出截断
       │   └─ result.metadata.truncated 已设置则跳过
       ├─ Effect.orDie               // 将可恢复错误转为 defect
       └─ Effect.withSpan("Tool.execute", { attributes })
```

**关键设计点：**

1. **单次编译**：`Schema.decodeUnknownEffect` 正常会为每次调用分配新闭包，这里通过将 `const decode` 提升到 `wrap()` 闭包顶部，使其在 `Info.init()` 时只编译一次
2. **跳过截断**：如果工具自身已设置 `result.metadata.truncated !== undefined`（任何值，包括 `false`），则自动截断被跳过
3. **Effect.orDie**：将所有校验错误转为不可恢复的 defect，调用方需通过 defect handler 捕获
4. **Effect.withSpan**：包装在 OpenTelemetry 风格的 span 中，属性包括 `tool.name`、`session.id`、`message.id`、`tool.call_id`

## Tool.init() — 桥接 Info → Def

```ts
function init<P, M>(info: Info<P, M>): Effect.Effect<Def<P, M>>
```

调用 `info.init()`（即 `wrap()` 闭包），物化 `DefWithoutID`，然后将 `id` 重新附上，生成完整 `Def`。

定义关系链：

```
define("tool_id", effect)
  → 产生 Info { id, init: wrap(...) }
     → Tool.init(info) 调用 info.init()
        → wrap() 物化 DefWithoutID
        → init() 附加 id
        → 产生完整的 Def
```

## 输出截断机制 (truncate.ts)

**截断参数：**

| 参数 | 默认值 | 配置覆盖 |
|------|--------|----------|
| `maxLines` | 2000 | `tool_output.max_lines` |
| `maxBytes` | 51200 (50KB) | `tool_output.max_bytes` |
| `direction` | `"head"` (保留头 N 行) | 每次调用可指定 |

**截断行为：**

```ts
// Result 类型是 discriminated union
type Result =
  | { content: string; truncated: false }
  | { content: string; truncated: true; outputPath: string }
```

- 行数和字节数同时监控，**先触及上限者胜出**
- 超出上限时完整输出写入 `{data_dir}/tool-output/tool_{ascending_id}`
- 截断预览 + 截断提示 + Agent 相关建议（若 Agent 有 Task tool 则建议委托；否则建议 Grep/Read with offset）
- `direction: "tail"` 时保留末尾 N 行（如 shell 输出）
- 每小时清理一次超过 7 天的输出文件

## Schema → JSON Schema 转换 (json-schema.ts)

```ts
function fromSchema(schema: Schema.Top): JSONSchema7
```

**管道：**

1. 查 `WeakMap<Schema.Top, JSONSchema7>` 缓存（按 Schema 对象引用去重）
2. `Schema.toJsonSchemaDocument(schema, { additionalProperties: true })` — 使用 Effect 内置转换，允许额外属性
3. 添加 `$schema: "draft/2020-12"` + `$defs`
4. `normalize()` — 标准化处理：
   - 去除 `additionalProperties: true`
   - 从可选属性中剥离 `null` 类型
   - 折叠非有限数的 number union
   - 展平 `allOf` / 单元素 `anyOf`
   - 为无上限 integer 添加 `minimum/maximum: Number.MIN/MAX_SAFE_INTEGER`
5. `inlineLocalReferences()` — 内联所有 `$ref: "#/$defs/..."` 引用（含循环检测）
6. `dropDefinitionsIfResolved()` — 若所有 ref 已内联则删除 `$defs`

**`fromTool()` 快捷方法：**

```ts
function fromTool(tool: Tool.Def): JSONSchema7 {
  return tool.jsonSchema ?? fromSchema(tool.parameters as Schema.Top)
}
```

若工具提供了预计算 `jsonSchema` 则直接使用，否则从 `parameters` Effect Schema 自动推导。

---

# 补充：全部内置工具参数 Schema 与执行路径 [v1.15.1]

## 工具注册全景

| 工具 ID | 源文件 | 用途 |
|---------|--------|------|
| `bash` | `shell.ts` | 执行 shell 命令 |
| `read` | `read.ts` | 读取文件或目录 |
| `glob` | `glob.ts` | 文件名 glob 匹配 |
| `grep` | `grep.ts` | 正则内容搜索 |
| `edit` | `edit.ts` | 精确字符串替换编辑 |
| `write` | `write.ts` | 写/覆写文件 |
| `task` | `task.ts` | 启动子 Agent |
| `task_status` | `task_status.ts` | 查询后台子 Agent 状态 |
| `todowrite` | `todo.ts` | 维护结构化任务列表 |
| `webfetch` | `webfetch.ts` | HTTP GET 取回网页内容 |
| `websearch` | `websearch.ts` | Exa/Parallel MCP 搜索 |
| `skill` | `skill.ts` | 加载专项技能 |
| `question` | `question.ts` | 向用户提问 |
| `apply_patch` | `apply_patch.ts` | 批量文件增删改 |
| `plan_exit` | `plan.ts` | 从 Plan Agent 切换到 Build Agent |
| `lsp` | `lsp.ts` | LSP 操作 (goto def, references, hover 等) |
| `repo_clone` | `repo_clone.ts` | 克隆外部仓库到缓存 |
| `repo_overview` | `repo_overview.ts` | 浏览仓库结构 |
| `invalid` | `invalid.ts` | 哨兵工具，参数错误时的兜底 |

## 各工具详细 Schema 与执行路径

### bash

**参数 Schema：**
```ts
Schema.Struct({
  command:     Schema.String,
  timeout:     Schema.optional(PositiveInt),
  workdir:     Schema.optional(Schema.String),
  description: Schema.String,
})
```

**描述 (model-facing)**：动态渲染，根据 shell 类型（bash/powershell/cmd）生成不同的命令提示和 Git 操作指南。

**执行路径：**
1. 解析 `workdir`，默认项目目录
2. **权限扫描**：用 `web-tree-sitter` 解析 shell AST，提取 `rm`/`cp`/`mv`/`mkdir` 等操作涉及的文件路径
3. `ctx.ask()` 请求 `external_directory` 权限（若有外部路径）和 `bash` 权限
4. 通过 `ChildProcessSpawner.spawn()` 创建子进程执行
5. 流式收集 stdout/stderr，超出 2×maxBytes 后溢出写入临时文件
6. 支持 abort 信号和超时 kill（force kill after 3 seconds）
7. 输出用 `tail()` 保留末尾 N 行/字节

### read

**参数 Schema：**
```ts
Schema.Struct({
  filePath: Schema.String,                // 绝对路径
  offset:   Schema.optional(NonNegativeInt),  // 起始行号(1-indexed)，默认 1
  limit:    Schema.optional(NonNegativeInt),  // 最大行数，默认 2000
})
```

**执行路径：**
1. 规范化 `filePath` 为绝对路径
2. `assertExternalDirectoryEffect()` — 检查在项目目录内
3. `ctx.ask({ permission: "read" })`
4. 路径不存在时调用 `miss()` 建议相似文件名
5. **目录**：读取 entries，分页返回
6. **文件**：
   - 读取前 4096 字节嗅探 MIME 类型
   - 图片/PDF → 返回 base64 附件
   - 二进制 → 抛出错误
   - 文本 → 流式读取，按 offset/limit 截断，单行最多 2000 字符，总计 50KB
   - 接触 LSP (`lsp.touchFile()`)

### edit

**参数 Schema：**
```ts
Schema.Struct({
  filePath:    Schema.String,               // 绝对路径
  oldString:   Schema.String,               // 要替换的文本
  newString:   Schema.String,               // 新文本（必须不同于 oldString）
  replaceAll:  Schema.optional(Schema.Boolean), // 替换所有匹配项
})
```

**执行路径：**
1. 校验 `filePath` 存在且 `oldString !== newString`
2. 用 **每文件信号量锁** 防止并发编辑同一文件
3. `oldString === ""` → 视为文件创建
4. 尝试 **9 级模糊匹配器**：
   - SimpleReplacer (精确匹配)
   - LineTrimmedReplacer (逐行 trim)
   - BlockAnchorReplacer (首尾行锚点 + Levenshtein)
   - WhitespaceNormalizedReplacer (折叠空白)
   - IndentationFlexibleReplacer (缩进不敏感)
   - EscapeNormalizedReplacer (反转义 `\n`、`\t` 等)
   - TrimmedBoundaryReplacer (边界 trim)
   - ContextAwareReplacer (50% 行匹配的上下文锚点)
   - MultiOccurrenceReplacer (列出所有精确匹配)
5. 生成 unified diff → `ctx.ask({ permission: "edit" })`
6. 写入文件 → 可选格式化 → 发布 `File.Event.Edited` 和 `FileWatcher.Event.Updated`
7. 接触 LSP，报告 diagnostics

### write

**参数 Schema：**
```ts
Schema.Struct({
  content:  Schema.String,
  filePath: Schema.String,  // 必须是绝对路径
})
```

**执行路径：**
1. 规范化路径，`assertExternalDirectoryEffect()`
2. 读取已有文件（如存在），提取 BOM
3. 生成 diff → `ctx.ask({ permission: "edit" })`
4. `fs.writeWithDirs()` → 可选格式化
5. 发布事件 → 接触 LSP → 收集 diagnostics（含最多 5 个其他项目文件）

### glob

**参数 Schema：**
```ts
Schema.Struct({
  pattern: Schema.String,
  path:    Schema.optional(Schema.String),  // 搜索目录，默认项目根
})
```

**执行路径：**
1. `ctx.ask({ permission: "glob" })`
2. 调用 `rg.files()` (ripgrep) 执行 glob 匹配
3. 对每个匹配获取 `mtime` → 按修改时间降序排列 → 截断至 100 个结果

### grep

**参数 Schema：**
```ts
Schema.Struct({
  pattern: Schema.String,                  // 正则表达式
  path:    Schema.optional(Schema.String),  // 搜索目录
  include: Schema.optional(Schema.String),  // 文件过滤 (如 "*.js")
})
```

**执行路径：**
1. `ctx.ask({ permission: "grep" })`
2. 调用 `rg.search()` 执行正则搜索
3. 按 mtime 降序排列 → 截断至 100 个匹配 → 按文件路径分组输出

### task

**参数 Schema：**
```ts
Schema.Struct({
  description:   Schema.String,    // 3-5 词简述
  prompt:        Schema.String,    // 任务描述
  subagent_type: Schema.String,    // 子 Agent 类型
  task_id:       Schema.optional(Schema.String),  // 恢复已有任务
  command:       Schema.optional(Schema.String),  // 触发命令
  background:    Schema.optional(Schema.Boolean), // 后台运行
})
```

**执行路径：**
1. `ctx.ask({ permission: "task" })` 对 subagent_type
2. 查找 Agent → 创建或恢复 Session → 派生子 Agent 权限
3. **后台模式**：通过 `BackgroundJob.Service.start()` 启动 → 完成后注入结果消息 + Toast 通知
4. **前台模式**：使用 `Effect.acquireUseRelease` 连接 abort 信号 → 同步执行 `ops.prompt()`

**特殊处理**：当 `experimentalBackgroundSubagents` 禁用时，JSON Schema 中移除 `background` 字段，防止模型尝试使用。

### todowrite

**参数 Schema：**
```ts
Schema.Struct({
  todos: Schema.mutable(Schema.Array(
    Schema.Struct({
      content:  Schema.String,   // 任务简述
      status:   Schema.String,   // pending | in_progress | completed | cancelled
      priority: Schema.String,   // high | medium | low
    })
  )),
})
```

**执行路径：**
1. `ctx.ask({ permission: "todowrite" })`
2. 调用 `todo.update({ sessionID, todos })` 持久化
3. 返回格式化 JSON

### webfetch

**参数 Schema：**
```ts
Schema.Struct({
  url:     Schema.String,
  format:  Schema.Literals(["text", "markdown", "html"]).pipe(
    Schema.optional, Schema.withDecodingDefault(Effect.succeed("markdown"))
  ),
  timeout: Schema.optional(Schema.Number),  // 最大 120 秒
})
```

**执行路径：**
1. 校验 URL 以 `http://` 或 `https://` 开头
2. `ctx.ask({ permission: "webfetch" })`
3. GET 请求 (Chrome UA) → 若 403 + `cf-mitigated: challenge` → 重试 (`User-Agent: opencode`)
4. 5MB 响应限制
5. 图片 → base64 附件
6. `markdown` 格式 → `TurndownService` HTML→MD 转换
7. `text` 格式 → `htmlparser2` 剥离 script/style

### websearch

**参数 Schema：**
```ts
Schema.Struct({
  query:                Schema.String,
  numResults:           Schema.optional(Schema.Number),
  livecrawl:            Schema.optional(Schema.Literals(["fallback", "preferred"])),
  type:                 Schema.optional(Schema.Literals(["auto", "fast", "deep"])),
  contextMaxCharacters: Schema.optional(Schema.Number),
})
```

**执行路径：**
1. Provider 选择：`OPENCODE_WEBSEARCH_PROVIDER` 环境变量 > flag > sessionID hash
2. Provider：`"exa"` (mcp.exa.ai) 或 `"parallel"` (search.parallel.ai)
3. 通过 MCP JSON-RPC 2.0 协议调用 → 解析 SSE `data: ` 前缀行
4. 描述中 `{{year}}` 被替换为当前年份

### skill

**参数 Schema：**
```ts
Schema.Struct({
  name: Schema.String,  // 从 available_skills 列表中选择
})
```

**执行路径：**
1. 查找 skill → 不存在则列出所有可用 skill 名称
2. `ctx.ask({ permission: "skill" })` 对 skill 名称
3. 解析 skill 目录 → 用 `rg.files()` 列出最多 10 个文件（排除 SKILL.md）
4. 返回 XML 格式的 skill 内容、目录和文件列表

### question

**参数 Schema：**
```ts
Schema.Struct({
  questions: Schema.Array(Schema.Struct({
    question: Schema.String,     // 完整问题
    header:   Schema.String,     // 极短标签 (≤30 字符)
    options:  Schema.Array(Schema.Struct({
      label:       Schema.String,
      description: Schema.String,
    })),
    multiple: Schema.optional(Schema.Boolean),  // 允许多选
  })),
})
```

**执行路径：**
1. 调用 `question.ask({ sessionID, questions, tool })` — 创建 `Deferred`
2. 发布 `Event.Asked` 到 Bus → 等待用户回复
3. 返回 `"question"="answer"` 格式化输出

### apply_patch

**参数 Schema：**
```ts
Schema.Struct({
  patchText: Schema.String,  // 完整 patch 文本
})
```

**Patch 格式**：`*** Begin Patch` / `*** End Patch` 信封，操作头 `*** Add File` / `*** Delete File` / `*** Update File`，`+`/`-` 行前缀，可选的 `*** Move to:`。

**执行路径：**
1. 解析 patch 为 hunks → 对每个 hunk：
   - **Add**：生成 diff → 写入
   - **Update**：读取现有文件 + BOM → 应用 chunks → 生成 diff → 写入
   - **Delete**：读取 → 生成删除 diff → 删除
2. `ctx.ask({ permission: "edit" })` 带组合 diff
3. 写入/格式化/删除 → 发布事件 → 接触 LSP → 收集 diagnostics

### plan_exit

**参数 Schema：** `Schema.Struct({})` — 无参数

**执行路径：**
1. 通过 `question.ask()` 询问用户是否切换到 Build Agent (Yes/No)
2. No → 抛 `Question.RejectedError`
3. Yes → 查找最后一条用户消息 → 创建合成用户消息 `agent: "build"` → 指示 Build Agent 执行 plan
4. 返回 "Switching to build agent"

### lsp

**参数 Schema：**
```ts
Schema.Struct({
  operation: Schema.Literals([
    "goToDefinition", "findReferences", "hover", "documentSymbol",
    "workspaceSymbol", "goToImplementation", "prepareCallHierarchy",
    "incomingCalls", "outgoingCalls",
  ]),
  filePath:  Schema.String,
  line:      Schema.compose(Schema.Int, Schema.GreaterThanOrEqualTo(1)),
  character: Schema.compose(Schema.Int, Schema.GreaterThanOrEqualTo(1)),
  query:     Schema.optional(Schema.String),
})
```

**执行路径：**
1. `ctx.ask({ permission: "lsp" })`
2. 检查 LSP client 可用性 → `lsp.touchFile()`
3. 1-indexed 位置 → 0-indexed LSP 位置转换：`{line: args.line - 1, character: args.character - 1}`
4. 分派到对应 LSP 方法 → 返回 JSON 结果

### repo_clone

**参数 Schema：**
```ts
Schema.Struct({
  repository: Schema.String,             // git URL 或 owner/repo
  refresh:    Schema.optional(Schema.Boolean),  // 强制刷新缓存
  branch:     Schema.optional(Schema.String),
})
```

**执行路径：**
1. 解析仓库引用 → 校验 branch
2. `RepositoryCache.ensure({ reference, refresh, branch })` 委托缓存层
3. 返回元数据：`{ repository, host, remote, localPath, status, head?, branch? }`

### repo_overview

**参数 Schema：**
```ts
Schema.Struct({
  repository: Schema.optional(Schema.String),
  path:       Schema.optional(Schema.String),
  depth:      Schema.optional(Schema.Number, { default: 3 }),  // 1–6
})
```

**执行路径：**
1. 解析目标（缓存仓库 or 本地路径）→ `assertExternalDirectoryEffect()`
2. 递归读取最多 `depth` 层 (上限 200 entries)
3. 忽略 `.git`、`node_modules`、`__pycache__` 等
4. 检测 16 种依赖文件 (Node/Python/Go/Rust/Ruby/Java/PHP)
5. 读取 `package.json` exports/main/bin → Git branch + HEAD

---

# 补充：Session / LLM / Processor 完整管道 [v1.15.1]

## 主循环 (prompt.ts: runLoop)

```
while (true):
  1. 加载消息 (filterCompacted)
  2. 分解状态: { user, assistant, finished, tasks }
  3. 退出条件: lastAssistant finished (非 "tool-calls") && 无 pending tool calls
  4. 处理 subtask / compaction tasks
  5. 检查 token 溢出 → 创建 compaction task
  6. 构建 assistant message stub
  7. resolveTools() → 从 ToolRegistry + MCP 获取工具
  8. 转换消息 → MessageV2.toModelMessagesEffect()
  9. 构建 system prompt (环境信息 + instructions + skills)
  10. handle.process(streamInput) → "compact" | "stop" | "continue"
  11. 循环或退出
```

## resolveTools() 细节 (prompt.ts:522–700)

1. **内置工具**：`registry.tools({ modelID, providerID, agent })` → `Tool.Def[]`
2. 每个 `Tool.Def` 包装为 AI SDK 的 `tool()` 格式：
   ```ts
   tools[item.id] = tool({
     description: item.description,
     inputSchema: jsonSchema(ProviderTransform.schema(input.model, ToolJsonSchema.fromTool(item))),
     execute(args, options) { /* 包装 item.execute + plugin hooks */ },
   })
   ```
3. **MCP 工具**：`mcp.tools()` → 分别包装 `execute`（含权限检查、plugin hooks、截断）

## LLM 流调用 (llm.ts)

```ts
streamText({
  model, messages,
  tools: resolveTools(tools),     // 按名排序 + 过滤禁用的
  activeTools: Object.keys(tools).filter(x => x !== "invalid"),
  toolChoice,                     // "required" 当 format: "json_schema"，否则 "auto"
  temperature, topP, topK, maxOutputTokens,
  abortSignal,                    // 来自 Effect.acquireRelease(new AbortController())
})
```

**GPT 模型特殊处理**：`experimental_repairToolCall` 会 lowercase 工具名或将失败调用路由到 `"invalid"` 工具。

## Session Processor (processor.ts)

**事件 → ToolPart 状态机：**

```
tool-input-start  → 创建 ToolPart { status: "pending", input: {}, raw: "" }
                    存入 ctx.toolcalls[callID] = { done: Deferred, partID, messageID, sessionID }

tool-call         → ToolPart → { status: "running", input: value.input, time: { start } }
                    末日循环检测: 3 次相同调用 → permission ask

tool-result       → completeToolCall() → { status: "completed", input, output, title, metadata,
                                           time: { start, end }, attachments? }
                    Deferred resolve → cleanup 可继续

tool-error        → failToolCall() → { status: "error", input, error, time: { start, end } }
                    Permission.RejectedError → ctx.blocked = true
```

**MessageV2.ToolPart 完整类型：**

```ts
ToolPart = { id, sessionID, messageID, type: "tool", callID, tool, state, metadata? }

// 状态机 discriminated union:
ToolStatePending   = { status: "pending",   input: {}, raw: string }
ToolStateRunning   = { status: "running",   input: Record<string,any>, title?, metadata?, time: { start } }
ToolStateCompleted = { status: "completed", input, output, title, metadata, time: { start, end, compacted? }, attachments? }
ToolStateError     = { status: "error",     input, error, metadata?, time: { start, end } }
```

## Deferred 同步机制

每个 tool call 在 `ctx.toolcalls[callID]` 中存储 `{ done: Deferred<void>, ... }`。`cleanup()` 函数在 abort 时等待所有 Deferred（250ms 超时），然后强制 abort 剩余 tool call。

**Provider-executed 工具**（如 DWS workflow 模型的 `metadata.providerExecuted: true`）由服务端执行，不重新进入循环。

---

# 补充：Plugin 工具桥接与自定义工具加载 [v1.15.1]

## fromPlugin() — Plugin Tool → Tool.Def 转换 (registry.ts:142–192)

```
Plugin 工具 (ToolDefinition)
  │
  ├─ args → isZodType 检测 (_zod 属性)
  │   ├─ 全部 Zod → z.object(args) → zodJsonSchema() + Schema.declare(safeParse)
  │   └─ 混合/非Zod → legacyJsonSchema() + Schema.Unknown
  │
  ├─ execute → Effect.promise(def.execute) 包装
  │   └─ PluginToolContext { ask, directory, worktree }
  │
  └─ 输出 → truncate.output() 自动截断
```

### Zod → JSON Schema 转换 (registry.ts:419–466)

1. `z.toJSONSchema(schema, { io: "input", metadata: zodMetadataRegistry(schema) })`
2. `zodMetadataRegistry` 递归遍历 Zod schema 树（通过 `_zod.def`），收集 `.meta()` 和 `.description`
3. `normalizeZodJsonSchema` 剥离 boolean 类型的 `exclusiveMaximum`/`exclusiveMinimum`
4. `$defs` → `definitions` 重命名适配 `JSONSchema7`

### Zod → Effect Schema 转换

```ts
const parameters = zodParams
  ? Schema.declare<unknown>((u): u is unknown => zodParams.safeParse(u).success)
  : Schema.Unknown
```

非 Zod 的 plugin args 直接使用 `Schema.Unknown`，无校验。

## 用户自定义工具加载 (registry.ts:194–208)

```ts
// 扫描所有配置目录
const matches = dirs.flatMap((dir) =>
  Glob.scanSync("{tool,tools}/*.{js,ts}", {
    cwd: dir, absolute: true, dot: true, symlink: true
  })
)

// 动态 import
const mod = yield* Effect.promise(() => import(pathToFileURL(match).href))

// 命名规则
for (const [id, def] of Object.entries(mod)) {
  custom.push(fromPlugin(id === "default" ? namespace : `${namespace}_${id}`, def))
}
```

- `default` export → tool ID = 文件名
- Named export → tool ID = `{文件名}_{导出名}`
- 每文件可导出多个工具

## isPluginTool 类型守卫 (registry.ts:400–402)

```ts
function isPluginTool(value: unknown): value is ToolDefinition {
  return typeof value === "object" && value !== null
    && "args" in value && "description" in value && "execute" in value
}
```

Duck-typing: 必须同时具有 `args`、`description` 和 `execute`。

---

# 补充：Tool Filtering 与 Feature Flag 门控 [v1.15.1]

## tools() 方法过滤逻辑 (registry.ts:313–358)

**1. WebSearch 过滤：**

```ts
if (tool.id === WebSearchTool.id) {
  return webSearchEnabled(input.providerID, { exa, parallel })
}
// providerID === "opencode" || flags.exa || flags.parallel
```

**2. Edit/Write vs ApplyPatch 路由（GPT 模型特殊处理）：**

```ts
const usePatch =
  input.modelID.includes("gpt-") && !input.modelID.includes("oss") && !input.modelID.includes("gpt-4")

if (tool.id === ApplyPatchTool.id) return usePatch
if (tool.id === EditTool.id || tool.id === WriteTool.id) return !usePatch
```

- GPT-5+ (非 oss、非 gpt-4) → 使用 `apply_patch` 工具（批量文件操作）
- GPT-4*、GPT-oss* → 保留传统 `edit` + `write` 工具
- 非 GPT 模型 → 保留 `edit` + `write`

**3. builtin 数组 Feature Flag 门控：**

| 工具 | Flag | 环境变量 |
|------|------|----------|
| `question` | `client ∈ [app,cli,desktop]` 或 `enableQuestionTool` | `OPENCODE_ENABLE_QUESTION_TOOL` |
| `task_status` | `experimentalBackgroundSubagents` | `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` |
| `repo_clone`, `repo_overview` | `experimentalScout` | `OPENCODE_EXPERIMENTAL_SCOUT` |
| `lsp` | `experimentalLspTool` | `OPENCODE_EXPERIMENTAL_LSP_TOOL` |
| `plan_exit` | `experimentalPlanMode` && `client === "cli"` | `OPENCODE_EXPERIMENTAL_PLAN_MODE` |

**4. Post-filter 增强：**

- `plugin.trigger("tool.definition", ...)` — 允许插件修改 `description`、`parameters`、`jsonSchema`
- TaskTool → 附加 `describeTask()` 输出的子 Agent 列表
- SkillTool → 附加 `describeSkill()` 输出的可用 skill 列表

---

# 补充：端到端全链路数据流 [v1.15.1]

```
┌─ modelside ──────────────────────────────────────────────────────────┐
│ 模型输出 function_call { name, arguments: JSON string }               │
│   → provider adapter 统一化为 AI SDK tool-call 事件                    │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ prompt.ts: resolveTools() ──────────────────────────────────────────┐
│ registry.tools() → Tool.Def[] → 每个包装为 AI SDK tool()             │
│   { description, inputSchema: jsonSchema, execute(args, options) }    │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ llm.ts: streamText() ───────────────────────────────────────────────┐
│ streamText({ model, messages, tools, activeTools, toolChoice, ... })  │
│   → 返回 fullStream: Stream<Event>                                    │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ processor.ts: handleEvent() ────────────────────────────────────────┐
│ tool-input-start → ToolPart { status: "pending" }                     │
│ tool-call        → ToolPart { status: "running", input: decoded }     │
│                     AI SDK 内部调用 tool.execute(args, options)       │
│ tool-result      → ToolPart { status: "completed", output, metadata } │
│ tool-error       → ToolPart { status: "error", error }                │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ tool.execute() 被 AI SDK 触发 ──────────────────────────────────────┐
│                                                                       │
│ prompt.ts 中的 execute 包装：                                         │
│   1. 构建 Tool.Context { sessionID, abort, callID, metadata(), ask() }│
│   2. plugin.trigger("tool.execute.before", ...)                       │
│   3. item.execute(args, ctx) → Effect<ExecuteResult>                  │
│      ├─ Schema.decodeUnknownEffect(args)   ← Tool.define 包装的校验   │
│      │   └─ 失败 → formatValidationError / 默认错误消息 → Effect.orDie│
│      ├─ 具体工具逻辑 (shell/read/edit/...)                            │
│      │   ├─ ctx.ask() → 权限闸门                                      │
│      │   ├─ ctx.metadata() → 实时更新 tool part 元数据                │
│      │   └─ 返回 { title, metadata, output, attachments? }            │
│      └─ truncate.output(output)            ← 自动截断                 │
│   4. plugin.trigger("tool.execute.after", ...)                        │
│   5. 返回 ExecuteResult 给 AI SDK                                     │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ 结果返回给 AI SDK → 生成 tool-result 事件 ──────────────────────────┐
│   → processor.ts 消费 tool-result                                     │
│   → completeToolCall() 更新 ToolPart 为 completed                     │
│   → Deferred resolve → cleanup 可继续                                │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌─ prompt.ts: runLoop 继续 ────────────────────────────────────────────┐
│   → 检查 finished 状态                                                │
│   → 若 "tool-calls" → 下一轮 LLM 请求（模型处理工具结果）              │
│   → 若 "stop" → 退出循环，返回 final message                          │
│   → 若 "compact" → 创建 compaction task，继续循环                     │
└──────────────────────────────────────────────────────────────────────┘
```

## 关键架构决策总结

1. **Effect Schema 边界前置**：参数校验从旧版"工具内部 `json.Unmarshal`"前移到 `Tool.define` 包装层，利用 Effect Schema 的编译时类型推断和运行时校验
2. **延迟加载模式**：`Tool.define` → `Info` (lazy) → `Tool.init` → `Def` (eager)，支持启动时注册、按需物化
3. **双截断路径**：原生工具通过 `wrap()` 自动截断；Plugin 工具在 `fromPlugin()` 中手动截断
4. **权限闸门统一**：所有 mutating 工具通过 `ctx.ask()` 触发权限系统，读取工具也需 `read` 权限
5. **事件驱动 LSP**：edit/write/apply_patch 写文件后自动接触 LSP 并报告 diagnostics
6. **BOM 保留**：edit/write/apply_patch 均通过 `Bom.readFile/split/join/syncFile` 保留 UTF-8 BOM
7. **信号量串行化**：edit 对同一文件使用信号量锁防止并发修改冲突
8. **末日循环检测**：processor 检测同一工具连续 3 次相同输入 → 触发 permission ask
9. **Plugin 工具隔离**：Plugin 工具使用 `Schema.declare(safeParse)` 桥接 Zod schema，JSON Schema 和 Effect Schema 分别独立转换

> **继续阅读**：上文为 v1.15.1 的完整架构分析。v1.17.9 中的变化见"v1.17.9 版本变化对比"章节；strace 实测验证见"strace 实测端到端全链路抓包"章节。

---

# 补充：v1.17.9 版本变化对比（对比基线 v1.15.1）

> 以下对比基于安装版 `1.17.9`（`<external-workspace>/opencode`，commit `a131811`）与旧源码 `1.15.1`（`<local-opencode-v1.15.1>`）。差异行数按 `diff` 统计。对应 v1.15.1 的各章节细节见上文 `[v1.15.1]` 标记的段落。

## 变化概览

| 文件 | 差异行数 | 变化级别 |
|------|----------|----------|
| `task.ts` | 380 | **重大重写** |
| `registry.ts` | 187 | **重大重写** |
| `read.ts` | 126 | 大幅修改 |
| `grep.ts` | 117 | 大幅修改 |
| `edit.ts` | 102 | 大幅修改 |
| `shell.ts` | 92 | 大幅修改 |
| `glob.ts` | 59 | 中等修改 |
| `tool.ts` | 50 | 结构改进 |
| `skill.ts` | 42 | 中等修改 |
| `apply_patch.ts` | 34 | 小幅修改 |
| `external-directory.ts` | 30 | 重写 |
| `write.ts` | 28 | 小幅修改 |
| `truncate.ts` | 27 | 小幅改进 |

## 1. tool.ts — InvalidArgumentsError：类型化校验错误

**最大变化**：将通用的 `new Error(...)` 替换为 `Schema.TaggedErrorClass`。

```ts
// 新增：类型化的校验错误类 (tool.ts:24-34)
export class InvalidArgumentsError extends Schema.TaggedErrorClass<InvalidArgumentsError>()(
  "ToolInvalidArgumentsError",
  {
    tool: Schema.String,
    detail: Schema.String,
  },
) {
  override get message() {
    return `The ${this.tool} tool was called with invalid arguments: ${this.detail}.\nPlease rewrite the input so it satisfies the expected schema.`
  }
}
```

**wrap() 中的使用变化：**

```ts
// 旧版：泛型 Error，无法上游匹配
Effect.mapError((error) => new Error(`The ${id} tool was called with invalid arguments: ${error}`))

// 新版：类型化错误，上游可用 Effect.catchTag("ToolInvalidArgumentsError", ...)
Effect.mapError((error) =>
  new InvalidArgumentsError({
    tool: id,
    detail: toolInfo.formatValidationError ? toolInfo.formatValidationError(error) : String(error),
  }),
)
```

**影响**：上游消费者现在可以精确匹配工具参数错误，进行自愈或路由，例如 `Effect.catchTag("ToolInvalidArgumentsError", handler)`。

## 2. 类型系统迁移：SessionV1 / PermissionV1

**Context 类型的字段变化：**

| 字段 | 旧类型 (1.15.1) | 新类型 (1.17.9) |
|------|-----------------|-----------------|
| `messages` | `MessageV2.WithParts[]` | `SessionV1.WithParts[]` |
| `ask()` 参数 | `Permission.Request` | `PermissionV1.Request` |
| `attachments` | `MessageV2.FilePart` (本地) | `SessionV1.FilePart` (来自 core) |

**本质**：类型从本地模块（`session/message-v2.ts`、`permission/index.ts`）迁移到共享的 `@opencode-ai/core/v1/` 命名空间。`MessageV2.WithParts` 本身已重导出 `SessionV1.WithParts`（`message-v2.ts:18`），所以这是**类型路由的统一化**。

## 3. registry.ts — ProviderV2、精简 Layer、工具移除

### 3.1 ProviderV2 迁移

```ts
// 旧版：普通 string alias
type ProviderID = string
type ModelID = string

// 新版：branded Effect Schema
ProviderV2.ID  // Schema.String.pipe(Schema.brand("ProviderV2.ID"))
ModelV2.ID     // Schema.String.pipe(Schema.brand("ModelV2.ID"))
```

`webSearchEnabled()` 签名也随之变化，使用 `ProviderV2.ID.opencode` 静态值而非字符串比较。

### 3.2 移除的工具

以下工具在 1.17.9 中**不再存在**（无源码文件、无导入、无注册）：

| 移除的工具 | 类型 | 推测原因 |
|-----------|------|----------|
| `task_status` | `TaskStatusTool` | 后台任务状态通过合成消息注入，不需要单独 tool |
| `repo_clone` | `RepoCloneTool` | 实验性功能下线或迁移至 MCP |
| `repo_overview` | `RepoOverviewTool` | 同上 |

当前 `builtin` 数组仅包含 15 个工具（含条件注册的 `question`、`lsp`、`plan`）。

### 3.3 精简的 Layer 依赖

**移除的依赖**：`Git.Service`、`Reference.Service`、`SessionStatus.Service`、`Bus.Service`、`AppFileSystem.Service`

**新增的依赖**：`LayerNode`、`httpClient`、`Database`、`FSUtil`、`EventV2Bridge`、`EffectBridge`

核心迁移路径：
```
AppFileSystem.Service → FSUtil.Service
Bus.Service           → EventV2Bridge.Service
(本地引用)             → InstanceState.context (scope 解析)
```

### 3.4 Plugin 桥接改进

```ts
// 修复 #27451, #27630：args 可能为 undefined
const args = def.args ?? {}  // 新增 fallback

// 新增 EffectBridge 桥接 Effect→Promise 上下文
const bridge = yield* EffectBridge.make()
const pluginCtx: PluginToolContext = {
  ...toolCtx,
  ask: (req) => bridge.promise(toolCtx.ask(req)), // Effect→Promise 桥接
  directory: ctx.directory,
  worktree: ctx.worktree,
}
```

`EffectBridge.make()` 捕获当前 Effect fiber 的上下文（InstanceRef + WorkspaceRef），然后通过 `.promise()` 将 Effect 包装为 Promise，使插件（`Promise`-based）能跨边界保留 Effect 的 workspace/instance 上下文。

### 3.5 describeSkill() 移除

整个 `describeSkill()` 函数被移除。Skill 列表现在通过：
- `skill.txt`：静态模型描述
- `session/system.ts:99`：系统 prompt 中的 `"Use the skill tool to load a skill..."`
- `skill/index.ts:331-353`：运行时生成 `available_skills` XML/markdown 列表

## 4. task.ts — 后台子 Agent 重写（380 行差异）

### 4.1 新的后台执行模型

```ts
// 背景子 Agent 生命周期：
extend(id, run)           // 追加到已有 job → 返回 BACKGROUND_UPDATED
  ↓ (fails to extend)
start({ id, title, onPromote, run })  // 新建 job → 注册通知监听器
  ↓
notify(id)                // fork 一个 fiber 等待 job 完成
  ↓                       // 完成后调用 inject("completed", output)
inject(state, summary)    // 合成 <task> XML 消息注入父 session

// 前台模式下支持"中途晋升"为后台任务：
Effect.raceFirst(
  background.wait(id),           // 等待完成
  background.waitForPromotion(id), // 等待晋升
)
```

### 4.2 EffectBridge 用于取消信号

```ts
// JS AbortSignal → Effect 桥接
const runCancel = yield* EffectBridge.make()
function onAbort() {
  runCancel.fork(cancel)  // 在桥接上下文中执行 Effect 取消
}

// acquireUseRelease 的 release：中断时取消 session + background job
(_, exit) =>
  Effect.gen(function* () {
    if (Exit.hasInterrupts(exit))
      yield* Effect.all([cancel, background.cancel(id)], { discard: true })
  }).pipe(
    Effect.ensuring(Effect.sync(() => ctx.abort.removeEventListener("abort", onAbort))),
  )
```

### 4.3 无独立 TaskStatusTool

1.17.9 中不存在 `task_status.ts`。后台任务结果通过 `inject()` 函数创建**合成文本 part** 注入父 session（XML 格式）：

```xml
<task id="..." state="completed|running|error">
  <summary>Background task completed: ...</summary>
  <task_result>...</task_result>
</task>
```

### 4.4 条件 jsonSchema 门控

```ts
// 后台 subagent 禁用时 → 模型看不到 background 参数
jsonSchema: flags.experimentalBackgroundSubagents
  ? undefined                          // 从完整的 Parameters schema 自动推导（含 background）
  : ToolJsonSchema.fromSchema(BaseParameters) // 仅含 description/prompt/subagent_type/task_id/command
```

## 5. 其他工具主要变化

### 5.1 edit.ts (102 行差异)

- `Bus` → `EventV2Bridge` 事件发布
- `AppFileSystem` → `FSUtil.Service` 文件操作
- 新增 `InstanceState.context` 替代直接注入
- 新增 `Snapshot.FileDiff` 类型用于元数据
- 新增 `isDisproportionateMatch` 安全检查（拒绝替换 span >4× oldString 长度 或 >500 字符）
- 信号量锁使用 `FSUtil.resolve()` 键而非旧路径解析

### 5.2 read.ts (126 行差异)

- 新增 `ReadStop` tagged error 用于流终止
- `AppFileSystem` → `FSUtil.Service`
- 新增 `Effect.fn("ReadTool.*")` 命名追踪
- `Instruction` 模块用于 system-reminder 内容解析
- `sniffAttachmentMime` + `isPdfAttachment` 用于 MIME 路由

### 5.3 grep.ts (117 行差异)

- `AppFileSystem` → `FSUtil.Service`
- `assertExternalDirectoryEffect` 新增 `kind` 参数（`"directory"` vs `"file"`）
- `Ripgrep.Service` 替换旧的 `rg` 工厂

### 5.4 shell.ts (92 行差异)

- `ChildProcess` 从 `effect/unstable/process` 导入（新版 Effect 子进程 API）
- `ShellID.ToolID` 替换硬编码 `"shell"` 字符串
- `BashArity` 前缀权限模式
- 新增 ring-buffer 方法防止内存溢出：保持 `2 × maxBytes` 在内存，超出写入临时文件

### 5.5 glob.ts (59 行差异)

- `AppFileSystem` → `FSUtil.Service`
- `Ripgrep.Service` 替换旧实现
- `InstanceState.context` 目录解析
- `assertExternalDirectoryEffect` 新增 `kind: "directory"` 参数

### 5.6 truncate.ts (27 行差异)

- 新增 `hasTaskTool()` 辅助函数：根据 Agent 权限决定截断提示
- 新增 `limits()` 方法：从 config 读取 `tool_output.max_lines` / `tool_output.max_bytes`
- 新增 `LayerNode` 导出

### 5.7 external-directory.ts (30 行差异 — 重写)

- 新增 `Effect.fn("Tool.assertExternalDirectory")` 命名追踪
- 新增 `Options` 类型：`{ bypass?: boolean, kind?: "file" | "directory" }`
- `kind` 决定 glob scope：directory → 直接用 target，file → `path.dirname(target)`
- 新增 `FSUtil.normalizePath` + `FSUtil.normalizePathPattern` 用于 Windows 兼容

## 6. 跨工具的统一模式变化

| 模式 | 旧版 (1.15.1) | 新版 (1.17.9) |
|------|--------------|---------------|
| 文件系统 | `AppFileSystem.Service` | `FSUtil.Service` |
| 事件总线 | `Bus.Service` | `EventV2Bridge.Service` |
| Scope 上下文 | 直接注入 | `InstanceState.context` |
| BOM 处理 | 部分工具使用 | edit/write/apply_patch 全部通过 `Bom` 模块 |
| Permission 类型 | `Permission.Request` | `PermissionV1.Request` |
| Provider/Model ID | 普通 string | `ProviderV2.ID` / `ModelV2.ID` (branded) |
| Plugin ask 桥接 | 无 | `EffectBridge.make().promise()` |
| 校验错误 | `new Error(...)` | `new InvalidArgumentsError(...)` |
| 截断配置 | 硬编码常量 | `tool_output.max_lines` / `max_bytes` config |
| LSP 报告 | 固定数量 | `MAX_PROJECT_DIAGNOSTICS_FILES = 5` |
| Shell 输出缓冲 | 内存字符串拼接 | Ring-buffer + 文件 sink fallback |

## 7. 架构意图总结

1. **类型统一化**：将分散在本地模块的类型定义迁移到 `@opencode-ai/core/v1/` 共享命名空间，使用 branded Effect Schema 获得编译时类型安全
2. **服务抽象提升**：`FSUtil`、`EventV2Bridge` 替代了旧的 `AppFileSystem`、`Bus`，允许在不同运行时（Node/Bun/Deno）间切换
3. **错误可观测性**：`InvalidArgumentsError` 作为 `TaggedErrorClass` 使工具参数校验失败可被上游精确匹配和处理
4. **后台子 Agent 重设计**：取消独立 `TaskStatusTool`，改用合成消息注入；新增中途晋升机制（foreground → background）
5. **Plugin 桥接加固**：`EffectBridge` 解决 Effect/Promise 边界上下文丢失问题；`def.args ?? {}` 修复空参数静默失败
6. **实验性功能收敛**：下架 `repo_clone`、`repo_overview`、`task_status`，清理 Feature Flag 表

---

# 补充：strace 实测端到端全链路抓包 [v1.17.9 实测]

**日期**：2026-06-24（后续补充）  
**方法**：用 `strace -f -e trace=process,execve,write` 附加到 `opencode run` 进程，触发一次 `bash` tool 调用，记录真实的系统调用链。  
**环境**：`<home>` 目录，`<provider>/<model>` 模型，`pkexec` 临时关闭 `ptrace_scope`（`echo 0 > /proc/sys/kernel/yama/ptrace_scope`）。

## 测试命令

```bash
# 追踪进程创建链
strace -f -e trace=process,execve -s 2000 -o /tmp/opencode-full-trace.log \
  timeout 120 ~/.opencode/bin/opencode run "run pwd" --model <provider>/<model>

# 追踪 write I/O（stdout 捕获）
strace -f -e trace=write -s 200 -o /tmp/opencode-write2.log \
  timeout 60 ~/.opencode/bin/opencode run "run pwd" --model <provider>/<model>
```

## 实测结果：TUI 输出

```
> primary-controller · <model>

$ pwd
<home>

`<home>`
```

## 进程树（strace 实测）

```
strace(383294)                           ← ptrace 追踪者
 └── timeout(383298)                      ← 120s 超时壳
      └── openencode(383299)              ← Bun ELF 主进程
           ├── [~50 个 clone3 线程]        ← Bun worker + Effect fibers
           ├── git(383314)                ← git rev-parse --show-toplevel (exit 128)
           ├── git(383315)                ← git rev-parse --git-common-dir (exit 128)
           ├── git(383333)                ← (二次 git 探测，exit 128)
           ├── git(383334)
           └── bash(383452)              ← ★ vfork+execve: /bin/bash -c pwd
```

**注意**：`git rev-parse` 返回 exit code 128 是因为 `<home>` 不是 git 仓库，opencode 开源版会尝试检测 git 上下文。

## bash 子进程创建（关键 syscall 序列）

```
383299 vfork()                                    ← 父进程阻塞，0-copy 页表共享
383452 execve("/bin/bash",                         ← 子进程替换为 bash
    ["/bin/bash", "-c", "pwd"],                   ← 命令：bash -c pwd
    0x29e84ad3010 /* 74 env vars */)              ← 继承 74 个环境变量
383452 exit_group(0)                              ← 子进程正常退出 (exit 0)
383299 --- SIGCHLD { si_pid=383452,               ← 父进程收到子进程退出信号
                     si_status=0 }                ← 退出码 0
383299 wait4(383452, ..., WNOHANG) = 383452       ← 非阻塞回收子进程
    { ru_utime={tv_sec=0, tv_usec=718},           ← 用户态 CPU 0.7ms
      ru_stime={tv_sec=0, tv_usec=1437} }         ← 内核态 CPU 1.4ms
```

**关键发现**：

1. **使用 `vfork()` 而非 `fork()`**：父进程在子进程 `execve` 前完全阻塞，零拷贝页表。这验证了源码中 `ChildProcess.make(command, [], { shell, cwd, env })` 的 spawn 策略。

2. **直接 `bash -c pwd`**：没有任何 `eval ${JSON.stringify(...)}` 包装。确认 `src/shell/shell.ts` 中的 `eval JSON.stringify` 仅用于登录 shell wrapper（`-l` 模式），普通 tool 调用不会经过。

3. **完整环境变量继承**：74 个 env vars 原样传递给子进程（包括 `PATH`、`HOME`、`OPENCODE_*` 等）。

## I/O 流（write syscall 追踪）

| fd | 内容（原始字节） | 含义 | 方向 |
|----|-----------------|------|------|
| `write(11, "\n> primary-controller · <model>\n")` | 模型信息 | → TUI 显示 |
| `write(11, "\n$ pwd\n")` | 解析后的命令 | → TUI 回显 |
| **`write(11, "<home>\n")`** | **`<home>`** | **shell stdout → TUI** |
| **`write(12, "\`<home>\`\n")`** | **`` `<home>` ``** | **工具结果 → 回写模型** |
| `write(13, "timestamp=... level=INFO run=13fbb24b message=\"creating instance\" directory=<home>")` | 日志 | → 结构化日志 |

**fd 推测**：
- `fd 11`：TUI pty（用户可见输出）
- `fd 12`：模型回写管道（工具结果注入 LLM 上下文）
- `fd 13`：OpenTelemetry 风格结构化日志流

## 权限评估日志（实测记录）

```json
{
  "timestamp": "2026-06-23T17:54:11.857Z",
  "level": "INFO",
  "run": "13fbb24b",
  "message": "evaluated",
  "permission": "bash",
  "pattern": "pwd",
  "action.permission": "bash",
  "action.pattern": "*",
  "action.action": "allow"
}
```

匹配配置规则 `bash: { "*": "allow" }`。权限评估发生在命令执行前（日志时间戳 17:54:11.857，bash 子进程 PID 383452）。

## 端到端数据流（实测验证版）

```
 ┌─ [模型侧] ────────────────────────────────────────────────────┐
 │ <model> 输出 function_call:                            │
 │   { name: "bash", arguments: '{"command":"pwd",...}' }         │
 └──────────────────────┬────────────────────────────────────────┘
                        │ provider adapter (AI SDK)
                        ▼
 ┌─ [processor.ts] ──────────────────────────────────────────────┐
 │ tool-call 事件 → ToolPart { status: "running", input: {...} }  │
 └──────────────────────┬────────────────────────────────────────┘
                        │ AI SDK 调用 tool.execute(args, ctx)
                        ▼
 ┌─ [tool.ts: wrap()] ───────────────────────────────────────────┐
 │ Schema.decodeUnknownEffect({ command: "pwd", ... })            │
 │   └─ ✅ 校验通过                                               │
 └──────────────────────┬────────────────────────────────────────┘
                        │ decoded args → shell.ts execute()
                        ▼
 ┌─ [shell.ts: execute()] ───────────────────────────────────────┐
 │ 1. tree-sitter 解析 "pwd" → AST 中无危险操作模式               │
 │ 2. ctx.ask({ permission: "bash", patterns: ["pwd"] })          │
 │    └─ write(fd13, "evaluated permission=bash pattern=pwd       │
 │                    action=allow")                              │
 │ 3. ChildProcess.make("pwd", [], {                              │
 │      shell: true, cwd: "<home>",                           │
 │      env: { ...process.env, ...plugin_env },                   │
 │      stdin: "ignore",                                          │
 │      detached: true                                            │
 │    })                                                          │
 └──────────────────────┬────────────────────────────────────────┘
                        │ ChildProcessSpawner.spawn()
                        ▼
 ┌─ [系统调用层] ────────────────────────────────────────────────┐
 │ 383299 vfork()                                                 │
 │ 383452 execve("/bin/bash", ["bash", "-c", "pwd"], 74 envs)     │
 │         └─ shell 执行 "pwd" → stdout: "<home>\n"           │
 │ 383452 exit_group(0)                                           │
 │ 383299 wait4(383452, ..., WNOHANG)                             │
 └──────────────────────┬────────────────────────────────────────┘
                        │ stdout 通过管道回流
                        ▼
 ┌─ [shell.ts: run()] ───────────────────────────────────────────┐
 │ stdout 管道读取 → "<home>\n"                               │
 │ truncate.output() → 未触发（< 2000 行, < 50KB）               │
 │ return { output: "<home>", metadata: { truncated: false }} │
 └──────────────────────┬────────────────────────────────────────┘
                        │
                        ▼
 ┌─ [processor.ts] ──────────────────────────────────────────────┐
 │ tool-result → completeToolCall()                               │
 │ ToolPart { status: "completed", output: "<home>" }         │
 │ write(fd12, "`<home>`\n") → 回写模型                      │
 └──────────────────────┬────────────────────────────────────────┘
                        │
                        ▼
 ┌─ [模型侧] ────────────────────────────────────────────────────┐
 │ 模型收到 tool_result: "<home>"                             │
 │ 继续下一轮推理 → 输出最终回复 "`<home>`"                   │
 └────────────────────────────────────────────────────────────────┘
```

## 实测 vs 源码分析验证矩阵

| 预期行为（源码推导） | 实测证据（strace） | 验证状态 |
|---------------------|-------------------|----------|
| `ChildProcess.make` 创建 bash 子进程 | `vfork + execve("/bin/bash", ["bash", "-c", "pwd"])` | ✅ 验证 |
| 非持久 shell（每次调用新进程） | 子进程 `exit_group(0)` → 父进程 `wait4` 回收 | ✅ 验证 |
| `stdin: "ignore"` | 子进程无 stdin fd 操作 | ✅ 验证 |
| `detached: true` (Linux) | `vfork` 而非 `clone(CLONE_NEWPID)` | ✅ 验证 |
| 环境变量继承 | 74 个 env vars 从父进程传递 | ✅ 验证 |
| 权限评估日志 | `write(fd13, "evaluated permission=bash pattern=pwd action=allow")` | ✅ 验证 |
| stdout 管道回流 → TUI | `write(fd11, "<home>")` | ✅ 验证 |
| 工具结果回写模型 | `write(fd12, "`<home>`\n")` | ✅ 验证 |
| `InvalidArgumentsError` 类 | 未触发（参数有效） | — 未覆盖 |
| shell wrapper `eval JSON.stringify` | 未出现（非登录 shell） | — 未覆盖 |
| 超时 kill（`forceKillAfter: "3 seconds"`） | 未触发（秒级完成） | — 未覆盖 |
| 输出截断到文件 (`tool-output/`) | 未触发（输出 < 50KB） | — 未覆盖 |
| 外部目录权限扫描 (`external_directory`) | 未触发（`pwd` 在项目目录内） | — 未覆盖 |
| tree-sitter AST 危险操作检测 | `pwd` 无危险模式，日志中无 denial | ✅ 间接验证 |

## 未覆盖的路径（需构造特定场景）

| 场景 | 触发方式 | 预期 syscall 特征 |
|------|---------|------------------|
| 参数校验失败 | 传递不符合 schema 的参数 | `InvalidArgumentsError` → `Effect.orDie` |
| 目录外命令 | `bash ls /etc` | `external_directory` 权限 ask → `write(fd13, "permission=external_directory")` |
| 超时 | `bash sleep 999` + 短 timeout | SIGKILL after 3s 宽限期 |
| 输出截断 | `bash yes` 或大文件 cat | `write()` 到 `tool-output/tool_*` 文件 |
| 登录 shell | 触发 bash -l 路径 | `eval ${JSON.stringify(command)}` wrapper |
| rm 拒绝 | `bash rm -rf /tmp/test` | 权限日志 `action=deny` + `PermissionDeniedError` |

## 进程资源消耗（本次 pwd 调用）

| 指标 | 值 |
|------|-----|
| 父进程等待时间 | < 2ms (user 0.7ms + sys 1.4ms) |
| 线程数（峰值） | ~50 个 `clone3` 线程 (Bun runtime) |
| 子进程 env vars | 74 |
| 退出码 | 0 |
| git 子进程 | 4 次（均 exit 128，非 git 目录） |

