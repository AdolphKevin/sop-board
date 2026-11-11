---
title: "被测服务写进检出的生成文件要用 .gitignore 或 snapshot_exclude 排除在产品快照之外"
scope: ["self:src/sop_board/gitops.py", "self:src/sop_board/runs.py", "self:src/sop_board/guards.py", "self:examples/demo-shop/**"]
source: ["self:docs/design.md"]
guards: ["self:docs/sop/guard.yaml#GRD-01-P01"]
verified: {"self": {"commit": "51bd8882bd514d6263526c6ec4fcb4009816a976", "date": "2026-11-11"}}
tags: ["快照", "product_changed", "守护运行", "环境重启", "__pycache__", ".gitignore", "snapshot_exclude", "演示项目"]
---
# 被测服务写进检出的生成文件要用 .gitignore 或 snapshot_exclude 排除在产品快照之外

现象：2026-11-10 在演示项目上自检时，三条 API 场景经真实入口独立执行并通过，“待沉淀”却是 0。结果行的 product_changed 都是 `lib/refundlib/__pycache__/__init__.cpython-313.pyc`。

根因：演示商城从 lib 仓库导入 refundlib，Python 把字节码写进了 lib 检出。快照包含未跟踪文件（没被 .gitignore 排除的都算），执行前后快照不同，run submit 因此给结果记上“执行期间产品变化”。当时的规则下这样的通过不算独立通过。

现行影响（2026-11-11 起 product_changed 只作版本记录，不再影响独立性与状态）：生成文件仍会改变快照，使 `run prepare` 每次都重启环境，使守护运行因“运行期间产品内容发生变化”而不通过、场景转待复测，结果上的版本记录也混入噪声。

修复与防护：演示项目的 app、lib 都带 `.gitignore`，忽略 `__pycache__/` 与 `*.pyc`。守护命令设了 PYTHONDONTWRITEBYTECODE。真实项目里的构建产物、缓存或日志写进仓库而没被忽略时，也会出现同样的现象。

发现方法：环境每轮都重启、守护运行报“运行期间产品内容发生变化”，或结果行的 product_changed 列出与改动无关的路径时，看这些路径是否属于生成文件；是的，加进产品仓库的 .gitignore，或写进 config.yaml 的 snapshot_exclude。

适用条件：搭建新项目或演示项目、改动快照计算、排查环境反复重启或守护运行无故不通过时。
