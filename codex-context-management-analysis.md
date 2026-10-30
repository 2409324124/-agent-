# Codex 上下文管理源码解析：首轮上下文拼装与压缩折叠

日期：2026-10-30
分析版本：`openai/codex` commit `cc2f80d5157ccf7540589de764318315c4443948`（2026-10-30）
实测数据：本机 Codex Desktop 26.1021.31345（core 0.153.4）的真实会话 rollout + codex-cli 0.166.0
源码根目录占位：`<codex-source>`（即仓库内 `codex-rs/` 目录）

这版只抓两件事：

1. 首轮上下文：新会话第一次请求模型时，input 到底是按什么顺序拼出来的。
2. 上下文压缩：什么时候触发、谁在生成摘要、压缩后的历史长什么样。

## 0. 最短结论

首轮上下文是确定性拼装：

```text
base_instructions（人格提示词，developer）
  -> 聚合 developer 消息（app-context / 协作模式 / 权限 / 模型说明…）
  -> 独立 developer 消息（guardian 等隔离策略）
  -> contextual user 消息（<recommended_plugins> / <environment_context>）
  -> 用户第一条消息
```

压缩是有损折叠，但有两条铁律：

1. **用户消息永不进摘要**：`compact.rs` 把所有真实 user 消息原文挑出来保留，只折叠 agent 自己的中间产出（assistant 回复、工具调用与输出）。
2. **压缩后重建同一副骨架**：initial context（environment_context、app-context 等）被剥离后按原结构重新插回，保证前缀稳定、prompt cache 继续命中。

## 1. 首轮上下文组装链路

入口是 `Session::build_initial_context_with_world_state`（`core/src/session/mod.rs:4719`）。它不直接拼字符串，而是收集一堆 `RenderedFragment`，按 slot 分流：

```text
build_initial_context_with_world_state
  |
  |-- developer_instructions（config.toml 的 developer_instructions）
  |-- 插件系统：plugins_manager.recommended_plugin_candidates_for_config()
  |-- extensions.context_contributors()
  |     |-- PromptSlot::DeveloperPolicy / DeveloperCapabilities -> developer_sections
  |     -- PromptSlot::ContextWindow -> context_window_hints
  |-- per-turn contributors（带 thread_id / turn_id / model_context_window）
  |-- TokenBudget feature -> notes/thread_hint MCP 调用
  |
  v
按 fragment 的 role 分流（session/mod.rs:4900 附近）：
  "developer" + 模型说明   -> developer_sections.insert(0, ...)  // 永远在最前
  "developer" + <multi_agent_mode> -> 独立消息
  "developer" 普通          -> developer_sections.push(...)
  "user"                   -> contextual_user_sections.push(...)
  |
  v
build_rendered_message(developer_sections)   // 聚合成一条 developer 消息
items.append(separate_developer_messages)    // 隔离的 developer 消息
items.push(contextual_user_message)          // 聚合的 user 角色上下文
```

所有 fragment 类型都在 `core/src/context/` 目录下，一个文件一种上下文部件（`mod.rs` 里有完整清单）：

| fragment | 文件 | 角色 |
|---|---|---|
| BaseInstructionsFragment | `context/base_instructions.rs` | developer |
| DeveloperInstructions | `context/developer_instructions.rs` | developer |
| ModelInstructions | `context/model_instructions.rs` | developer（insert 到 0 号位） |
| MultiAgentModeInstructions | `context/multi_agent_mode_instructions.rs` | developer（独立消息） |
| RecommendedPluginsInstructions | `context/recommended_plugins_instructions.rs` | user |
| EnvironmentContext | `context/environment_context.rs` | user |
| UserInstructions（config 的 instructions / AGENTS.md） | `context/user_instructions.rs` | user |
| MemoryContextFragment | `context/memory.rs` | user |
| CompactionSummary | `context/compaction_summary.rs` | user（压缩后的续轮） |

