#!/usr/bin/env python3
"""运行 Phase 6 当前 Mac + 飞书 production preflight/recovery/soak。"""

from lobster0.evals.phase6_production import run_phase6_production_gate

if __name__ == "__main__":
    raise SystemExit(run_phase6_production_gate())
