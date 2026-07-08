#!/usr/bin/env python3
"""Benchmark model-driven opencode agent turns.

This drives `opencode run --format json`, not the HTTP shell endpoint.  Each
sample includes a real model request, tool selection, tool execution, session
storage writes, and on-disk artifact verification.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def read_proc_io(pid: int) -> dict[str, int] | None:
    try:
        values: dict[str, int] = {}
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip())
        return values
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def read_rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    return 0


def children_of(root: int) -> set[int]:
    pids = {root}
    changed = True
    while changed:
        changed = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in pids:
                continue
            try:
                stat = (entry / "stat").read_text()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                continue
            if ppid in pids:
                pids.add(pid)
                changed = True
    return pids


def read_net_dev() -> dict[str, dict[str, int]]:
    data: dict[str, dict[str, int]] = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        iface, rest = line.split(":", 1)
        fields = rest.split()
        data[iface.strip()] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
    return data


def net_delta(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> dict[str, int]:
    total = {"lo_rx_bytes": 0, "lo_tx_bytes": 0, "non_lo_rx_bytes": 0, "non_lo_tx_bytes": 0}
    for iface, values in after.items():
        prev = before.get(iface, values)
        rx = max(0, values["rx_bytes"] - prev["rx_bytes"])
        tx = max(0, values["tx_bytes"] - prev["tx_bytes"])
        if iface == "lo":
            total["lo_rx_bytes"] += rx
            total["lo_tx_bytes"] += tx
        else:
            total["non_lo_rx_bytes"] += rx
            total["non_lo_tx_bytes"] += tx
    return total


class Sampler:
    def __init__(self) -> None:
        self.roots: set[int] = set()
        self.first_io: dict[int, dict[str, int]] = {}
        self.last_io: dict[int, dict[str, int]] = {}
        self.max_rss_kb = 0
        self.max_pid_count = 0

    def add_root(self, pid: int) -> None:
        self.roots.add(pid)

    def sample(self) -> None:
        live: set[int] = set()
        for root in list(self.roots):
            live.update(children_of(root))
        self.max_pid_count = max(self.max_pid_count, len(live))
        rss = 0
        for pid in live:
            rss += read_rss_kb(pid)
            io = read_proc_io(pid)
            if io is None:
                continue
            self.first_io.setdefault(pid, io)
            self.last_io[pid] = io
        self.max_rss_kb = max(self.max_rss_kb, rss)

    def io_delta(self) -> dict[str, int]:
        keys = ["rchar", "wchar", "read_bytes", "write_bytes", "cancelled_write_bytes"]
        totals = {key: 0 for key in keys}
        for pid, last in self.last_io.items():
            first = self.first_io.get(pid, {})
            for key in keys:
                totals[key] += max(0, last.get(key, 0) - first.get(key, 0))
        return totals


@dataclass
class Trial:
    index: int
    ok: bool
    duration_s: float
    returncode: int | None
    session_id: str | None
    stdout_path: str
    stderr_path: str
    tool_counts: dict[str, int]
    subagent_session_ids: list[str]
    file_ok: bool
    error: str | None


def prompt_for(workdir: Path, mode: str, index: int, subagents: int) -> str:
    if mode == "single":
        target = workdir / "bench-out" / f"turn-{index}.txt"
        return (
            f"Use the bash tool to create {target} containing exactly ok-{index}. "
            f"Then cat {target} to verify. Keep the final answer short."
        )
    tasks = []
    for n in range(subagents):
        target = workdir / "bench-out" / f"turn-{index}-subagent-{n}.txt"
        tasks.append(
            f"subagent {n}: create {target} containing exactly ok-{index}-{n}, "
            "then verify with cat"
        )
    return (
        f"Use the task tool to launch exactly {subagents} implementation-agent subagents in parallel. "
        + "; ".join(tasks)
        + ". After all subagents finish, use bash in the parent session to cat every generated file. "
        + "Keep the final answer short."
    )


def command_for(args: argparse.Namespace, prompt: str) -> list[str]:
    common = [
        "run",
        "-m",
        args.model,
        "--agent",
        args.agent,
        "--dir",
        args.workdir,
        "--format",
        "json",
        prompt,
    ]
    if args.profile == "binary":
        common.insert(1, "--auto")
        inner = ["opencode", *common]
    elif args.profile == "source":
        common.insert(1, "--dangerously-skip-permissions")
        inner = [
            "bun",
            "run",
            "--cwd",
            args.source_pkg,
            "--conditions=browser",
            "./src/index.ts",
            *common,
        ]
    else:
        raise ValueError(args.profile)
    return ["prlimit", f"--as={args.address_space_bytes}", "--", *inner]


def parse_stdout(path: Path) -> tuple[str | None, dict[str, int], list[str], str | None]:
    session_id: str | None = None
    tool_counts: dict[str, int] = {}
    subagent_ids: list[str] = []
    error: str | None = None
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("sessionID") or session_id
        if event.get("type") == "tool_use":
            part = event.get("part", {})
            tool = part.get("tool", "unknown")
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            metadata = part.get("state", {}).get("metadata", {})
            sub_session = metadata.get("sessionId")
            if sub_session:
                subagent_ids.append(sub_session)
        if event.get("type") == "error":
            error = json.dumps(event, ensure_ascii=True)
    return session_id, tool_counts, subagent_ids, error


def verify_files(workdir: Path, mode: str, index: int, subagents: int) -> bool:
    if mode == "single":
        path = workdir / "bench-out" / f"turn-{index}.txt"
        return path.exists() and path.read_text().strip() == f"ok-{index}"
    for n in range(subagents):
        path = workdir / "bench-out" / f"turn-{index}-subagent-{n}.txt"
        if not path.exists() or path.read_text().strip() != f"ok-{index}-{n}":
            return False
    return True


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    idx = min(len(sorted_values) - 1, round((len(sorted_values) - 1) * p))
    return sorted_values[idx]


def run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    workdir = Path(args.workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (workdir / "bench-out").mkdir(parents=True, exist_ok=True)

    sampler = Sampler()
    active: dict[subprocess.Popen[bytes], tuple[int, float, Path, Path]] = {}
    trials: list[Trial] = []
    net_before = read_net_dev()
    started = time.monotonic()
    next_index = 0

    while next_index < args.turns or active:
        while next_index < args.turns and len(active) < args.concurrency:
            prompt = prompt_for(workdir, args.mode, next_index, args.subagents)
            stdout_path = out_dir / f"turn-{next_index:03d}.stdout.jsonl"
            stderr_path = out_dir / f"turn-{next_index:03d}.stderr.log"
            stdout_f = stdout_path.open("wb")
            stderr_f = stderr_path.open("wb")
            proc = subprocess.Popen(
                command_for(args, prompt),
                cwd=workdir,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
            stdout_f.close()
            stderr_f.close()
            sampler.add_root(proc.pid)
            active[proc] = (next_index, time.monotonic(), stdout_path, stderr_path)
            next_index += 1

        sampler.sample()
        for proc, meta in list(active.items()):
            index, start, stdout_path, stderr_path = meta
            if proc.poll() is None:
                if time.monotonic() - start > args.timeout:
                    proc.kill()
                else:
                    continue
            proc.wait()
            session_id, tool_counts, subagent_ids, parse_error = parse_stdout(stdout_path)
            file_ok = verify_files(workdir, args.mode, index, args.subagents)
            expected_tasks = args.subagents if args.mode == "subagents" else 0
            error = parse_error
            if proc.returncode != 0:
                err_tail = stderr_path.read_text(errors="replace")[-1000:]
                error = f"returncode={proc.returncode}; stderr_tail={err_tail}"
            if args.mode == "single" and tool_counts.get("bash", 0) < 1:
                error = error or "missing bash tool call"
            if args.mode == "subagents" and tool_counts.get("task", 0) < expected_tasks:
                error = error or f"expected {expected_tasks} task calls, got {tool_counts.get('task', 0)}"
            ok = proc.returncode == 0 and file_ok and error is None
            trials.append(
                Trial(
                    index=index,
                    ok=ok,
                    duration_s=time.monotonic() - start,
                    returncode=proc.returncode,
                    session_id=session_id,
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                    tool_counts=tool_counts,
                    subagent_session_ids=subagent_ids,
                    file_ok=file_ok,
                    error=error,
                )
            )
            del active[proc]
        time.sleep(args.sample_interval)

    sampler.sample()
    elapsed = time.monotonic() - started
    durations = [trial.duration_s for trial in trials]
    ok_count = sum(1 for trial in trials if trial.ok)
    total_tool_counts: dict[str, int] = {}
    for trial in trials:
        for tool, count in trial.tool_counts.items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
    summary = {
        "profile": args.profile,
        "mode": args.mode,
        "model": args.model,
        "agent": args.agent,
        "turns": args.turns,
        "concurrency": args.concurrency,
        "subagents_per_turn": args.subagents if args.mode == "subagents" else 0,
        "address_space_bytes": args.address_space_bytes,
        "ok": ok_count,
        "failed": len(trials) - ok_count,
        "elapsed_s": elapsed,
        "agent_turn_rps": ok_count / elapsed if elapsed > 0 else 0,
        "duration_p50_s": statistics.median(durations) if durations else None,
        "duration_p95_s": percentile(durations, 0.95),
        "duration_p99_s": percentile(durations, 0.99),
        "max_rss_mb": sampler.max_rss_kb / 1024,
        "max_pid_count": sampler.max_pid_count,
        "proc_io_delta": sampler.io_delta(),
        "net_delta": net_delta(net_before, read_net_dev()),
        "tool_counts": total_tool_counts,
        "trials": [trial.__dict__ for trial in sorted(trials, key=lambda item: item.index)],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=True))
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["binary", "source"], default="binary")
    parser.add_argument("--mode", choices=["single", "subagents"], default="single")
    parser.add_argument("--model", default="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    parser.add_argument("--agent", default="primary-controller")
    parser.add_argument("--source-pkg", default="/srv/storage/projects/opencode-anomaly/packages/opencode")
    parser.add_argument("--workdir", default="/tmp/opencode-model-turn-bench")
    parser.add_argument("--out-dir", default="/tmp/opencode-model-turn-bench/results")
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--subagents", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--address-space-bytes", type=int, default=10 * 1024 * 1024 * 1024)
    args = parser.parse_args()
    summary = run(args)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
