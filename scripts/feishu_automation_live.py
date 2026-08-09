"""运行显式确认的 Phase 6 飞书 Automation 生产验收。"""

from miniclaw.evals.feishu_automation_live import run_feishu_automation_live_harness

if __name__ == "__main__":
    raise SystemExit(run_feishu_automation_live_harness())
