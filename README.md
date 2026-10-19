# sop-board

SOP 验证协作看板。开发岗的代理用 [praxis-skills](https://github.com/AdolphKevin/praxis-skills) 的 `sop-verify` 技能，把业务 SOP 的验收场景与每轮验收结果推送到看板；测试岗在同一个网页上传测试用例、查看进度、复核 Agent 判为通过的场景，并按场景提交缺陷。看板展示 Agent 验收通过率、人工复测通过率与测试进度。

状态：开发中。

## 使用方式

任何成员 clone 本仓后用 uv 在本机启动看板；团队共用一份数据时，由启动者开启内网访问，其他成员用浏览器打开。分析测试用例时，看板调用启动者本机已登录的 Codex。暂不提供服务器部署。

## 相关仓库

- [praxis-skills](https://github.com/AdolphKevin/praxis-skills)：`sop-verify` 技能，从业务 SOP 写场景、派独立会话执行、登记结果。
- [team-workflow](https://github.com/AdolphKevin/team-workflow)：团队流程；看板按轮次导出的结果可直接用 `tw team scenarios submit` 登记。
