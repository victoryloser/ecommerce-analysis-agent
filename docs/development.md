# 架构与数据流

## 处理链路

用户消息 → 会话与澄清 → AnalysisPlan → 语义校验 → SQL 编译与审计 → 只读执行 → 结果、图表和证据。

模型选择指标、条件与步骤，数据库计算金额、比例及排名。缺失年份、实体歧义及工作流条件进入统一澄清；明确的槽位回复直接补入计划后重新校验。

| 模块 | 职责 |
|---|---|
| source / import_data / data_upload | 来源、快照、上传映射和质量检查 |
| models / semantic / metadata | 计划、指标口径、关系和粒度 |
| providers / planner / clarification | 模型协议、规划和澄清 |
| sql_compiler / advanced / predicates | 聚合、比较、贡献、诊断和条件 |
| sql_guard / sql_audit / executor / query_worker | 审核、授权和执行 |
| session / session_store / orchestrator | 持久会话和工作流 |
| drilldown / sql_workspace | 下钻路径和 SQL 探索 |
| answers / presentation / charts / evidence / trace | 展示、导出和执行记录 |

## 数据与状态

导入的数据集使用独立快照与 manifest，绑定来源、schema、指标版本及覆盖期。界面从本机 manifest 发现数据集，无数据时显示上传入口。默认商品销售额按下单时间、已送达状态计算，不含运费；其他口径由计划明确指定。支付金额按订单计算，商家/品类支付额未定义分摊规则。

data/sessions.sqlite3 保存对话、成功计划、待澄清状态、下钻路径及 SQL 草稿。恢复时核对数据版本，每次查询重新授权。备份同时保留 data/ 和 runs/，后者含会话引用的证据；上传数据属于业务状态。

pending_plan 与 last_successful_plan 分离，澄清或执行失败不覆盖成功分析。模型上下文使用结构化状态、结果摘要及近期消息，默认最多 48,000 UTF-8 字节，每轮模型与工具合计最多 8 次调用。

## 下钻与 SQL

下钻按稳定 ID 绑定分组，期间取交集，原指标及条件继承；返回上层读取父证据。订单支持商品、支付及评价子表。

SQL 可访问原始表或上次完整分析结果。后者重新编译成功计划为 analysis_result，不使用当前页代替全量。草稿生成与执行分开，探索结果不覆盖结构化分析状态。

只读查询默认 5 秒、256 MB、2 线程，单页最多 500 行；完整导出沿用执行限制。自由 SQL 禁止写入、管理命令、外部读取及递归，自定义口径标为探索。

## 扩展与验证

新增指标先定义粒度、分母、过滤范围和舍入，再修改语义层、编译器及独立基准。新增意图须接入统一澄清、执行和展示，覆盖缺条件、多轮补条件及失败恢复场景。

模型协议在 providers.py 中维护，不做自动协议探测或跨模型切换。模型集成测试记录规划响应、查询结果及数据版本，输出到 runs/。

仓库不分发数据文件。`pytest` 的导入与首次使用测试会在临时目录生成少量业务记录，不依赖本机数据或模型密钥。

历史样例回归仍保留：`excel_full` 使用本机 `material/ecommerce_agent_demo.xlsx`，`csv_small` 使用 `material/ecommerce_agent_demo_csv/` 下按标准表名命名的九个 CSV 文件。缺少这些文件时，相应测试标记为跳过。有文件时可先运行 `python -m app.import_data`，再运行完整测试及 `scripts/check_*_ui.py`。`app.demo`、`app.baseline`、`app.evaluate` 和录屏脚本均属于这些本地样例工具。

`scripts/build_release.py` 仅打包代码和文档，不包含 `material/` 或数据文件；`scripts/verify_release.py` 在干净环境运行测试，并记录缺少本地样例导致的跳过项。

## 自动测试

`.github/workflows/ci.yml` 在 push、pull_request 或手动触发时运行，测试矩阵为 Python 3.11、Ubuntu 和 Windows。工作流安装锁定依赖、检查依赖一致性、运行离线 pytest，并构建源码包。Junit 测试报告作为 Actions artifact 保留七天。

工作流不需要模型密钥或业务数据；临时测试记录由 `tests/test_data_onboarding.py` 在测试目录生成。缺少本地历史样例的用例会记录为跳过，不能将这些跳过项计为通过。

## 文档截图

安装可选浏览器工具并生成 README 截图：

```powershell
.venv/Scripts/python.exe -m pip install playwright==1.65.0
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe scripts/capture_readme.py
```

已安装 Microsoft Edge 时，也可以运行 `python scripts/capture_readme.py --browser-channel msedge`。端口默认 7871，可通过 `--port` 更改。

脚本在临时目录生成一笔虚构订单和关联记录，并使用固定模型响应。它验证首次启动、上传、字段核对、导入和查询的界面流程；金额由数据库计算。输出为 `docs/images/first-run.png` 和 `docs/images/analysis.png`，不会读取本地业务数据或调用真实模型。截图可以随文档提交到 Git，临时数据在结束后清理。

## 浏览器与录屏

可选开发工具：

```powershell
.venv/Scripts/python.exe -m pip install playwright==1.65.0 imageio-ffmpeg==0.6.0
.venv/Scripts/python.exe -m playwright install ffmpeg
.venv/Scripts/python.exe scripts/check_browser.py
$ffmpegPath = .venv/Scripts/python.exe -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
.venv/Scripts/python.exe scripts/record_demo.py --output .build/demo-new --ffmpeg $ffmpegPath
```

浏览器检查使用本机 Edge。录屏调用已配置的模型服务，并使用隔离会话库；输出由 --output 指定。

## 部署

运行目录包括 app/ 和本机配置。安装依赖后直接启动 app.ui，并通过页面导入符合 [数据格式说明](data-format.md) 的文件；CLI 使用相同的数据与会话存储。

界面默认用于本机开发；企业网络部署需接入实际身份认证与访问控制，当前未提供完整企业登录系统。