注意一个反直觉设计：**大量系统信息以 user 角色下发**（environment_context、推荐插件、AGENTS.md）。这与 Responses API 的角色语义有关，实测 rollout 里也确实如此（见第 6 节）。

`EnvironmentContext::render`（`context/environment_context.rs:59`）渲染出的 XML 结构实测为：

```xml
<environment_context>
  <cwd>D:\tankdazhan</cwd>
  <shell>powershell</shell>
  <current_date>2026-09-10</current_date>
  <timezone>Asia/Shanghai</timezone>
  <filesystem><workspace_roots><root>…</root></workspace_roots>
  <permission_profile type="…">…
```

## 2. 压缩触发链路

判定逻辑集中在 `core/src/session/context_window.rs` 的 `context_window_token_status_with_config`（:47）：

```text
每轮结束/开始前
  |
  v
active_context_tokens = 会话累计 token 用量
  |
  |-- auto_compact 作用域（AutoCompactTokenLimitScope）：
  |     Total          -> 按全量上下文计
  |     BodyAfterPrefix -> 只计首轮前缀之后新增的 token
  |
  |-- 三道闸门：
  |     1. auto_compact_scope_limit + fallback_buffer   （软阈值，可配）
  |     2. full_context_window_limit                    （硬上限）
  |        = context_window * effective_context_window_percent / 100
  |     3. turn_end_compaction_threshold_reached        （回合末提前量）
  |        = active >= full_limit * model_post_turn_compact_threshold_percent / 100
  |
  v
token_limit_reached == true
  -> run_inline_auto_compact_task（本地摘要，compact.rs:117）
  -> run_inline_remote_auto_compact_task（服务端摘要，compact_remote_v2.rs:112）
  -> token_budget 模式 -> run_inline_auto_compact_task（compact_token_budget.rs:34，
     跳过摘要，直接开新窗口）
```

另有一条**异步后台压缩**（`session/async_compaction.rs:40`）：开了 `Feature::AsyncCompaction` 且 provider 支持 `RemoteCompactionSupport::V2` 时，用量超过 `async_compaction_threshold_percent` 就提前在后台跑摘要，等真正触顶时直接领养（adopt）已完成的结果——这就是为了消除"压缩卡住 30 秒"的体验问题。

## 3. 压缩执行：三条路径

| 路径 | 入口 | 摘要来源 | 说明 |
|---|---|---|---|
| 本地摘要 | `compact.rs:154` `run_compact_task` | 自己发一次模型请求 | `run_compact_task_inner_impl`（:263）把当前历史 + 压缩指令发给同一个模型，取回摘要文本 |
| 远端 v2 | `compact_remote_v2.rs:142` | 服务端 | 历史发给服务端压缩接口，返回加密摘要项 |
| token budget | `compact_token_budget.rs:22` | 无 | 跳过摘要，直接 `start_new_context_window`，只保留骨架 |

本地路径的摘要请求构造（`compact.rs:296-330`）有个细节：摘要请求**复用同一个 client session**（sticky routing / websocket 增量状态在重试间存活），base_instructions 优先从历史里找回原文，找不到才回退到 session 当前值。

### 折叠算法

`build_compacted_history_with_limit`（`compact.rs:714`）：

