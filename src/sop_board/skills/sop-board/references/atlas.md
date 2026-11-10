# 交互地图

地图描述 Web 入口“在哪、叫什么、怎么定位、点了打开什么、发出什么请求”，供执行者操作；业务预期属于场景。

1. `sop-board atlas stale [--entry <入口>]`：某入口为 null 表示还没有地图；列出的页面是引用源码已变化的过期页面。
2. `sop-board atlas prepare --entry <入口> [--mode full|stale]`：还没有地图或前端大改用 full（缺省）；只更新过期页面用 stale。
3. 读输出的 prompt.md，按其只读前端源码写地图文件。
4. `sop-board atlas submit --entry <入口>`：校验结构与源码位置并记录源码摘要。不合格时按输出修正后重交。报告页面数；地图文件留在仓库，由开发者审核提交。
