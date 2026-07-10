# opencode 运行期开销实测与 TypeScript 高性能开发笔记

日期：2026-07-08  
实测对象：`opencode 1.17.13` 二进制、`anomalyco/opencode` 本地源码树  
测试机器：Linux 6.17.0-35-generic，Ubuntu 24.04.3，x86_64，Bun 1.3.14，Node v22.19.0

这篇文档回答一个更工程化的问题：

> opencode 作为 TypeScript/Bun 写成的 code agent，真实运行时文件开销、网络 IO、内存占用和事件流吞吐大概能到什么量级？

结论先放前面：

- HTTP 框架基线可以到 `7.9k-8.8k RPS`；code agent 的真实上限要看 agent turn 链路。
- 多 session 状态读取约 `1.4k-1.7k RPS`；大量 session 后的列表查询会掉到 `68 RPS`。
- session 创建写入约 `1.2k RPS`，对应进程 block write 约 `82 MB/s`。
- 真正接近本地 code-agent 工具执行的 `POST /session/:id/shell` 上限约 `166-176 RPS`。并发从 `128` 拉到 `512` 不再涨吞吐，只会把 p99 从 `895ms` 拉到 `3.2s`。
- 真实模型驱动 `opencode run` agent turn 远低于 direct shell：二进制 single-tool 稳定并发 `2` 时约 `0.158 turn/s`；并发 `4` 开始出现 `database is locked`。
- 多 subagent 场景下，二进制 `2` 个 subagent、并发 `2` 时约 `0.056 turn/s`；`4` 个 subagent、并发 `1` 时约 `0.025 turn/s`。
- 源码 TS 入口能跑通 `opencode run` 模型驱动路径，但并发 `2` 同样会出现 `database is locked`；源码 single-tool 成功样本约 `0.069-0.099 turn/s`，`2` subagent 单并发约 `0.031 turn/s`。
- `opencode --mini` 不适合作为 10GiB 批量压测入口：二进制 mini 在 10GiB address-space limit 下输入 prompt 后出现 OpenTUI/JSC `RangeError: Out of memory` 或 Bun `Illegal instruction`；源码 mini 也出现 JSC memory exhaustion 或卡在 `BUILD` 不触发工具。
- shell 工具路径的主要开销集中在 session/message/part 写入、事件处理、shell 子进程创建和输出回写；实测 block write 稳定在 `50-54 MB/s`。
- 源码 TS 入口补测：direct shell tool 约 `166 RPS`；direct shell tool 加真实 4KB 文件写入约 `181 RPS`，写出 `2812` 个文件。这个路径仍低于完整模型驱动 agent turn。
- 进程用 `prlimit --as=10737418240` 设置了 `10 GiB` 地址空间上限；压测后 RSS 能从峰值 `1.28 GB` 冷却回 `522 MB`，FD 稳定 `21`。
- SSE/event stream 仍是单独风险点：短连接反复打开/关闭时曾出现 RSS 不回落和 `MaxListenersExceededWarning`，普通 HTTP RPS 覆盖不到这个问题。

---

## 模型驱动 agent turn 压测

这一轮改成真实模型驱动路径，不再用 direct HTTP shell endpoint 代表 code agent：

```bash
prlimit --as=10737418240 -- \
opencode run --auto \
  -m xiaomi-token-plan-sgp/mimo-v2.5-pro \
  --agent primary-controller \
  --dir /tmp/opencode-model-turn-bench/... \
  --format json \
  'Use the bash/task tools..., verify files with cat...'
```

源码版入口：

```bash
prlimit --as=10737418240 -- \
bun run --cwd /srv/storage/projects/opencode-anomaly/packages/opencode \
  --conditions=browser ./src/index.ts run \
  --dangerously-skip-permissions \
  -m xiaomi-token-plan-sgp/mimo-v2.5-pro \
  --agent primary-controller \
  --dir /tmp/opencode-model-turn-bench/... \
  --format json \
  'Use the bash/task tools..., verify files with cat...'
```

可复跑脚本：

```bash
python3 bench/opencode_model_turn_bench.py \
  --profile binary \
  --mode subagents \
  --subagents 2 \
  --turns 4 \
  --concurrency 2 \
  --workdir /tmp/opencode-model-turn-bench/binary-subagents2-c2 \
  --out-dir /tmp/opencode-model-turn-bench/results/binary-subagents2-c2
```

这个脚本采集：

