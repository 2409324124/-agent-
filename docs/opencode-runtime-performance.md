# opencode 运行期开销实测与 TypeScript 高性能开发笔记

日期：2026-07-08  
实测对象：`opencode 1.17.13` 二进制、`anomalyco/opencode` 本地源码树  
测试机器：Linux 6.17.0-35-generic，Ubuntu 24.04.3，x86_64，Bun 1.3.14，Node v22.19.0

这篇文档回答一个更工程化的问题：

> opencode 作为 TypeScript/Bun 写成的 code agent，真实运行时文件开销、网络 IO、内存占用和事件流吞吐大概能到什么量级？

结论先放前面：

- 单次 CLI 冷启动不是轻量脚本级别，简单命令约 `180 MB` RSS，`debug info` 约 `310 MB` RSS。
- 打包后的 opencode 二进制明显降低了源码 TS 入口的文件系统压力；同样跑 `debug info`，二进制约 `185` 次 file syscall，源码入口约 `8808` 次。
- 空闲 server 约 `292 MB` RSS；源码 TS 入口启动 server 约 `363 MB` RSS。
- 普通 HTTP 路径吞吐可接受：本机 `2000` 次 `/health` 请求约 `1.4s` 完成，RSS 增量约 `39 MB`。
- 运行时状态的高风险点不在“TypeScript 语言本身”，而在 SSE/event stream 这类长连接订阅生命周期。短连压测 `2000` 次 `/event` + `/global/event` 后，RSS 增量约 `217 MB`，冷却 `60s` 后仍未回落。
- 对 code agent 来说，性能上限通常被运行时状态、事件订阅、工具输出、会话历史、LLM streaming 和网络 provider 限制；TS/Bun 只是其中一层。

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

这类 debug 命令不是 LLM 请求，不代表真实 agent 调模型时的网络开销。真实 agent 路径会多出 provider streaming、重试、MCP/tool 网络调用和日志传输。

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

这个结果说明普通短 HTTP 请求不是主要问题。FD 没增长，连接关闭后进入内核 `TIME-WAIT`，用户态进程没有明显句柄泄露。

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

- FD 一直稳定，说明不是传统“文件描述符没关”的泄露。
- TCP 最终只剩 `LISTEN`，说明连接关闭本身被内核回收了。
- RSS 不但没有回落，冷却后还继续上升。
- server 输出了 `MaxListenersExceededWarning`，提示同一个事件对象上累计了过多 listener。

这类现象更像“用户态订阅/队列/Effect fiber 没完整释放”，不是 socket 没关。

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

同时没有观察到明显 `CLOSE_WAIT`，也没有发现内核 OOM 记录。这进一步支持一个判断：问题更接近运行时对象保留或后台 fiber/listener 生命周期问题，而不是底层网络连接没有 close。

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

所以修复方向不是“把 TS 改成别的语言”，而是让 server SSE handler 复用 bounded stream helper，或至少做到：

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

对 code agent 来说，真正需要优化的不是“语言是否是 TypeScript”这个抽象问题，而是这些具体点：

- 不要在热路径频繁动态加载大量模块。
- 长连接、SSE、watcher、event bus 必须有明确生命周期。
- queue 默认不要 unbounded，除非能证明生产速率永远小于消费速率。
- tool output 要分层存储：内存里只保留摘要和索引，大输出落盘或外部 blob。
- session history 要有截断、压缩、分页和 lazy load。
- LLM streaming 要把网络背压传递到内部队列，而不是无界缓存。
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

> 够，但前提是像写高性能服务一样写 TS：打包运行、控制队列、关闭订阅、传递背压、限制内存态，而不是把运行时状态都挂在无界 event/fiber/stream 上。

如果只看语言，容易误判；如果看 runtime 生命周期，问题会具体很多。
