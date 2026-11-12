---
title: "创建工作输出时避开已有目录，失败只清理本次新建的目录"
scope: ["self:src/sop_board/workdirs.py", "self:src/sop_board/runs.py", "self:src/sop_board/fixes.py", "self:src/sop_board/guards.py", "self:src/sop_board/authoring.py", "self:src/sop_board/launcher.py"]
source: ["self:docs/sop/browser.yaml#BRW-07-P02", "审核方独立验收缺陷 DEFECT-run-dir-deleted，2026-11-13"]
guards: ["self:docs/sop/browser.yaml#BRW-07-P02"]
verified: {"self": {"commit": "c0b61cfac0fee48a6493a02ee3658fb2e6d73d95", "date": "2026-11-13"}}
tags: ["数据丢失", "数据库重建", "运行目录", "prepare", "目录冲突"]
---
# 创建工作输出时避开已有目录，失败只清理本次新建的目录

现象：本机数据库重建后运行编号从头分配，runs/1 仍有旧证据；prepare 的 mkdir 报已存在，异常处理却无条件 rmtree，导致旧证据与结果被删除。

做法：原子创建目录；有文件、目录或符号链接占用时顺延名称，记录实际位置。只有创建成功并取得归属后才允许失败清理，绝不把已存在路径交给清理。提交与报告读取记录里的实际路径。修复、沉淀、编写、合并、启动记录与复测输出同样避让旧内容。

保护场景为 BRW-07-P02；场景引用不代表本次已独立复验，回归测试未建设。启动记录改为独立目录，日志位置保存在 launches.log，提示词与日志保持同一前缀。