- `agent_turn_rps`：完整模型驱动 turn/s。
- 每个 turn 的 wall time、成功/失败、session id。
- JSON event 里的 `bash` / `task` / `write` / `todowrite` 工具次数。
- 子 agent session id。
- 文件内容验证结果。
- `/proc/<pid>/io` 聚合读写量、峰值 RSS、进程数。
- `/proc/net/dev` 的 loopback 和外部网卡收发字节。

二进制 `opencode 1.17.13` 结果：

| 场景 | turn | 并发 | 成功 | turn/s | p50 | p95/p99 | 峰值 RSS | 进程数峰值 | 工具调用 | 关键结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| single bash/write | `8` | `2` | `8/8` | `0.158` | `11.26s` | `15.41s` | `1.09 GB` | `8` | `bash=15, write=1` | 稳定上限口径 |
| single bash/write | `8` | `4` | `6/8` | `0.240` 成功 turn/s | `11.35s` | `13.72s` | `2.09 GB` | `8` | `bash=11` | 出现 `database is locked`，不可视为稳定上限 |
| `2` subagent/turn | `4` | `2` | `4/4` | `0.056` | `32.68s` | `38.18s` | `1.09 GB` | `4` | `task=8, bash=7, todowrite=3` | 稳定多 subagent 主口径 |
| `4` subagent/turn | `2` | `1` | `2/2` | `0.025` | `39.06s` | `42.67s` | `586 MB` | `2` | `task=8, bash=4, todowrite=2` | 单 turn 内 subagent 加深后延迟接近 `40s` |

二进制 IO：

| 场景 | proc write_bytes | proc read_bytes | loopback RX/TX | 外部网卡 RX/TX |
|---|---:|---:|---:|---:|
| single, 并发 `2`, `8/8` | `64.9 MB` | `0 MB` | `1.07/1.07 MB` | `1.14/3.33 MB` |
| single, 并发 `4`, `6/8` | `48.4 MB` | `0.01 MB` | `0.46/0.46 MB` | `0.67/2.20 MB` |
| `2` subagent, 并发 `2`, `4/4` | `59.1 MB` | `0.02 MB` | `0.69/0.69 MB` | `1.40/6.10 MB` |
| `4` subagent, 并发 `1`, `2/2` | `45.8 MB` | `0 MB` | `0.91/0.91 MB` | `1.61/5.28 MB` |

源码 TS 入口结果：

| 场景 | turn | 并发 | 成功 | turn/s | p50 | p95/p99 | 峰值 RSS | 工具调用 | 关键结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| single bash/write | `1` | `1` | `1/1` | `0.069` | `14.31s` | `14.31s` | `854 MB` | `bash=2` | 源码 `run` + 全模型名可用 |
| single bash/write | `4` | `2` | `3/4` | `0.099` 成功 turn/s | `14.18s` | `14.93s` | `1.61 GB` | `bash=5` | 并发启动会撞 `database is locked` |
| `2` subagent/turn | `2` | `1` | `2/2` | `0.031` | `32.12s` | `34.07s` | `860 MB` | `task=4, bash=3` | 源码多 subagent 单并发可用 |

源码版注意点：

- `run` 的跳过权限参数是 `--dangerously-skip-permissions`；新版二进制使用 `--auto`。
- `run` 必须用全模型名 `xiaomi-token-plan-sgp/mimo-v2.5-pro`；短名 `mimo-v2.5-pro` 会被解析成 `mimo-v2.5-pro/.` 并报 `ProviderModelNotFoundError`。
- 源码 `run` 单并发能完成真实 bash/task 工具调用；并发进程主要风险仍是共享 SQLite 锁。

这一轮的生产结论：

```text
direct shell endpoint: ~170 RPS
model-driven single tool turn: ~0.16 turn/s stable, ~0.24 turn/s starts failing
model-driven 2-subagent turn: ~0.056 turn/s stable
model-driven 4-subagent turn: ~0.025 turn/s stable
```

code agent 的生产上限取决于：

```text
模型调用延迟
  + 父 session 写入
  + task 子 session 创建和写入
  + 共享 SQLite 锁竞争
  + 工具调用结果回写
  + 最终父 session 汇总
```

在当前配置下，多进程并发 `opencode run` 的第一个硬瓶颈是 `database is locked`。10GiB 内存上限和文件写入带宽还没先撞上。

### mini TUI 压测失败记录

