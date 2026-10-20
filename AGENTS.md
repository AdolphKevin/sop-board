# sop-board 开发约定

## 当前入口

以 README.md、docs/verification.md、docs/playwright-acceptance.md 和 .tw/ 中仍适用的项目事实为依据。team-workflow 与 team-knowledge 已不再作为本仓开发或验收的前置工具；不运行 tw 命令。保留历史 .tw 数据及旧契约的读取兼容，不删除历史记录。

Web 验收技能、模板、转换器和测试统一维护在 src/sop_board/skills/playwright-acceptance/，由 sop-board 包发行；不要依赖另一个 playwright-acceptance 检出目录。命令入口为 sop-board guide 与 sop-board playwright。

## 完成标准

- 预期来自已确认的业务要求、SOP 或接口约定，不把当前实现输出当作正确答案。
- 产品业务验收经真实入口执行并留证；实现者自检不冒充独立验收。
- 不删除断言、放宽预期、跳过场景或改成当前输出来通过验收。
- 如实区分通过、失败、受阻、跳过、未执行和回归未建设。
- 结果返回原项目与批次，保持场景编号、内容指纹与产品快照；不把子集通过写成整批完成。
- 按改动运行相应测试，知识更新与代码属于同一逻辑改动。过程报告与浏览器证据放在仓库外。

## 本机验证

Python：uv run --frozen pytest -q -p no:cacheprovider tests。
内置技能 Python：python3 -m unittest discover -s src/sop_board/skills/playwright-acceptance/tests。
内置技能 Node：node --test src/sop_board/skills/playwright-acceptance/tests/test_runtime.mjs。
worker 复用集成测试另需已安装 @playwright/test >=1.64，可通过 PW_TEST_PACKAGE 指定包目录。

## 安全边界

- 真实账号登录、人机验证和二次验证由用户在真实浏览器完成，遇到 browser may not be secure 停止。
- 仅在已确认的测试环境操作，合成账号登录沿用本次授权范围；不自行扩大权限。
- 不复用动态授权链接，不依赖常开的浏览器标签页续期服务。
- 凭证只经环境变量传递，不写入文档、任务说明或证据。
- 不自行清理用户数据；清理仅限用户授权范围。
