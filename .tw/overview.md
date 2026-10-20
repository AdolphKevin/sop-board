---
title: "sop-board 项目总览"
source: ["README.md"]
---
# sop-board 项目总览

SOP 验证协作看板。开发岗的代理用 praxis-skills 的 `sop-verify` 技能推送场景文件、证据与每轮验收结果；测试岗在网页上复核 Agent 判为通过的场景、上传测试用例、按场景提交缺陷；页面展示 Agent 验收通过率、人工复测通过率与测试进度。看板按轮次导出 team-workflow 能直接登记的结果文件。

任何成员 clone 后用 uv 在本机启动，默认只监听本机，团队共用时由启动者开启内网访问。暂不提供服务器部署。

场景文件与结果文件的字段与 team-workflow、sop-verify 同义，三个仓库各自实现，不共享代码。接口、存储与统计口径以 `docs/designs/` 中当前需求的方案为准。

当前访问规则见 `docs/designs/SOP-BOARD-7.md` I1 与 `docs/proposals/2026-10-20-v9-workspace.md` D31：页面仅保存标记人，普通写入免令牌；开发者写操作只接受实际连接来自回环地址，非回环页面不生成开发者按钮。POST、PUT 校验同源与 JSON 类型，二进制证据上传仅免 JSON 类型要求。`--writable-repo` 在 T1 保留，后续由 T2 改为工作区配置。

验收以 `docs/sop/` 的场景经最终交付产物的真实入口执行为准。按 AdolphKevin 对 SOP-BOARD-7 T1 的第二次更正，删除仓库现有测试与仅供测试使用的依赖、配置和辅助脚本，开发中不写单元、接口与集成测试。命令、数据核对和留证方式见 `docs/verification.md`；本次保持 `.tw/config.json` 原样。