为了满足“先调出 opencode 再交互输入”的口径，实际验证过 `opencode --mini`：

- 普通手工/单次交互可以成功：mini UI 打开后输入 prompt，允许 `/tmp/*` 权限，模型调用 bash 写文件并 cat 验证。
- 但作为 10GiB 批量压测入口失败：
  - Python `pty.openpty()` 驱动二进制 mini，输入 prompt 后 Bun `1.3.14` 报 `panic: Illegal instruction`。
  - `tmux` 驱动二进制 mini，输入 prompt 后 OpenTUI/JSC 报 `RangeError: Out of memory`，栈在 `editBufferGetText` / `onContentChange`。
  - 源码 mini + 全模型名在 10GiB 下触发 `ASSERTION FAILED: MemoryExhaustion`。
  - 源码 mini + 短模型名能进入 UI，但卡在 `BUILD`，没有观察到 bash/tool 调用，也没有写出文件。

因此，`--mini` 可以作为人工 smoke test，但不能作为这轮 10GiB 极限压测的稳定 driver。正式 RPS/turn/s 采用 `opencode run --format json`，它仍然经过真实模型、真实工具、真实 session storage 和真实文件验证。

---

## 生产口径压测结果

这一轮直接给 RPS、延迟、网络吞吐和文件 IO。

启动方式：

```bash
OPENCODE_SERVER_PASSWORD=bench \
OPENCODE_DISABLE_MODELS_FETCH=1 \
prlimit --as=10737418240 -- \
opencode serve --pure --hostname 127.0.0.1 --port 19400 --print-logs --log-level ERROR
```

确认到的资源限制：

```text
AS address space limit: 10737418240 bytes
NOFILE: 1048576
```

测试口径：

- 进程：`opencode 1.17.13` 二进制 server。
- 内存上限：`10 GiB` address space。
- agent：`primary-controller`。
- 多 agent/session 池：预创建 `2000` 个 session，压测时按 session 轮询。
- 本地工具命令：`printf ok`。
- 鉴权：Basic `opencode:bench`。
- 观测：benchmark client 输出 RPS/p95/p99，`/proc/<pid>/io` 记录文件 IO，`/proc/net/dev` 记录 loopback 网络 IO。
- 不测外部 LLM provider：`POST /session/:id/message` 会被模型 provider 限速、计费和网络延迟主导；本节重点是 opencode 本机 runtime 能承受的状态和工具 IO。

绝对 RPS 表：

| 路径 | 并发 | 数据规模 | RPS | p95 | p99 | 结论 |
|---|---:|---:|---:|---:|---:|---|
| `GET /health` | `128` | 无状态 | `8823` | `21ms` | `30ms` | HTTP 框架短请求基线 |
| `GET /health` | `512` | 无状态 | `7877` | `78ms` | `112ms` | 高并发下仍接近 8k RPS |
| `POST /session` | `128` | 持续创建 | `1223` | `123ms` | `174ms` | session 写入上限 |
| `GET /session/:id` | `256` | `2000` session 池 | `1740` | `160ms` | `224ms` | 单 session 信息读取 |
| `GET /session/:id/message?limit=20` | `256` | `2000` session 池 | `1384` | `204ms` | `239ms` | message list 读取 |
| `GET /session?limit=50` | `256` | 大量 session 后 | `68` | `3791ms` | `3817ms` | 全局 session list 是明显瓶颈 |
| `POST /session/:id/shell` | `128` | `2000` session 池 | `176` | `793ms` | `895ms` | 本地工具执行最佳点 |
| `POST /session/:id/shell` | `256` | `2000` session 池 | `173` | `1523ms` | `1539ms` | 进入平台期，延迟翻倍 |
| `POST /session/:id/shell` | `512` | `2000` session 池 | `166` | `3235ms` | `3243ms` | 过饱和，无吞吐收益 |

IO 吞吐表：

