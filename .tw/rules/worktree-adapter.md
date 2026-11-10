---
title: "worktree 检出由 adapter.worktree/cleanup 契约建立和清理"
scope: ["self:src/sop_board/checkouts.py"]
source: ["self:docs/design.md"]
verified: {"self": {"commit": "ee879cd9757ae595df27aa351a8fee9b0910f20b", "date": "2026-11-10"}}
tags: ["worktree", "检出", "适配", "checkout create"]
---
# worktree 检出由 adapter.worktree/cleanup 契约建立和清理

`sop-board checkout create/remove` 是看板建立或清理 worktree 检出的唯一入口。项目声明 `adapter.worktree` 与 `cleanup`（须同时声明）时，用它们建立和清理；没有声明时，用 `git worktree add -b sop-board/<名称>` 建在 `<数据目录>/checkouts/<名称>/<别名>`，清理用 `git worktree remove`。

适配沿用既有变量名：`SOP_TASK_DIR`（检出目录）、`SOP_FIX_BRANCH`（分支 `sop-board/<名称>`）、`SOP_REPOS`（逗号分隔的别名）、`SOP_BASE_<别名>`（登记仓库路径），可选 `SOP_BASE_COMMITS`；清理另有 `SOP_WORKTREES`。worktree 的最后一行输出须为 `{"repos": {别名: 路径}, "env": {变量名: 文字值}}`，覆盖全部请求的仓库；env 不能使用 `SOP_` 开头的变量，随检出保存，供该检出的环境使用。

返回路径须同时满足三项核对：是 Git 工作树根目录；与登记仓库共用 git common dir；检出请求的分支（并出现在 `git worktree list` 中）。清理只交出已核对的路径；清理后目录仍在即报错，保留现场。分支不随清理删除，开发者的提交留在分支上。

用户自己建立的 worktree 不经过适配：命令在其中运行时按 git common dir 自动识别为检出。

## 依据
v10 设计“检出与环境”一节（使用者 2026-11-10 批准）。保留 cw2 已有的 worktree.py，所以契约变量名不变。
