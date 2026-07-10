# Codex 后台监听机制解析：长任务为什么能挂着跑

日期：2026-07-10
分析版本：`openai/codex` commit `dc5ae37`
源码根目录占位：`<codex-source>`
本机验证项目：`/home/miku/projects/riichi-mahjong-recognition`

这篇讲一个很实用的点：

> Codex 怎么做到一边让长命令继续跑，一边把控制权还给 agent，之后还能继续监听输出？

结论先放前面：

- 这套能力主要不是旧 `shell_command`，而是 `unified_exec` 的 `exec_command` + `write_stdin`。
- `exec_command` 初次启动进程，只等 `yield_time_ms` 这么久；如果进程还活着，就返回 `session_id`。
- 活进程被放进 `UnifiedExecProcessManager` 的 `ProcessStore`，不会因为本轮工具调用返回就被杀掉。
- `write_stdin({ session_id, chars: "" })` 是“空输入轮询”，不是给进程写东西；它会取最近输出，顺便判断进程是否还活着。
- `start_streaming_output` 和 `spawn_exit_watcher` 是后台监听的两条异步线：一条持续发增量输出，一条等进程退出后发最终结束事件。
- TUI 的 `/ps`、`/stop` 只是这套进程表的 UI 管理入口。
- 本次动态验证真实启动了 YOLOv8 麻将训练：`exec_command` 返回 `session_id=90426`，随后用空 `write_stdin` 轮询到训练结束、GPU 负载和 checkpoint 输出。

## 1. 一张图看懂

```text
模型/agent 想跑一个长任务
        |
        v
exec_command
  cmd: "python train.py ..."
  yield_time_ms: 1000
        |
        v
ExecCommandHandler
  allocate_process_id()
  resolve cwd / shell / sandbox / permission
        |
        v
UnifiedExecProcessManager::exec_command
        |
        +-- open_session_with_sandbox(...)
        |     -> local PTY 或 remote exec-server process
        |
        +-- start_streaming_output(...)
        |     -> tokio task 持续读 stdout/stderr chunk
        |
        +-- store_process(...)
        |     -> ProcessStore[process_id] = ProcessEntry
        |     -> spawn_exit_watcher(...)
        |
        +-- collect_output_until_deadline(1s)
        |
        `-- 如果进程还活着：
              返回 ToolOutput { process_id: Some(id) }
              模型侧看到 session_id

之后：

write_stdin
  session_id: id
  chars: ""
        |
        v
UnifiedExecProcessManager::write_stdin
        |
        +-- prepare_process_handles(id)
        +-- collect_output_until_deadline(...)
        +-- refresh_process_state(id)
        |
        +-- still alive  -> 返回 process_id: Some(id)
        `-- exited       -> 返回 process_id: None + exit_code
```

一句话：

```text
不是 Codex 卡在命令那里等几个小时。
是命令进了进程表，Codex 每次只取一段输出，下一轮再按 session_id 轮询。
```

## 2. 工具层：为什么会出现 session_id

源码锚点：

- `<codex-source>/codex-rs/core/src/tools/handlers/shell_spec.rs:21`
- `<codex-source>/codex-rs/core/src/tools/handlers/shell_spec.rs:110`
- `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs:231`
- `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs:20`

`exec_command` 的工具说明直接写着：

```text
Runs a command in a PTY, returning output or a session ID for ongoing interaction.
```

它的关键参数：

```text
cmd
  要执行的 shell 命令。

tty
  true 时分配 PTY；false 或省略则用普通 pipe。

yield_time_ms
  初次等多久再返回。
  有效范围：250ms - 30000ms。

max_output_tokens
  单次工具返回给模型的输出预算。
```

`write_stdin` 是配套工具：

```text
session_id
  running unified exec session 的 id。

chars
  要写入 stdin 的字节。
  默认空字符串，代表只轮询输出，不输入内容。

yield_time_ms
  空轮询默认可以等 5000ms - 300000ms。
```

这里有个命名细节：源码内部叫 `process_id`，但工具参数叫 `session_id`。`write_stdin.rs` 里也写了注释：

```text
The model is trained on `session_id`.
```

也就是说，模型习惯看到的是 `session_id`，内部管理表用的是 `process_id`。

## 3. 初次启动：exec_command 如何把进程留下来

源码锚点：

- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:91`
- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:121`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:408`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:450`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:452`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:857`

`ExecCommandRequest` 里有几个字段很关键：

```text
command
process_id
yield_time_ms
cwd
tty
sandbox_permissions
additional_permissions
```

`ProcessStore` 是后台挂起能力的核心容器：

```text
ProcessStore
  processes: HashMap<i32, ProcessEntry>
  reserved_process_ids: HashSet<i32>
```