| 场景 | RPS | 响应 body 吞吐 | loopback 吞吐 | block write | 说明 |
|---|---:|---:|---:|---:|---|
| `GET /health`, 512 并发 | `7877` | `22.7 MB/s` | `28.8 MB/s` | `0 MB/s` | HTTP/network 基线 |
| `POST /session`, 128 并发 | `1223` | `0.46 MB/s` | `1.25 MB/s` | `82 MB/s` | SQLite/WAL 写放大明显 |
| `GET /session/:id`, 256 并发 | `1740` | `0.63 MB/s` | `1.58 MB/s` | `0 MB/s` | 主要是用户态读和 JSON 序列化 |
| `GET /session/:id/message`, 256 并发 | `1384` | `0.003 MB/s` | `0.77 MB/s` | 近似 `0 MB/s` | 空消息列表，响应小 |
| `GET /session?limit=50`, 256 并发 | `68` | `1.24 MB/s` | `0.17 MB/s` | 近似 `0 MB/s` | 状态规模变大后查询/投影慢 |
| `POST /session/:id/shell`, 128 并发 | `176` | `0.15 MB/s` | `0.27 MB/s` | `54 MB/s` | 工具执行、part 更新、输出回写 |
| `POST /session/:id/shell`, 256 并发 | `173` | `0.15 MB/s` | `0.28 MB/s` | `52 MB/s` | 吞吐不涨，排队变长 |
| `POST /session/:id/shell`, 512 并发 | `166` | `0.14 MB/s` | `0.27 MB/s` | `50 MB/s` | 过饱和 |

内存和 FD：

| 场景 | 压测前 RSS | 压测后 RSS | 冷却后 RSS | FD |
|---|---:|---:|---:|---:|
| `GET /health`, 128 并发 | `348 MB` | `597 MB` | `332 MB` | `21` |
| `POST /session`, 128 并发 | `341 MB` | `693 MB` | `384 MB` | `21` |
| `POST /session/:id/shell`, 128 并发 | `473 MB` | `842 MB` | `483 MB` | `21` |
| `POST /session/:id/shell`, 512 并发 | `489 MB` | `1284 MB` | `522 MB` | `21` |

这一轮最重要的结论：

```text
opencode HTTP server 可以接近 8k RPS；
opencode session 状态读大约 1.4k-1.7k RPS；
opencode session 创建写大约 1.2k RPS；
opencode 本地 shell 工具执行约 170 RPS。
```

生产环境 code agent 的本机极限要看这条链路：

```text
agent turn
  -> session/message/part 写入
  -> event publish
  -> shell/tool 子进程或 MCP/tool 网络调用
  -> output metadata 回写
  -> session 状态查询/回放
```

在这个链路上，本机工具执行已经在 `~170 RPS` 进入平台期。继续增加并发只会拉高延迟，不会提高吞吐。

---

## 源码 TS 入口补测

上面的生产 RPS 表主要用的是安装好的二进制：

```text
/home/miku/.opencode/bin/opencode
opencode 1.17.13
```

为了回答“源码跑起来会怎样”，又用本地源码入口补测了一轮：

```bash
XDG_DATA_HOME=/tmp/opencode-source-homecfg-data \
XDG_CACHE_HOME=/tmp/opencode-source-homecfg-cache \
XDG_STATE_HOME=/tmp/opencode-source-homecfg-state \
OPENCODE_SERVER_PASSWORD=bench \
prlimit --as=10737418240 -- \
bun run --cwd /srv/storage/projects/opencode-anomaly/packages/opencode \
  --conditions=browser ./src/index.ts serve \
  --pure --hostname 127.0.0.1 --port 19411 --print-logs --log-level ERROR
```

源码树：

```text
/srv/storage/projects/opencode-anomaly
package version: 1.17.11
entry: packages/opencode/src/index.ts
```

有效结果：

| 路径 | 并发 | 数据规模 | RPS | p95 | p99 | 说明 |
|---|---:|---:|---:|---:|---:|---|
| source `POST /session` | `128` | 持续创建 | `1079` | `136ms` | `195ms` | 比二进制 `1223 RPS` 低约 `12%` |
| source `GET /session/:id` | `256` | `2000` session 池 | `1698` | `163ms` | `225ms` | 和二进制 `1740 RPS` 接近 |
| source direct `POST /session/:id/shell` | `128` | `1000` session 池，`printf ok` | `166` | `825ms` | `875ms` | 和二进制 shell 平台期接近 |
| source direct `POST /session/:id/shell` | `128` | `2000` session 池，写 4KB 文件 | `181` | `753ms` | `768ms` | 写出 `2812` 个文件到 `/tmp/opencode-source-file-bench` |

源码 direct shell + 文件写入的命令：

```bash
mkdir -p /tmp/opencode-source-file-bench
head -c 4096 /dev/zero > /tmp/opencode-source-file-bench/file-$(date +%s%N)-$$
```

文件写入验证：

```text
files: 2812
directory size: 12 MB
opencode source data dir: 125 MB
process block write delta: ~771 MB / 15.6s
```

