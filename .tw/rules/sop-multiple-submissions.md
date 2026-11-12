---
title: "一次 SOP 编写工作逐份登记文件，并累计更新同一条业务记录"
scope: ["self:src/sop_board/authoring.py", "self:src/sop_board/works.py", "self:src/sop_board/prompts/author-sop.md", "self:src/sop_board/skills/sop-board/references/author-sop.md"]
source: ["self:docs/reference.md", "self:docs/sop/authoring.yaml", "统一验收缺陷 F1 第 2 轮修复（2026-11-13），修法由审核方确定"]
verified: {"self": {"commit": "60d1c86a0ad464e07ad86ad10898b99006e0b416", "date": "2026-11-13"}}
guards: ["self:docs/sop/authoring.yaml#AUT-01-P01", "self:docs/sop/authoring.yaml#AUT-01-N01"]
tags: ["sop submit", "多份 SOP", "业务交叉", "work-not-found", "work-state-invalid", "sources.json"]
---
# 一次 SOP 编写工作逐份登记文件，并累计更新同一条业务记录

新 SOP 与随之改正的已有 SOP 共用一次 prepare 的资料清单，分别校验并在文末追加确认记录。首次提交后工作仍标为 submitted，后续提交允许 prepared 和 submitted；abandoned 拒绝。不带编号时取本检出最近一次 SOP 编写工作，有新资料时重新 prepare。

本机 local_work.result 与业务记录库 work.result 都用 files 清单记录文件名、确认人和时间；继续提交更新同一条业务记录，不新增行。SQLite 与 PostgreSQL 使用同一套 SQL。

## 依据

AUT-01-P01 要求新文件与业务交叉导致修改的已有文件分别提交。原实现首次提交后结束工作，随后只查 prepared，导致第二份没有确认记录；只放开工作状态仍会被 works.publish 的重复登记处理丢弃后续结果，故须同时累计并更新结果。