真正启动时，`UnifiedExecProcessManager::exec_command` 做这几步：

```text
1. open_session_with_sandbox(...)
   打开本地 PTY 或 exec-server 进程。

2. start_streaming_output(...)
   立刻启动后台 reader，持续读输出。

3. 判断 process_started_alive
   如果进程还没退出：
     store_process(...)

4. collect_output_until_deadline(...)
   只等 yield_time_ms，不无限等。

5. refresh_process_state(...)
   还活着：
     ToolOutput.process_id = Some(id)
   已退出：
     ToolOutput.process_id = None
```

最关键的一句注释在源码里：

```text
Persist live sessions before the initial yield wait so interrupting the
turn cannot drop the last Arc and terminate the background process.
```

这句话解释了为什么长任务不会因为工具调用先返回而死掉：它在初次等待前就被放进 `ProcessStore`，有 manager 持有 `Arc<UnifiedExecProcess>`。

## 4. 后台监听：输出不是等轮询时才读

源码锚点：

- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:37`
- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:40`
- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:53`
- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:104`
- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:107`

这里有两个后台 task。

第一条：输出流监听。

```text
start_streaming_output(process, context, transcript)
        |
        v
tokio::spawn(async move {
  loop {
    receiver.recv()
      -> process_chunk(...)
      -> transcript.push_chunk(...)
      -> EventMsg::ExecCommandOutputDelta(...)
  }
})
```

这条线负责“边跑边看见输出”。注意它不是等 `write_stdin` 时才读输出，而是进程一启动就有 reader 在后台持续收 chunk。

第二条：退出监听。

```text
spawn_exit_watcher(...)
        |
        v
tokio::spawn(async move {
  exit_token.cancelled().await
  output_drained.notified().await
  emit_exec_end_for_unified_exec(...)
})
```

这条线负责进程最终结束时发一个完整的 end event。它用 transcript 作为最终聚合输出来源。

所以“后台监听”其实是：

```text
live process
  + output reader task
  + exit watcher task
  + retained transcript buffer
  + process table entry
```

不是一个单独的“监听命令”，而是一组 async task 和共享状态。

## 5. 轮询：write_stdin 空输入到底做了什么

源码锚点：

- `<codex-source>/codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs:69`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:636`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:690`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:740`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:791`

空轮询路径：

```text
write_stdin({ session_id: 50234, chars: "" })
        |
        v
parse args
        |
        v
unified_exec_manager.write_stdin(...)
        |
        +-- prepare_process_handles(process_id)
        |     从 ProcessStore 找进程
        |
        +-- 因为 input 为空，不写 stdin
        |
        +-- collect_output_until_deadline(...)
        |     从 output_buffer 取新 chunk
        |
        +-- refresh_process_state(process_id)
              Alive  -> process_id: Some(id)
              Exited -> store.remove(id), process_id: None
```

`write_stdin.rs` 里还有一个 UI 细节：

```text
Empty stdin is a background poll.
```

空轮询只有在进程还活着时才发 `TerminalInteractionEvent`；如果这次轮询刚好看到进程结束，就不再把它伪装成一次用户交互。

## 6. 输出为什么不会无限爆炸

源码锚点：

- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:64`
- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:67`
- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:69`
- `<codex-source>/codex-rs/core/src/unified_exec/mod.rs:71`
- `<codex-source>/codex-rs/core/src/unified_exec/head_tail_buffer.rs:4`
- `<codex-source>/codex-rs/core/src/unified_exec/async_watcher.rs:29`

几个硬边界：

```text
MIN_YIELD_TIME_MS = 250
MAX_YIELD_TIME_MS = 30000
MIN_EMPTY_YIELD_TIME_MS = 5000
DEFAULT_MAX_BACKGROUND_TERMINAL_TIMEOUT_MS = 300000
UNIFIED_EXEC_OUTPUT_MAX_BYTES = 1 MiB
MAX_UNIFIED_EXEC_PROCESSES = 64
```

输出缓冲不是完整保存无限日志，而是 `HeadTailBuffer`：

```text
HeadTailBuffer
  head: 保留开头
  tail: 保留结尾
  omitted_bytes: 统计中间丢了多少
```

这个设计很适合训练任务：

```text
开头：
  参数、数据集、模型、环境

中间：
  大量 epoch 进度，必要时可丢

结尾：
  final metrics、保存路径、错误栈
```

另外每个输出增量事件也有限制：`async_watcher.rs` 里把单个 delta 控制在 8192 bytes，避免 JSON-RPC/UI 一次吞超大块。

## 7. /ps 和 /stop 只是 UI 管理层

源码锚点：

