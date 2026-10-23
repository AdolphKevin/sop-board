---
title: "工作树建立和清理由同一适配契约管理"
scope: ["self:src/sop_board/**"]
source: ["self:docs/proposals/2026-10-20-v9-workspace.md", "self:docs/designs/SOP-BOARD-7.md", "self:docs/proposals/2026-10-22-integrated-rework.md"]
verified: {"self": {"commit": "8bfb61e7636c227a58270c888eb5e5044ede7899", "date": "2026-10-20"}}
guards: ["self:docs/sop/sop-board-7.yaml#SOP-71-P01", "self:docs/sop/sop-board-7.yaml#SOP-74-P01"]
---
# 工作树建立和清理由同一适配契约管理

看板修复、看板沉淀和自检共用 worktree/cleanup 适配入口；对应命令未声明时才使用默认 Git 建立或删除方式，不再支持旧准备命令。返回路径须为对应仓库的工作树根目录、git common dir 与基线相同且当前分支等于请求分支。清理只处理已核对路径，不接管错误输出所指的其他目录。

修复会话和复测使用已核对的实际路径及返回的环境，不假定工作树位于固定子目录。合入后先删除工作树并核对目录及 Git 登记消失，再删除已合入分支；失败保留现场。返回环境只保存在进程内，任务下载不公开工作树或适配生成的本机配置。

搭建指引先要求与开发者确认建法、清理方法和所需本机文件，再编写适配；报告写明外部工具的来源以及团队成员共用前须具备的依赖。代码和指引不内置具体项目的搭建知识。

## 依据
SOP-BOARD-7 修订 3 的 D33、D34、I3、I5，以及 SOP-71-P01 和 SOP-74-P01。

整体方案 D77、D80、D81 补充：看板沉淀按 SOP_BASE_COMMITS 的固定提交建立或复用工作树，清理后保留开发者尚未合入的任务分支；待验证不自动发起执行。开发会话修复直接在准备时核对的当前产品目录中进行，不建立或清理工作树、不自行提交；不能把看板修复的适配与合入步骤套到会话入口。
