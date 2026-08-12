-- depth-1 子 Agent 的父子 Run 关联。
-- 不新建表：子 Run 的生命周期、lease 与重启恢复与普通 Run 完全一致，
-- 复制一张表只会让恢复逻辑分叉。
--
-- 深度不存列，由 parent_run_id IS NOT NULL 推导——存一个可以被写错的深度
-- 字段，不如让非法状态无法表达。
ALTER TABLE task_runs ADD COLUMN parent_run_id INTEGER REFERENCES task_runs(id);
ALTER TABLE task_runs ADD COLUMN subagent_id TEXT;

CREATE INDEX task_runs_parent_idx ON task_runs(parent_run_id, id);