- `<codex-source>/codex-rs/tui/src/slash_command.rs:112`
- `<codex-source>/codex-rs/tui/src/chatwidget/slash_dispatch.rs:469`
- `<codex-source>/codex-rs/tui/src/chatwidget.rs:1462`
- `<codex-source>/codex-rs/core/src/tasks/mod.rs:818`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:1392`
- `<codex-source>/codex-rs/core/src/unified_exec/process_manager.rs:1411`

TUI 注册了两个命令：

```text
/ps
  list background terminals

/stop
  stop all background terminals
```

对应派发：

```text
SlashCommand::Ps
  -> chat_widget.add_ps_output()

SlashCommand::Stop
  -> chat_widget.clean_background_terminals()
  -> AppCommand::CleanBackgroundTerminals
  -> app_server.thread_background_terminals_clean(thread_id)
```

核心侧有列表和终止接口：

```text
Session::list_background_terminals
  -> unified_exec_manager.list_processes()

Session::terminate_background_terminal(process_id)
  -> unified_exec_manager.terminate_process(process_id)
```

`list_processes` 只返回还没退出的进程：

```text
BackgroundTerminalInfo
  item_id
  process_id
  command
  cwd
```

这说明 `/ps` 看到的不是系统全局进程，而是 Codex 当前线程管理的 unified exec 后台进程。

## 8. 动态验证：这次真实跑到的现象

### 8.1 可控后台 session

我用一个 8 秒 shell 命令复现了后台挂起和空轮询：

```text
exec_command:
  for i in $(seq 1 8); do
    printf 'codex-bg-demo step=%s time=%s\n' "$i" "$(date +%H:%M:%S)"
    sleep 1
  done

yield_time_ms = 1000
```

初次返回：

```text
Process running with session ID 50234
codex-bg-demo step=1 time=19:24:27
codex-bg-demo step=2 time=19:24:28
```

然后空轮询：

```text
write_stdin(session_id=50234, chars="", yield_time_ms=2500)
```

拿到剩余输出并看到进程结束：

```text
codex-bg-demo step=3 time=19:24:29
codex-bg-demo step=4 time=19:24:30
codex-bg-demo step=5 time=19:24:31
codex-bg-demo step=6 time=19:24:32
codex-bg-demo step=7 time=19:24:33
codex-bg-demo step=8 time=19:24:34
Process exited with code 0
```

这条动态证据证明：

```text
初次工具调用不需要等完整进程结束。
session_id 可以把后台进程重新接上。
空 write_stdin 是真实的后台轮询。
```

### 8.2 YOLOv8 麻将项目：这次真实后台训练

本机项目存在：

```text
/home/miku/projects/riichi-mahjong-recognition
```

关键文件：

```text
scripts/train_yolo.py
data/yolo_dataset_final/mahjong.yaml
```

环境检查：

```text
torch 2.6.0+cu124
ultralytics 8.4.67
GPU: NVIDIA GeForce RTX 4060 Laptop GPU
```

沙箱内 `torch.cuda.is_available()` 是 `False`，提权 GPU 检查是 `True`。训练类场景确实依赖更高权限访问 NVIDIA 设备。

这次启动的真实命令：

```bash
.venv/bin/python scripts/train_yolo.py \
  --mode train \
  --data data/yolo_dataset_final/mahjong.yaml \
  --model-size n \
  --img-size 640 \
  --batch-size 8 \
  --epochs 1 \
  --device 0
```

工作目录：

```text
/home/miku/projects/riichi-mahjong-recognition
```

初次工具调用：

```text
exec_command(command=..., yield_time_ms=1000)
  -> Process running with session ID 90426
```

这一步证明的不是“命令已经结束”，而是：

```text
YOLO 训练进程还活着。
Codex 已经把它登记进 unified exec 的进程表。
后续必须用 session_id=90426 继续轮询。
```

轮询过程：

```text
poll #1:
  读取模型、数据集扫描、训练配置。
  日志确认 CUDA:0 = NVIDIA GeForce RTX 4060 Laptop GPU。
  输出目录进入 mahjong_detection-6。

poll #2:
  训练进度从约 16% 继续推进到约 98%。
  日志出现 500 个 batch 的进度条。
  GPU_mem 约 1.22G。

poll #3:
  训练到 100%。
  开始 validation。
  输出 mAP 指标、best.pt、last.pt。
  进程退出码为 0。
```

训练期间的 GPU 采样：

```text
time                     temp  power   util  mem
2026/07/10 19:28:54.779  62C   69.15W  86%   1433MiB
2026/07/10 19:29:16.881  68C   59.48W  66%   1811MiB
2026/07/10 19:29:31.117  57C   1.50W   0%    15MiB
```

关键训练输出：

```text
Ultralytics 8.4.67
Python-3.13.13
torch-2.6.0+cu124
CUDA:0 (NVIDIA GeForce RTX 4060 Laptop GPU, 7806MiB)

