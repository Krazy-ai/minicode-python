"""MiniCode 的定时任务运行器。

读取 JSON 配置文件，按顺序跑多个 headless 任务。被 ``minicode-cron`` 入口调用。

配置格式：

    {
      "tasks": [
        {"name": "daily-check", "prompt": "Summarize the repository status"}
      ]
    }

未提供配置文件时，运行器会打印一条说明信息后干净退出（避免误报）。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def _default_config_path() -> Path:
    """返回默认配置路径，可被 MINI_CODE_CRON_CONFIG 环境变量覆盖。"""
    return Path(os.environ.get("MINI_CODE_CRON_CONFIG", ".mini-code/cron.json"))


def load_cron_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载并校验 cron 配置文件。

    返回的字典保证含有 ``tasks`` 键（list）。文件不存在时返回 ``{"tasks": []}``。
    格式不对时抛 ``ValueError``。
    """
    config_path = Path(path) if path is not None else _default_config_path()
    if not config_path.exists():
        return {"tasks": []}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cron config must be a JSON object")
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("cron config field 'tasks' must be a list")
    return {"tasks": tasks}


def run_configured_tasks(config: dict[str, Any], *, dry_run: bool = False) -> list[dict[str, Any]]:
    """按 config 顺序跑所有任务，返回每个任务的执行结果。

    Args:
        config: load_cron_config 返回的字典
        dry_run: 仅校验配置不真正执行 prompt

    Returns:
        每条结果是 ``{"name": str, "ok": bool, ...}`` 形式的字典；若 ok=False
        则附带 ``error`` 字段；ok=True 且非 dry_run 时附带 ``response`` 字段。
    """
    from minicode.headless import run_headless

    results: list[dict[str, Any]] = []
    for index, task in enumerate(config.get("tasks", [])):
        if not isinstance(task, dict):
            results.append({"index": index, "ok": False, "error": "task must be an object"})
            continue
        prompt = str(task.get("prompt", "")).strip()
        name = str(task.get("name") or f"task-{index + 1}")
        if not prompt:
            results.append({"name": name, "ok": False, "error": "prompt is required"})
            continue
        if dry_run:
            results.append({"name": name, "ok": True, "dryRun": True})
            continue
        results.append({"name": name, "ok": True, "response": run_headless(prompt)})
    return results


def main(argv: list[str] | None = None) -> None:
    """cron 运行器的 CLI 入口。

    - 未传 ``--once`` 时进入轮询循环，每隔 ``--interval`` 秒执行一遍。
    - 配置中没有任务时直接打印提示并退出（不会无限空转）。
    """
    parser = argparse.ArgumentParser(description="Run MiniCode scheduled headless tasks.")
    parser.add_argument("--config", default=None, help="Path to cron JSON config.")
    parser.add_argument("--once", action="store_true", help="Run tasks once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Validate tasks without executing prompts.")
    parser.add_argument("--interval", type=float, default=60.0, help="Polling interval in seconds.")
    args = parser.parse_args(argv)

    while True:
        config = load_cron_config(args.config)
        if not config["tasks"]:
            print(f"No cron tasks configured in {args.config or _default_config_path()}.", flush=True)
        else:
            for result in run_configured_tasks(config, dry_run=args.dry_run):
                print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once or not config["tasks"]:
            return
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    main()
