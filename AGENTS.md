# Flow Board 开发约定

## 当前入口

开发、验收、缺陷复验与质量复盘先读 [docs/standard.md](docs/standard.md)，它是验收标准的唯一正文。搭建、项目操作与 Web 驱动分别读取 README.md、docs/verification.md、docs/playwright-acceptance.md，并直接读取 .tw/ 中仍适用的项目事实；不运行 tw 命令。保留历史 .tw 数据及旧契约的读取兼容。

Web 验收技能、模板、转换器和测试统一维护在 src/sop_board/skills/playwright-acceptance/，由 sop-board 包发行；不要依赖另一个 playwright-acceptance 检出目录。命令入口为 sop-board guide 与 sop-board playwright。

回归沉淀技能维护在 src/sop_board/skills/regression-distill/，随包发行；sop-board skills install 安装两个客户端的技能链接，sop-board distill submit 在本机登记关联。产品侧另行授权的测试建设按该技能执行，场景执行与修复会话仍不写测试；本仓开发继续遵守下文“不新增测试”。关联不改变验收结论，数据库格式保持 5。入口凭据只通过 credential_env 声明名称并从启动进程传入会话，内网不显示名称，任何材料不写取值。

## 完成标准

- 预期来自已确认的业务要求、SOP 或接口约定，不把当前实现输出当作正确答案。
- 产品业务验收经真实入口执行并留证；实现者自检不冒充独立验收。
- 不删除断言、放宽预期、跳过场景或改成当前输出来通过验收。
- 如实区分通过、失败、受阻、跳过、未执行和回归未建设。
- 结果返回原项目与批次，保持场景编号、内容指纹与产品快照；不把子集通过写成整批完成。
- 按改动运行相应测试，知识更新与代码属于同一逻辑改动。过程报告与浏览器证据放在仓库外。

## 本机验证

遵守 main 的 v9 决策：验收以 docs/sop/ 场景经真实入口执行为准，不新增单元、接口或集成测试，不恢复上游已删除的根 tests/。现有内置技能资源保持兼容。工作区 config.yaml 是入口唯一配置，sop-board guide 保留完整搭建指引；具体 Web 驱动指引用 sop-board playwright guide。普通写入无需令牌，开发者操作仅限本机连接，不能恢复旧项目 API 和令牌逻辑。

## 安全边界

- 真实账号登录、人机验证和二次验证由用户在真实浏览器完成，遇到 browser may not be secure 停止。
- 仅在已确认的测试环境操作，合成账号登录沿用本次授权范围；不自行扩大权限。
- 不复用动态授权链接，不依赖常开的浏览器标签页续期服务。
- 凭证只经环境变量传递，不写入文档、任务说明或证据。
- 不自行清理用户数据；清理仅限用户授权范围。
