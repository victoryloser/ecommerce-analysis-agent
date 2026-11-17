# R2 评测与交付操作

正式材料由使用者在仓库外或本地忽略目录提供，不随代码发布。评测模块读取题面和原始来源，Agent 只接收问题及正常业务目录，不接收参考答案。新增工具不依赖外部评测平台。

## 准备快照

```powershell
python scripts/prepare_round2.py --package "semi-material-2/公开复赛包_v1.1" --output .build/r2-snapshots.json
```

默认导入 CSV；`--format xlsx` 可验证 Excel 入口。每次生成新快照 ID，映射文件不可覆盖。`--data-dir` 可指定隔离数据库目录，后续命令须使用同一目录。

## 连续评测

配置现有 `.env` 的模型服务后运行以下命令；这些命令会实际请求模型，不注入固定计划。

```powershell
python -m app.round2_evaluate --snapshots .build/r2-snapshots.json --questions "semi-material-2/公开复赛包_v1.1/03_综合问题与连续追问.md" --case D1:A --case D4:B --output runs/r2-smoke
python -m app.round2_evaluate --snapshots .build/r2-snapshots.json --questions "semi-material-2/公开复赛包_v1.1/03_综合问题与连续追问.md" --repeats 3 --output runs/r2-full
python -m app.evaluation_summary --reports runs/r2-full/report.json
```

Smoke 为 8 回合子集。完整单轮为 D1–D4 × A/B × 4 题，共 32 回合；三轮共 96 回合。选择 `D1:A3` 会运行整条 A 链，保证真实前置上下文。每个数据集、组和重复使用独立 Session。组内按顺序执行，失败不会被标准计划补齐。

`--max-model-calls` 默认 128，`--max-total-calls` 默认 400，`--max-seconds` 默认 3600。每轮开始前按已有 4 次模型、11 次总调用上限预留预算；耗时在回合边界检查，单请求仍受 provider timeout 限制。未启动的题保留在分母内。

`report.json` 在首个请求前写入完整矩阵，每个回合开始与结束保存；trace 逐事件追加。报告包含模型、查询和目录调用数、实际回答、分项判定、快照身份、实现及判定器指纹。服务返回 usage 时才记录实测 token，没有返回则标为 `not_provided`，不推测费用。

`index.md` 与 `evidence-index.json` 提供相对路径证据索引和 SHA-256。`evidence/` 包含引用的查询、工作流和 trace 副本，可在搬迁整个运行目录后查阅。`sessions.sqlite3` 用于恢复，不用于发布源代码。

## 失败与恢复

超时或 HTTP 故障会停止当前运行，保留剩余未运行项。故障排除后，使用同一命令、同一输出目录，加 `--resume --recover`。恢复要求题面、代码、判定器、模型配置、快照及会话检查点一致。

恢复是新 attempt，通过 `parent_attempt_id` 指向原尝试；首次失败不因恢复成功变成通过。部分工作流只继续未完成步骤。连续修改或恢复中复用的已完成步骤，必须与同一会话先前记录的完整证据一致，仍逐项核对快照身份和原始数据结果；其他会话、未知来源或被修改的旧证据不能通过。若进程在回合中途崩溃且无法确认检查点，保留旧记录并用新输出目录重跑整条链；不会猜测进度。

产品错误保留为失败，后续题继续使用实际上下文。修复实现后应新建完整运行。汇总拒绝不同题面、实现、判定器、模型配置或快照身份混合计分；固定规划、故障注入和真实模型模式分开。

条件更新的操作和值在输入入口校验。必填结果设置不允许remove；更新日期须放入updates的period，不能写在会被忽略的顶层time_range。此类结构错误沿用最多两次模型格式修正，所有请求计入model_calls；不是评测失败后重跑，也不由宿主注入参考计划。

## 判定边界

判定器检查有效日期、状态、筛选、指标、全部品类、第三名并列、两期异常业务键、自然客户去重、金额完整性和实际回答。期望值来自独立遍历原始来源的 Oracle，不调用生产 SQL 编译器或质量分类器。

允许指标拆分、步骤换序、合法冠军订单替换、简单 AND 的等价 `eq/in` 筛选，以及被题目筛选固定为单一值的分组汇总；不要求固定四步工作流。复杂 OR/NOT 无法证明等价时报告需要核验，不直接算成功。回答数值通过标签、期间和实体对应，不靠全文哈希或数字是否出现；当前自动检查适配产品的确定性表格与文案，表达自然度仍需人工复核。

云帐房网关的 `qwen3.8-max` 在本次实测中，未设置思考模式的正式规划请求分别超过 60 秒和 180 秒；显式 `LLM_ENABLE_THINKING=false` 后同类请求约 4 秒返回。本地采用该配置，超时设为 180 秒。此参数仅在环境变量明确配置时发送，其他网关可不配置；模式也写入评测指纹。历史失败记录保留，不与改配置后的结果合并。

运行离线校验：

```powershell
python -m pytest -q
```

`test_r2_evaluation.py` 覆盖判定器反例、替代选单、故障计数和重复隔离。`test_r1_round2.py` 在有正式包时对实际连续更新的 32 回合追加新判定器验证；它使用固定规划，不证明真实模型通过。`test_r2_data_relations.py` 验证 CSV/Excel 业务等价与 D1/D2 拆分支付等价；D1→D3 金额更正、D3→D4 金额资格变化及故障恢复沿用 R0/R1 测试。这些测试独立于正式 32 回合分母。

`python scripts/check_r2_ui.py` 使用合成数据和固定规划执行完整录屏流程，报告明确标记 `fixed-planner-browser`；它同时验证换数据、新会话、下载依据和视频解码，不访问模型服务。可选浏览器工具安装方法见开发文档。

## 正式录屏

安装现有可选 Playwright 和 ffmpeg 后：

```powershell
python scripts/record_round2.py --snapshots .build/r2-snapshots.json --questions "semi-material-2/公开复赛包_v1.1/03_综合问题与连续追问.md" --sessions .build/r2-video-sessions.sqlite3 --output .build/r2-video --ffmpeg "path/to/ffmpeg.exe"
```

脚本执行 D1/A 与 D4/B 的真实点击和真实模型请求，检查换数据后会话与证据身份，下载并打开实际依据。保留未剪辑 WebM、MP4、截图、浏览器 trace、实际回答及题号—turn—record—时间点索引。关闭 context 后才转码并完整解码验证。浏览器 trace 与业务判定分开；失败录像和失败记录保留，不标为成功演示。

## 固定源码包

```powershell
python scripts/build_release.py --commit <已提交的SHA> --output dist/source.zip
python scripts/verify_release.py --archive dist/source.zip
```

源码字节直接从指定 Git 提交的 blob 读取，使用固定归档时间、排序和产品清单。工作区未提交修改不进入包。排除方案、比赛材料、业务数据、运行记录、真实 `.env` 及 `AGENTS.md`。离线包验证报告明确列出通过、失败、跳过，不等于正式数据或真实模型验收。比赛报告、运行证据和录像放在独立交付目录，不纳入 GitHub 源码包。

案例报告可引用索引中的 D4/B2、B3（金额规则及可确认结果），以及 A1–A4、B3–B4（连续更正及整单核查）。应引用实际结果和失败，不预填通过率。初赛对照需核实真实初赛提交；中间版本不能冒充初赛版本。
