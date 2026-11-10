# 执行范围

`sop-board run prepare` 写出 `scope.json`，绑定本轮的检出、场景原文与各场景指纹。执行与转换使用同一份，不按标题或当前页面重新生成业务场景。

```json
{
  "project": "<项目名>",
  "checkout": "<检出名>",
  "scenario_file_sha256": "scenarios.yaml 字节的 SHA-256",
  "scenarios": {"shop#W": "该场景的内容指纹"},
  "web_evidence": {"shop#W": {"video": true, "trace": true}}
}
```

- `scenarios` 的键是 `<文件>#<编号>`；`web_evidence` 对每个 Web 场景给出完整的 `video`、`trace` 布尔开关，取自入口声明。关闭项不启动、不保存。
- 运行时 `PW_SCOPE` 与 `PW_SCENARIO_FILE` 由 run.env 提供。`global-setup.mjs` 与夹具核对原文摘要，并在报告里写 `sop-scope` 注解（项目、检出、原文摘要、场景 key、指纹）。`convert` 用同一份 `--scope`、`--scenario-file` 逐项核对；旧报告、被替换的指纹与执行后变化的原文都不能混入本轮。
- 快照一致性不替代上游对业务预期与产品版本的审核。

## 完整与部分执行

- 完整执行：范围内每个场景都要有结果，遗漏即转换失败。
- 部分执行：显式加 `--allow-partial`。转换器只输出已测结果，`coverage.json` 列出 `expected`、`executed`、`missing`；未执行的场景写成 `not-executed` 行，不伪造 blocked、skip 或 pass。
- 技术定位、只读探测或只完成部分步骤不等于场景通过；不为这些子步骤另起编号，也不把"子集通过"写成整轮完成。
- `coverage.json` 只是本轮转换的覆盖记录，不是结果契约，其字段不进入 `results.jsonl`。

## 提前停止

业务失败由夹具标为预期失败，本轮继续。受阻、连接断开、页面或浏览器崩溃在首个出现时停止（`maxFailures: 1`），不让新 worker 重连。进程中途退出时，用逐条落盘的 `report.partial.json` 转换已完成的场景，并核对实际执行编号。报告里框架自动生成的 skipped 记录，先与明确声明的业务跳过区分。

## 提交后核对

`run submit` 输出列出登记、作废（运行期间场景内容变了）与未执行的场景。向调用方报告本轮总范围、已执行范围与剩余未验证范围，不以 Playwright 的绿色数量代替业务验收进度。实现者未知时独立性为未声明，不凭作者配置猜测。