这里要非常明确地区分两种路径：

| 路径 | 是否模型驱动 | 是否真实工具执行 | 是否写 session storage | 是否写项目/文件 |
|---|---:|---:|---:|---:|
| direct `POST /session/:id/shell` | 否 | 是，执行 bash tool | 是 | 取决于 command，本次已写 `/tmp` 文件 |
| `POST /session/:id/message` | 是 | 由模型决定 | 是 | 由模型/tool 决定 |

这组 direct HTTP 结果之后，又补了 CLI `run` 模型驱动压测，见上面的“模型驱动 agent turn 压测”。需要区分两个历史现象：

1. 旧的源码 HTTP `POST /session/:id/message` smoke test 没有得到有效 RPS。当时 HTTP message 路径触发了 provider/model resolution 问题：

```text
ProviderModelNotFoundError:
  Model not found: openai/gpt-5.5

ProviderModelNotFoundError:
  Model not found: xiaomi-token-plan-sgp/mimo-v2.5-pro
  Did you mean: mimo-v2.5-pro?
```

2. 当前源码 CLI `run` 路径可以用全模型名跑通：

```bash
bun run --cwd /srv/storage/projects/opencode-anomaly/packages/opencode \
  --conditions=browser ./src/index.ts run \
  --dangerously-skip-permissions \
  -m xiaomi-token-plan-sgp/mimo-v2.5-pro \
  --agent primary-controller \
  --format json ...
```

因此，源码树现在可以做 CLI `run` 模型驱动压测；但 HTTP message 路径和 mini TUI 路径仍需单独看待，不能把它们的失败混同为工具执行能力失败。

---

## 测试范围

本次测了三类路径：

| 类别 | 测试内容 | 关注点 |
|---|---|---|
| CLI 冷启动 | `--version`、`debug paths`、`debug info` | 进程启动、模块加载、文件 IO |
| server 空闲 | `opencode serve --pure` | 常驻内存、FD、线程、监听 socket |
| runtime IO | `/health`、`/event`、`/global/event` | HTTP 吞吐、SSE 订阅、内存回收 |

测试时尽量避开模型 provider 的外部网络调用，避免把 LLM 延迟混进 runtime 基准：

```bash
OPENCODE_DISABLE_MODELS_FETCH=1 opencode serve --pure --hostname 127.0.0.1 --port 19330 --print-logs --log-level ERROR
```

这里的结果适合判断本机 runtime 上限和趋势，不等价于云端多用户压测。

---

## 本机安装与源码体积

| 项目 | 体积 |
|---|---:|
| `~/.opencode/bin/opencode` | `160 MB` |
| `~/.opencode` | `58 MB` |
| `~/.local/share/opencode` | `617 MB` |
| `~/.cache/opencode` | `8.1 MB` |
| `/srv/storage/projects/opencode-anomaly` | `3.8 GB` |
| `/mnt/ssd512/opencode` | `153 MB` |

`/srv/storage/projects/opencode-anomaly` 体积明显大，是因为包含完整工作树和依赖；`/mnt/ssd512/opencode` 更接近干净源码树。

本机 `opencode` 命令实际指向：

```text
/home/miku/.opencode/bin/opencode
version: 1.17.13
binary size: 167,639,168 bytes
```

---

## CLI 冷启动开销

用 `/usr/bin/time -v` 观察二进制命令：

| 命令 | wall time | max RSS | file input | file output |
|---|---:|---:|---:|---:|
| `opencode --version` | `0.32s` | `187300 KB` | `24 KB` | `8 KB` |
| `opencode debug paths` | `0.33s` | `187852 KB` | `0 KB` | `8 KB` |
| `opencode debug info` | `0.57s` | `317924 KB` | `8984 KB` | `120 KB` |

同样用本地源码 TS 入口跑：

```bash
bun run --cwd /srv/storage/projects/opencode-anomaly/packages/opencode --conditions=browser ./src/index.ts --version
```

| 命令 | wall time | max RSS | file input | file output |
|---|---:|---:|---:|---:|
| source `--version` | `1.34s` | `246916 KB` | `75648 KB` | `8 KB` |
| source `debug info` | `1.78s` | `338068 KB` | `112344 KB` | `112 KB` |

这里能看到 TypeScript 源码入口的典型开发态成本：需要从源码和依赖图加载大量模块，冷启动显著慢于打包二进制。对常驻进程来说，这个成本主要发生在启动期；对频繁短命令 CLI 来说，它会直接影响体感。