1. `collect_annotated_user_messages`（:594）从旧历史中挑出所有真实 user 消息——`is_summary_message`（:621）用 `SUMMARY_PREFIX`（`prompts/templates/compact/summary_prefix.md`）过滤掉上一轮压缩留下的摘要；
2. 用户消息按 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`（`compact.rs:63`）的预算从新到旧选取，超预算就丢最老的；
3. 摘要文本放在最后；
4. `insert_initial_context_before_last_real_user_or_summary`（:650）把 initial context 重新插回——**优先插在最后一条真实用户消息之前**，没有用户消息就插在摘要之前，保证摘要/compaction 项永远在末尾。

远端 v2 的保留预算更大：`RETAINED_MESSAGE_TOKEN_BUDGET = 64_000`（`compact_remote_v2.rs:87`），由服务端决定折叠内容，客户端拿到的是 `ResponseItem::Compaction { encrypted_content }`——**摘要在本地不可见**（Fernet 加密，`gAAAAA…` 开头），rollout 里只能看到密文。

## 4. 核心源码锚点矩阵

| 关注点 | 文件 | 说明 |
|---|---|---|
| 首轮组装入口 | `core/src/session/mod.rs:4719` | `build_initial_context_with_world_state` |
| 上下文部件清单 | `core/src/context/mod.rs` | 50+ 种 fragment，一个文件一种 |
| 环境上下文渲染 | `core/src/context/environment_context.rs:59` | XML 结构 |
| AGENTS.md 加载 | `core/src/agents_md.rs` | 发现与注入 |
| 触发判定 | `core/src/session/context_window.rs:47` | 三道闸门 |
| 本地压缩 | `core/src/compact.rs` | 摘要请求 + 折叠算法 |
| 远端压缩 v2 | `core/src/compact_remote_v2.rs` | 服务端摘要、64k 保留预算 |
| token budget 压缩 | `core/src/compact_token_budget.rs` | 无摘要开新窗 |
| 异步后台压缩 | `core/src/session/async_compaction.rs:40` | 提前跑、触顶领养 |
| 回合循环调用点 | `core/src/session/turn.rs:629` | `token_limit_reached` 消费处 |

## 5. 实测验证（本机 rollout + 端到端触发）

数据来自 `~/.codex/sessions/` 下真实会话（Codex Desktop 26.1021，core 0.153.4；codex-cli 0.166.0）。

**首轮**：`session_meta` 里存完整 `base_instructions` 文本；其后依次是 developer `<app-context>`（桌面版能力说明）、user `<recommended_plugins>`、user `<environment_context>`、用户首条消息——与第 1 节的代码路径一致。

**缓存**：该会话单轮 `input_tokens=46557` 中 `cached_input_tokens=45824`（98.4%）。固定前缀拼装的效果直接体现在这里。

**压缩**：一次真实 `compacted` 记录（`window_number: 4`，即第 4 次窗口轮换）的 `replacement_history` 共 48 项：42 条 user 消息原文 + 3 条 developer 上下文（app-context / 多 agent / 插件清单，原样重建）+ 2 条 user + 末尾 1 条 `{"type":"compaction", "encrypted_content": "gAAAAA…"}`。payload 还带 `retained_context`、`window_id`/`previous_window_id` 链、`latest_token_usage_record`。对应一次 `ContextCompaction` turn item，耗时约 34 秒。

### 5.1 端到端：真实触发一次压缩（codex-cli 0.166.0）

利用 `model_auto_compact_token_limit` 把阈值压到 12000，正常对话即可触发：

```bash
codex -c model_auto_compact_token_limit=12000 exec --sandbox danger-full-access \
  --skip-git-repo-check '请记住数字 42，只回复一个字：好'
codex -c model_auto_compact_token_limit=12000 exec --sandbox danger-full-access \
  resume --last --skip-git-repo-check '再记住数字 7，只回复一个字：好'
# 输出中出现 "context compacted"
codex -c model_auto_compact_token_limit=12000 exec --sandbox danger-full-access \
  resume --last --skip-git-repo-check '我之前让你记住的两个数字分别是什么？'
