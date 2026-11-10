---
title: "复制 git worktree 目录做测试会把提交写进真实分支"
scope: ["self:docs/verification.md", "self:src/sop_board/checkouts.py"]
tags: ["worktree", "临时副本", "验收", "自检"]
source: ["https://github.com/AdolphKevin/sop-board/commit/b95ba6f3863422d200a7cec0784b75eba78ccda4"]
verified: {"self": {"commit": "ee879cd9757ae595df27aa351a8fee9b0910f20b", "date": "2026-11-10"}}
---
# 复制 git worktree 目录做测试会把提交写进真实分支

现象：为验证安装缓存（SOP-104），用 rsync 把 worktree 复制到临时目录后在里面提交，三次测试提交（作者为测试身份）出现在真实功能分支上，并随推送进入 main，事后只能改写 main 历史。

根因：worktree 根目录的 `.git` 是指向主仓库 `worktrees/<名>` 的文件，复制后仍指向同一仓库与分支；在复制目录里提交等于在原分支提交。

防护：需要可随意提交的副本时用 `git clone <检出> <临时目录>`（或 `git worktree add` 新分支），不要复制 worktree 目录；推送前用 `git log <基线>..HEAD --format='%h %an %s'` 核对作者和提交是否都是预期的。

适用条件：任何在临时副本里提交、改动或重装的自检与验收。
