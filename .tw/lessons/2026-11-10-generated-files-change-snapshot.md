---
title: "被测服务往检出里写的生成文件会改变产品快照，通过随之不算独立通过"
scope: ["self:src/sop_board/gitops.py", "self:src/sop_board/runs.py", "self:examples/demo-shop/**"]
source: ["self:docs/design.md"]
guards: ["self:docs/sop/guard.yaml#GRD-01-P01"]
verified: {"self": {"commit": "8893f89e0c95ba99295cc30bba5a19e37e37cd13", "date": "2026-11-10"}}
tags: ["快照", "product_changed", "待沉淀", "__pycache__", ".gitignore", "演示项目"]
---
# 被测服务往检出里写的生成文件会改变产品快照，通过随之不算独立通过

现象：2026-11-10 在演示项目上自检时，三条 API 场景经真实入口独立执行并通过，“待沉淀”却是 0。结果行的 product_changed 都是 `lib/refundlib/__pycache__/__init__.cpython-313.pyc`。

根因：演示商城从 lib 仓库导入 refundlib，Python 把字节码写进了 lib 检出。快照包含未跟踪文件（没被 .gitignore 排除的都算），执行前后快照不同，run submit 因此给结果记上“执行期间产品变化”。这样的通过不算独立通过，不进入待沉淀；产品一变还会落成待复测。

修复与防护：演示项目的 app、lib 都带 `.gitignore`，忽略 `__pycache__/` 与 `*.pyc`。守护命令本来就设了 PYTHONDONTWRITEBYTECODE。真实项目里的构建产物、缓存或日志写进仓库而没被忽略时，也会出现同样的现象。

发现方法：通过却不进待沉淀、或无故变成待复测时，先看结果行的 product_changed 列出了哪些路径；属于生成文件的，把它们加进产品仓库的 .gitignore，或写进 config.yaml 的 snapshot_exclude。

适用条件：搭建新项目或演示项目、改动快照计算、排查“待沉淀”或“待复测”与预期不符时。