---

## 文件系统 syscall 对比

用 `strace -f -c -e trace=file` 看 `debug info`：

| 入口 | file syscalls | 主要来源 |
|---|---:|---|
| 二进制 `opencode debug info` | `185` | 少量配置、路径、系统信息读取 |
| 源码 TS 入口 `debug info` | `8808` | Bun/TS 模块解析、依赖加载、package 查找 |

二进制路径的 syscall 摘要：

```text
185 total
92 openat
21 newfstatat
23 readlink
24 access
12 statx
9 mkdir
```

源码 TS 入口的 syscall 摘要：

```text
8808 total
7313 openat
452 newfstatat
457 statx
398 readlink
84 access
68 stat
5 execve
```

这说明“TypeScript 慢”这个判断要拆开看：

- 开发态源码入口的模块发现和加载确实重。
- 打包成单文件或少文件运行后，文件 IO 压力会下降很多。
- 真正进入常驻 server 后，性能瓶颈更多转向内存状态、事件订阅和网络流。

---

## 网络 syscall 基线

`opencode debug info` 本身几乎不产生外部网络 IO。用 `strace -f -c -e trace=network` 看，只有 `12` 次 network syscall：

```text
7 recvmsg
2 sendto
1 socket
1 bind
1 getsockname
```

这类 debug 命令属于本地诊断，不代表真实 agent 调模型时的网络开销。真实 agent 路径会多出 provider streaming、重试、MCP/tool 网络调用和日志传输。

---

## Server 空闲开销

二进制 server：

```bash
OPENCODE_DISABLE_MODELS_FETCH=1 opencode serve --pure --hostname 127.0.0.1 --port 19330 --print-logs --log-level ERROR
```

空闲采样：

| 时间 | RSS | FD | threads | TCP |
|---|---:|---:|---:|---|
| `0s` | `292 MB` | `21` | `13` | `1 LISTEN` |
| `5s` | `292 MB` | `21` | `14` | `1 LISTEN` |
| `15s` | `292 MB` | `21` | `11` | `1 LISTEN` |
| `30s` | `292 MB` | `21` | `14` | `1 LISTEN` |

源码 TS 入口 server：

| 时间 | RSS | FD | threads | TCP |
|---|---:|---:|---:|---|
| `0s` | `367 MB` | `23` | `17` | `1 LISTEN` |
| `5s` | `363 MB` | `23` | `17` | `1 LISTEN` |
| `15s` | `363 MB` | `23` | `15` | `1 LISTEN` |

结论：常驻 server 基线内存并不低，二进制约 `292 MB`，源码入口高出约 `70 MB`。如果要做 code agent 长时间常驻，这个数值是容量规划的底座。

---

## 普通 HTTP 吞吐：`/health`

对二进制 server 连续打 `2000` 次 `/health`：

| 指标 | 结果 |
|---|---:|
| 请求数 | `2000` |
| 总耗时 | `1428 ms` |
| 压测前 RSS | `292 MB` |
| 压测后 RSS | `332 MB` |
| RSS 增量 | `+39 MB` |
| FD | `21` |
| TCP | `1 LISTEN` + `2000 TIME-WAIT` |

这个结果说明普通短 HTTP 请求压力不大。FD 没增长，连接关闭后进入内核 `TIME-WAIT`，用户态进程没有明显句柄泄露。

---

## 运行时状态最大 IO 压测：SSE event stream

对 `/event` 和 `/global/event` 做短连接压测。每轮启动 `250` 个 `/event` 和 `250` 个 `/global/event`，连接用 `timeout 0.1 curl -sN` 短时间后断开。

| 轮次 | 累计 SSE 连接 | RSS | RSS 增量 | FD | TCP |
|---|---:|---:|---:|---:|---|
| 1 | `500` | `398 MB` | `+66 MB` | `21` | `1 LISTEN` + `2374 TIME-WAIT` |
| 2 | `1000` | `459 MB` | `+127 MB` | `21` | `1 LISTEN` |
| 3 | `1500` | `504 MB` | `+173 MB` | `21` | `1 LISTEN` |
| 4 | `2000` | `548 MB` | `+217 MB` | `21` | `1 LISTEN` |

总耗时约 `5067 ms`。随后冷却观察：

