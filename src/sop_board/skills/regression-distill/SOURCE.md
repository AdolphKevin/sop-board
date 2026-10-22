# 数据库测试资源来源

- 仓库：`https://github.com/AdolphKevin/praxis-skills`
- 来源提交：`409bf127e55b88411d021578842d313ea86d01b0`
- 来源路径：`verify-backend-sop-db/SKILL.md`、`verify-backend-sop-db/scripts/run_postgres_test.sh`
- 收入位置：`references/backend-db.md`、`scripts/run_postgres_test.sh`

运行器保留原文。参考正文保留真实入口、生产同类数据库、确定性数据、精确断言、负向分支与失败追踪的做法；完成条件改为本技能的逐测试运行证据与 `regression.json`。产品问题交调用方决定修复；非 PostgreSQL 运行器随产品测试设施维护。

同步时在来源仓库用 `git show <提交>:<来源路径>` 读取指定版本，比较与这里的差异；更新运行器及适用的参考段落，保留本技能的范围和报告要求，记录新的来源提交。核对脚本语法、已有数据库分支的退出码传递，以及有授权隔离环境时的容器启动与清理；如实报告未运行范围。