# 回答：42，7 —— 压缩后用户消息存活验证通过
```

该会话 rollout 中出现 2 条 `compacted` 记录，结构验证了第 3 节的折叠算法：

- `replacement_history` 第 0 项是 `additional_tools`（developer）——**工具声明也被重建**，不只是文本上下文；
- 中间是重建的 developer 消息（multi_agent_mode / skills 等）+ 保留的 user 消息原文；
- 末尾恒为 `{"type":"compaction", "encrypted_content": "gAAAAA…"}`；
- CLI 走的同样是**远端加密摘要**路径，不是本地明文摘要——说明默认 ChatGPT 登录 + openai provider 下 `RemoteCompactionSupport::V2` 生效。

**CLI vs 桌面版首轮差异（实测）**：同一 Rust core。CLI 首轮没有 `<app-context>`、没有 `<recommended_plugins>`，取而代之的是 developer `<skills_instructions>`、`<multi_agent_role>`、`<multi_agent_mode>`；`<environment_context>` 两边一致。桌面版多出 app-context、插件市场、computer-use 相关 fragment。

### 5.2 端到端：AGENTS.md 注入位置

在测试目录放一个含口令的 `AGENTS.md`，`codex exec` 直接问口令，模型答出——说明首轮已注入。rollout 确认其形态为 **user 角色**消息，紧跟在 environment_context 之后、用户首条消息之前：

```text
# AGENTS.md instructions for C:\...\e2e

<INSTRUCTIONS>
# 项目约定
- 本项目的口令是 banana-split
</INSTRUCTIONS>
```

即源码里的 `UserInstructions` fragment：role=user，包 `<INSTRUCTIONS>` 标签。

### 5.3 端到端：Skill 的发现、注入与触发

在 `~/.codex/skills/test-echo-skill/SKILL.md` 放一个带暗号的测试技能（frontmatter 含 name/description），`codex exec '测试技能'` 模型直接答出暗号 `PINEAPPLE-777`。rollout 揭示 skill 机制是**渐进披露（progressive disclosure）**：

1. **首轮只注入元数据**：`<skills_instructions>`（developer 角色）里只有 skill 名称 + description + skill roots 表：
   ```text
   ### Skill roots
   - `r0` = `C:/Users/Administrator/.codex/skills`
   - `r1` = `.../.codex/skills/.system`
   - `r2` = `.../plugins/cache/openai-bundled`（插件技能）
   ### Available skills
   - test-echo-skill: 测试用技能。当用户说"测试技能"时…
   ```
2. **按需读正文**：模型判断相关后才用 shell 读 `SKILL.md` 全文（rollout 里有对应 Get-Content 调用），正文不进首轮上下文——这正是 skills 对上下文友好的关键设计。
3. 触发判定靠 description 写得是否命中场景；frontmatter 校验见 `skills/src/parser.rs:43`。

**Windows 沙箱注意点**：本机默认 read-only 沙箱下 exec 报 `CreateProcessWithLogonW failed: 1385`（受限令牌登录失败），需 `--sandbox danger-full-access` 或桌面版 config 里的 `[windows] sandbox = "elevated"`。

## 6. 可调配置（config.toml）

| 键 | 作用 |
|---|---|
| `model_auto_compact_token_limit` | 软阈值，覆盖模型默认值 |
| `model_auto_compact_token_limit_scope` | `Total` / `BodyAfterPrefix`，计数口径 |
| `model_post_turn_compact_threshold_percent` | 回合末提前压缩百分比（0-100，见 config/mod.rs:3383 校验） |
| `compact_prompt` | 自定义压缩指令 |
| `instructions` / `developer_instructions` | 注入首轮上下文 |
| `project_doc_max_bytes` | AGENTS.md 截断上限 |

## 7. 限制与版本差异

- 摘要内容走服务端加密（远端 v2），本地无法审计摘要质量；本地路径（`compact.rs`）的摘要是明文。
- 用户消息保留预算 20k（本地）/ 64k（远端 v2），超长会话里最老的用户消息照样会丢——"永不压缩"是有预算的。
- 机制演进极快：二进制与源码里已有 `experimental_compact_prompt_file`、`async_compaction_threshold_percent` 等开关，桌面版实际行为取决于 core 版本与 feature flag，以源码为准。