| 冷却时间 | RSS | FD | TCP |
|---|---:|---:|---|
| `5s` | `549 MB` | `21` | `1 LISTEN` + `3682 TIME-WAIT` |
| `30s` | `554 MB` | `21` | `1 LISTEN` + `1962 TIME-WAIT` |
| `60s` | `585 MB` | `21` | `1 LISTEN` |

关键现象：

- FD 一直稳定，传统“文件描述符没关”的泄露迹象不明显。
- TCP 最终只剩 `LISTEN`，说明连接关闭本身被内核回收了。
- RSS 不但没有回落，冷却后还继续上升。
- server 输出了 `MaxListenersExceededWarning`，提示同一个事件对象上累计了过多 listener。

这类现象更像“用户态订阅/队列/Effect fiber 没完整释放”，socket 未关闭的迹象不明显。

---

## 更极端复现：接近 2 GB 后崩溃

之前做过更长 SSE 压测：

```text
round 35: RSS 2071 MB
FD: 25
TCP: LISTEN + TIME-WAIT
```

停止请求后继续观察：

```text
after_sleep=0s   rss_mb=2039
after_sleep=30s  rss_mb=4922
process exited with code 134
```

同时没有观察到明显 `CLOSE_WAIT`，也没有发现内核 OOM 记录。这进一步支持一个判断：问题更接近运行时对象保留或后台 fiber/listener 生命周期问题，底层网络连接未关闭的可能性较低。

---

## 源码侧疑点

新版 server SSE handler 在：

```text
packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts
packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts
```

`event.ts` 的核心形态是：

```text
Queue.unbounded<EventV2.Payload>()
events.listen(...)
Stream.fromQueue(queue)
Effect.addFinalizer(unsubscribe)
```

这里有两个风险：

- `Queue.unbounded` 没有容量上限，生产速度超过消费速度时会积压。
- finalizer 只做 `unsubscribe`，没有明确 `Queue.shutdown(queue)`。

core 里其实已经有更安全的模式：

```text
packages/core/src/event.ts
allBounded(events, capacity)
  -> Queue.dropping(capacity)
  -> finalizer: unsubscribe + Queue.shutdown(queue)
```

修复方向应落在 server SSE handler：复用 bounded stream helper，或至少做到：

- 用 bounded/dropping/sliding queue 替代 unbounded queue。
- 连接断开时同时 `unsubscribe` 和 `Queue.shutdown`。
- 给 SSE stream 加 per-connection timeout / heartbeat / abort 绑定。
- 对 event listener 数量加观测指标，压测后必须回落到基线。

---

## TypeScript 在 code agent 上的性能上限

从这次数据看，TS/Bun 的上限取决于运行方式：

| 运行方式 | 特征 | 适合场景 |
|---|---|---|
| 源码 TS 入口 | 冷启动和 file syscall 高 | 本地开发、调试、快速迭代 |
| 打包二进制 | 文件 IO 低，启动更快 | CLI 分发、常驻 server、生产使用 |
| 长时间 agent runtime | 瓶颈转向内存状态和流生命周期 | code agent、IDE agent、MCP host |

对 code agent 来说，真正需要优化的是这些具体点：

- 不要在热路径频繁动态加载大量模块。
- 长连接、SSE、watcher、event bus 必须有明确生命周期。
- queue 默认不要 unbounded，除非能证明生产速率永远小于消费速率。
- tool output 要分层存储：内存里只保留摘要和索引，大输出落盘或外部 blob。
- session history 要有截断、压缩、分页和 lazy load。
- LLM streaming 要把网络背压传递到内部队列，避免无界缓存。
- 观测指标要覆盖 RSS、heap、FD、listener count、queue depth、event lag。

---

## 工程判断

这次实测可以把 opencode 的开销分成三层：

```text
启动期:
  源码 TS 入口重，打包后二进制明显改善。

普通请求期:
  /health 这类短 HTTP 请求表现正常，FD 不增长。

事件流运行期:
  SSE/event stream 在短连接反复打开/关闭时出现明显 RSS 增长，
  且冷却后没有恢复，是当前最值得修的点。
```

所以回答“TypeScript 的性能上限够不够 code agent 用”：

> 够，但前提是像写高性能服务一样写 TS：打包运行、控制队列、关闭订阅、传递背压、限制内存态，避免把运行时状态挂在无界 event/fiber/stream 上。

如果只看语言，容易误判；如果看 runtime 生命周期，问题会具体很多。
