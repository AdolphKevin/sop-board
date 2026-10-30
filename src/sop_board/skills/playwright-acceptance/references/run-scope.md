# 执行范围与原批次

开始执行前固定上游给出的项目、批次、场景原文 SHA-256 和本轮场景内容指纹；原文留在调用方指定的位置，快照和结果放在仓库外。由看板任务快照或 `sop-board playwright scope` 从原文生成真实指纹，不按标题或当前页面重新生成业务场景。

`scope.json` 结构如下（占位内容替换为本轮实际值）：

```json
{
  "project": "github.com/example/project",
  "batch": "requirement-v1",
  "scenario_file_sha256": "原始场景文件字节的 SHA-256",
  "scenarios": {
    "SOP-01-P01": "该场景的上游内容指纹",
    "SOP-01-N01": "该场景的上游内容指纹"
  }
}
```

新任务另有 `web_evidence` 对象，键为场景编号，每项为完整的 `{"video": true, "trace": true}` 布尔开关，取自该场景入口（示例值可由声明关闭）。旧 scope 省略该字段时两项均开启。手工 `scope` 命令用 `--entries <entries.json>` 传入入口声明；省略时保持旧默认。模板将同一 scope 写入报告，转换时核对录制开关一致。工作区运行中只改 `web_evidence`、`atlas` 或顶层 `fixtures` 不使本轮作废；本轮继续使用已冻结设置，下一轮采用新值。

运行时设置 `PW_SCOPE` 与 `PW_SCENARIO_FILE`。预检核对原文版本，夹具在报告中记录 `sop-scope` 注解，包括项目、批次、原文版本、场景编号和指纹。转换时使用同一份 `--scope` 和 `--scenario-file`，逐项核对注解；旧报告、不同批次、被替换的指纹或执行后变化的原文均不能混入本轮。它验证的是上游快照一致性，不替代上游对业务预期及产品版本的审核。

没有 scope 的旧流程仍可转换结果，但输出 `scope_verified: false`，不能声称已检查批次和内容版本。有 pending 时照常检查编号及完整性。没有 pending/scope 时只知道报告内范围，不知道原批次总数。

## 完整与部分执行

- 完整执行：提供 `--pending` 或 `--scope`，范围内每条场景须有结果，遗漏即转换失败。pending 与 scope 同时提供时，pending 决定本轮预期编号，scope 验证每条结果的批次和指纹。
- 部分试跑：显式提供 `--allow-partial`，并提供 pending/scope 说明总范围；转换器只输出已测结果，`coverage.json` 单列 `expected`、`executed`、`missing`。未执行场景不伪造 blocked、skip 或 pass。
- 技术定位、只读预探测或只完成某条场景的部分步骤不等于该场景通过。不要给这些子步骤换编号后把“子集 4/4”当作“原批次完成”。确需另开 smoke 批次时先明确用户认可的范围，并说明与原批次的关系。

`coverage.json` 仅是本轮转换的覆盖记录，不是看板结果契约，不把里面的额外字段加进 `results.jsonl`。

## 提前停止

业务失败由模板标为预期失败，本轮继续执行后续场景；受阻、连接断开等意外失败在首个出现时停止，不继续通过新 worker 建立调试连接。执行进程中途退出时，用逐条落盘的 report.partial.json 转换已完成的场景。保留原始报告，逐项核对实际执行记录；配置中有编号不表示它已执行。部分转换显式使用 `--allow-partial`，未产生实际结果的编号列入 coverage 的 missing，不伪造为通过、受阻或主动跳过。若报告包含框架自动生成的 skipped 记录，先区分其与明确声明的业务跳过；保留原文，在范围说明中写清 runner 提前停止，不声称执行覆盖完整。

## 提交后核对

调用方将结果交回输入的原工作区与批次。提交前再次核对场景/产品版本是否仍有效；提交后通过读取接口或页面核对实际场景编号、状态、轮次和已执行数。报告同时说明原批次总范围、本轮已执行范围和剩余未验证范围；不能用 Playwright 的绿色数量替代业务验收进度。实际实现者未知时明确独立性未声明，不凭作者配置猜测实现会话。

自动任务使用看板准备好的 scope.json，转换后交回任务目录的 results.jsonl；不要调用手工 submit。手工试跑可用 scope 子命令建立快照，再通过 submit 子命令登记到原批次。产品快照须包含 commit、digest（也接受 product_digest/product_fingerprint）及 dirty，准确性由调用方核对，不把 HEAD 当作运行态已验证的证明。

工作区版的兼容 project 字段保存实际工作区键，示例 github.com/example/project 在旧格式中为项目身份；新建 scope 使用 sop-board playwright scope --workspace <键>。旧场景中的 regression 字段不属于 v9 格式，不能直接推送到新工作区。
