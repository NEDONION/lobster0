#!/usr/bin/env python3
"""Discord 人工 live 验收入口；本脚本绝不主动发送消息。"""

from lobster0.evals.live import run_live_harness

if __name__ == "__main__":
    raise SystemExit(run_live_harness("discord"))