1 epochs completed in 0.009 hours.

all:
  images: 750
  instances: 6750
  Box(P): 0.0175
  R: 0.523
  mAP50: 0.0176
  mAP50-95: 0.0167

Speed:
  0.2ms preprocess
  1.4ms inference
  0.0ms loss
  0.7ms postprocess per image
```

训练产物：

```text
/mnt/ssd512/miku-home/projects/riichi-mahjong-recognition/runs/detect/runs/train/mahjong_detection-6
  weights/best.pt
  weights/last.pt
```

这就是“玩真的”的动态证据：

```text
真实 GPU 训练
  -> exec_command 初次只等 1 秒
  -> 返回 session_id=90426
  -> 训练继续在后台跑
  -> 空 write_stdin 多次接回输出
  -> Codex 看到最终指标和退出码
```

### 8.3 提权边界和长任务复现方式

第一次启动 GPU 训练时，提权审查拒绝了命令，因为它会消耗 GPU 并写训练产物。用户重新明确授权后，同一类 1-epoch YOLO smoke 才被启动并完成。

这点和源码层面的权限链路是对得上的：

```text
ExecCommandHandler
  -> parse sandbox_permissions
  -> permission / approval check
  -> UnifiedExecProcessManager::exec_command
  -> open_session_with_sandbox
```

如果没有通过权限边界，进程不会进入 `ProcessStore`。一旦通过并启动成功，后面的后台监听机制就和普通长 shell 一样：

```text
exec_command(yield_time_ms=1000)
  -> session_id

write_stdin(session_id, chars="", yield_time_ms=5000~300000)
  -> 取输出
  -> 刷新进程状态
  -> 还活着就继续返回 session_id
  -> 退出则返回 exit_code
```

把这次 1-epoch 换成 100 epoch，机制不变，只是轮询间隔变长：

```text
1 epoch smoke:
  每 5-30 秒轮询一次即可。

100 epoch / SFT:
  每 1-5 分钟轮询一次更合理。
  重点看 loss、eval 指标、checkpoint、错误栈。
```

## 9. 为什么这适合 SFT / 训练任务

训练任务通常有这些特点：

```text
启动信息很重要：
  模型、数据、batch、device、输出目录

中间日志很多：
  epoch 进度、loss、显存、速度

结尾最重要：
  best checkpoint、last checkpoint、metrics、错误栈

人不需要每秒盯：
  只需要每几十秒/几分钟看一次状态
```

Codex 的后台 exec 正好匹配：

```text
yield_time_ms 短：
  先把控制权还给 agent。

session_id：
  后面可以继续接上。

HeadTailBuffer：
  保留开头和结尾，避免日志爆炸。

empty write_stdin：
  不干扰训练进程，只取最近输出。

/ps：
  查看当前后台任务。

/stop：
  需要时清理后台任务。
```

这就是为什么你之前跑 SFT 时会感觉“Codex 很适合挂着看”：它不是一个 blocking shell，而是一个带 process table、输出缓冲、事件流和轮询工具的后台终端管理器。

## 10. 最短源码阅读路线

按这个顺序读：

```text
1. 工具 schema
   core/src/tools/handlers/shell_spec.rs

2. 初次执行
   core/src/tools/handlers/unified_exec/exec_command.rs
   core/src/unified_exec/process_manager.rs::exec_command

3. 进程持有
   core/src/unified_exec/mod.rs::ProcessStore
   core/src/unified_exec/process_manager.rs::store_process

4. 输出监听
   core/src/unified_exec/async_watcher.rs::start_streaming_output
   core/src/unified_exec/async_watcher.rs::spawn_exit_watcher

5. 轮询与交互
   core/src/tools/handlers/unified_exec/write_stdin.rs
   core/src/unified_exec/process_manager.rs::write_stdin

6. 输出截断
   core/src/unified_exec/head_tail_buffer.rs

7. UI 管理
   tui/src/slash_command.rs
   tui/src/chatwidget/slash_dispatch.rs
   tui/src/chatwidget.rs::add_ps_output
   core/src/tasks/mod.rs::list_background_terminals
```

最后记住这条主线：

```text
exec_command 负责启动长任务并把活进程登记进 ProcessStore。
start_streaming_output 负责后台持续收输出。
write_stdin 空输入负责后续轮询。
HeadTailBuffer 负责日志不会无限爆。
/ps 和 /stop 负责让用户管理这些后台终端。
```
