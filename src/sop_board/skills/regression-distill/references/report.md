# guard.json 契约

UTF-8 JSON，放在 `sop-board distill prepare` 给出的工作目录。`sop-board distill submit` 读取、核对并实际运行后，把守护合并进产品仓库 `<场景目录>/regression.yaml`。

```json
{
  "commands": {
    "unit": {"repo": "app", "cwd": ".", "argv": ["uv", "run", "pytest", "-q", "--junitxml={junit}"], "timeout": 900},
    "web": {"repo": "app", "cwd": "acceptance/guards", "argv": ["node", "{playwright}", "test", "--reporter=junit"], "timeout": 1800, "browser": "merchant"}
  },
  "guards": {
    "refund#R2-P01": {
      "acceptance": {"result": 41},
      "tests": [
        {"id": "amount", "repo": "app", "path": "tests/test_refund.py", "name": "tests/test_refund.py::test_refund_paid_amount", "layer": "unit", "command": "unit"}
      ],
      "coverage": {
        "expected": [{"index": 0, "text": "买家原支付账户到账 99.00 元", "tests": ["amount"]}],
        "forbidden_effects": [{"index": 0, "text": "退款超过 99 元", "tests": ["amount"]}]
      },
      "detection": {"method": "mutation", "assertion": "退款金额等于实付金额", "evidence": "detection-R2-P01.txt"},
      "ledger": []
    }
  }
}
```

## commands

- 键是命令编号（小写字母、数字、`-`、`_`）。与 regression.yaml 已有的同名命令内容必须相同，否则换一个编号。
- `repo`：本检出中的仓库别名；`cwd`：仓内相对目录；`argv`：参数数组，不经 shell。`{junit}` 换成 junit 输出路径（也设为 PLAYWRIGHT_JUNIT_OUTPUT_NAME），`{playwright}` 换成看板运行时的 Playwright CLI。
- `timeout`：秒，1～7200，缺省 900；可选 `env`：额外环境变量（不能以 `SOP_` 开头）。`cwd`、`argv`、`env` 都随 regression.yaml 提交，里面不写本机绝对路径。
- 浏览器层命令写 `browser: <身份>`：运行前看板拉起本检出的环境、取得该身份浏览器的租约，注入 `PW_CDP_ENDPOINT`、入口地址、`PW_NODE_ROOT` 与 `PLAYWRIGHT_BROWSERS_PATH`。`{playwright}` 使用同一份运行时；retest 不生成原验收的 scope.json、identities.json 或 run.env，守护不依赖这些材料。
- 浏览器守护可放在 spec 库任意子目录（如 `acceptance/guards/`），同目录保留 spec、`library.mjs`、项目辅助文件、守护 `fixtures.mjs` 和配置；命令 `cwd` 指向该目录。夹具由 `PW_NODE_ROOT/package.json` 创建 `createRequire` 取得 `@playwright/test`，只经 CDP 连接看板浏览器，不自己启动浏览器；`expect` 保持 Playwright 原实现，`step` 执行传入步骤，断言异常正常传播。前置准备与只读查询等业务代码放 `library.mjs`，不藏进夹具。
- 配置让 junit 写到 `PLAYWRIGHT_JUNIT_OUTPUT_NAME`，其余报告与截图写到它所在目录的子目录；保留单 worker、无重试。首次保真比较允许换运行夹具，夹具自身是否正确仍须实际运行和检出能力核对。

## guards

- 键是场景键 `<文件>#<场景编号>`；一个场景只在全部预期都有测试时才写进来。
- `acceptance.result`：acceptance.json 中的独立通过结果编号。须是同指纹、经真实入口、执行者与实现者不同的通过；实现者自检、未声明或早先保留（未实际执行）的通过都会被拒。
- `tests`：`id` 在本守护内唯一；`repo`、`path` 指向本检出中的普通文件（摘要由 submit 计算，不用填）；`name` 是 junit 执行清单中的测试名（pytest 可写 `文件::函数`，Playwright 写测试标题）；`layer` 为 `unit`、`api`、`component`、`browser`；`command` 为命令编号。
- `coverage`：expected 与 forbidden_effects 按原文顺序逐条列全，`index` 从 0 开始，`text` 与原文一字不差，`tests` 为覆盖它的测试 id。缺一条整份拒绝。
- `detection`：`method` 为 `mutation`（受控改错）或 `known-defect`（复现已知缺陷），`assertion` 写实际失败的业务断言，`evidence` 为证据文件（相对 guard.json）。
- `ledger`：只用于浏览器层。改写已有 spec 的断言时逐条写 `{"assertion_id", "kind": "rewrite", "reason"}`；删除断言须 `{"assertion_id", "kind": "withdraw", "reason", "ref"}`，ref 指向业务预期变更的确认来源。保真检查以 git HEAD 中的同一 spec 连同它的本地导入（`./fixtures.mjs`、`./library.mjs` 及其导入）为基线；HEAD 还没有这份 spec 时，以产生独立通过的那份 spec 为基线（见主技能第 5 步）。

## 守护什么时候有效

retest 只运行有效守护，以下条件缺一不可：

- 场景存在，当前指纹与登记时相同；
- 测试文件仍是仓库内的普通文件，且 sha256 一致；
- coverage 与当前原文逐条一致；
- 检出能力证据存在；
- acceptance 是同指纹的独立通过；
- 没有登记过漏检。

漏检指最近一次守护运行通过，之后真实入口执行或人工复测却发现不通过。漏检后要再次沉淀，须同时满足两点：acceptance 是漏检之后取得的新的独立通过；测试摘要与漏检时不同，即补上了能抓住该问题的断言。

retest 中，每条命令退出码为 0、声明的测试名都在 junit 中且通过、运行前后产品快照不变，才记守护运行通过，场景状态不变；否则场景转为待复测，并附上原因。
