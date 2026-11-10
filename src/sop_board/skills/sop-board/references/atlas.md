# 梳理交互（交互地图）

适用于声明了 `entries.<入口>.atlas` 的 Web 入口。地图只记录“怎么操作”，不提供业务预期；规则与输出格式见 `docs/reference.md` 的“交互地图”。

1. 运行 `sop-board atlas show --json`，查看当前版本、过期页面和待处理反馈。

2. 选模式：

   | 情况 | 模式 |
   | --- | --- |
   | 还没有地图，或前端大改 | 全量 |
   | 有过期页面 | `--stale` |
   | 只需修正个别页面或处理反馈 | `--pages <编号,…>` |

   运行 `sop-board atlas prepare --entry <入口> --session <标识> [模式]`。需要只读浏览器核对时加 `--browser`；核对期间不要另行开始执行。

3. 按任务目录的 `prompt.md` 只读前端源码，写出 `final.json`：
   - 不改任何文件，不运行产品命令；
   - 不写可见性、权限、校验、默认值、业务结果，这些属于场景；
   - 页面用到的多语言文件也写进页面的 `sources`；
   - 逐条处理 `feedback.json`，写进 `feedback_handled`。

4. 运行 `sop-board atlas submit --task <编号> --session <标识>`。不合格时按问题逐条修正后重交；登记成功后报告版本、页面数和已处理的反馈。

执行会话发现地图缺失或有误时，不改地图，只在本任务目录写 `atlas-feedback.jsonl`，每行一个 JSON 对象：

```json
{"page": "<页面编号>", "kind": "missing|locator|name|reach|other", "detail": "<实际所见>", "scenario": "<场景编号>", "control": "<可选>", "evidence": "<可选，任务目录内的证据路径>"}
```

这些反馈随执行结果一并收下，在下一次梳理时处理。

`kind` 按实际失败阶段与观察选择：

- `reach`：按地图的导航或点击步骤未能到达目标页面，途中控件缺失或不能唯一匹配也属此类。
- `missing`：目标页面已到达，等待后目标控件仍不存在。
- `locator`：检查目标控件时定位声明无效、匹配数量不符或不能唯一匹配；零匹配选 `missing`。
- `name`：已识别控件的实际名称或多语言文案与地图不符；不能凭零匹配推断名称有误。
- `other`：以上之外的问题，如控件存在但不可见、请求声明不足、证据保存或清理失败。

依据执行阶段和实际匹配数分类，不依据错误文字中的关键词。

## 对照界面

1. 地图就绪后运行 `sop-board atlas bind <批次> --session <标识>`。场景或地图有变化时加 `--stale`，只对照变化的部分。

2. 按 `prompt.md` 为每个场景写出：
   - `steps`：每一步对应的页面与控件；
   - `texts`：所有加引号的文字，逐条判为 `match`、`translation`、`mismatch`、`absent` 或 `data`；
   - `identity`：操作身份；
   - `mutates`：是否改动数据。

   不确定时判 `mismatch` 或 `absent`，交给人决定；看板会核对 `match` 的文字确实存在。

3. 运行 `sop-board atlas submit --task <编号>`。登记后批次页「界面对照」列出不一致与找不到的条目。

4. 把条目原样转告使用者，由使用者逐条决定，会话不得代填：
   - **按 SOP**：执行时实际不符判失败；
   - **改 SOP**：场景暂不执行，修改场景后重新对照。

   使用者给出姓名后运行 `sop-board atlas decide <批次> --item <场景>#<条目> --decision sop|change --by <姓名>`。
