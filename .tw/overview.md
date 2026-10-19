---
title: "sop-board 项目总览"
source: ["README.md"]
---
# sop-board 项目总览

SOP 验证协作看板。开发岗的代理用 praxis-skills 的 `sop-verify` 技能推送场景文件、证据与每轮验收结果；测试岗在网页上复核 Agent 判为通过的场景、上传测试用例、按场景提交缺陷；页面展示 Agent 验收通过率、人工复测通过率与测试进度。看板按轮次导出 team-workflow 能直接登记的结果文件。

任何成员 clone 后用 uv 在本机启动，默认只监听本机，团队共用时由启动者开启内网访问。暂不提供服务器部署。

场景文件与结果文件的字段与 team-workflow、sop-verify 同义，三个仓库各自实现，不共享代码。接口、存储与统计口径以 `docs/designs/` 中当前需求的方案为准。
