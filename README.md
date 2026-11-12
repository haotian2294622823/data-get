# 制造数据抽取项目

按系统 LOT、批次实际结束日期、品番查询。填写多个条件时取交集。前工程按直接用料的 `material_lot_no` 回库查询，使用材料自身的 LOT，日期和品番只筛系统 LOT。

## 启动

在项目目录安装依赖并运行：

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

页面使用 `.env` 的 `APP_PASSWORD` 登录。DFEC 沿用现有 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`、`DB_SCHEMA`、`DB_TIMEZONE`、`DB_SSLMODE`。默认 Schema 为 `public`，日期时区为 `Asia/Shanghai`。

接入结构和关联键相同的另一个产品时，在同一份本地 `.env` 增加产品代码和带前缀的连接设置：

```dotenv
LOT_PRODUCTS=DFEC,ABC
ABC_DB_HOST=
ABC_DB_PORT=5432
ABC_DB_NAME=
ABC_DB_USER=
ABC_DB_PASSWORD=
ABC_DB_SCHEMA=public
ABC_DB_TIMEZONE=Asia/Shanghai
```

重启后，页面会显示产品数据库选择。每次查询只使用选中的连接。所有产品须提供相同的七张表和关联字段。

## 原始文件和合并宽表

页面查询在后台按完整系统 LOT 分批处理，保存到 `outputs/queries/<产品>/batch_<任务编号>/`。可用 `EXPORT_DIR` 指定存档根目录。完成后自动按列名对齐，拼接为一份 `final.csv`，并生成 `result.zip`（包含完整 CSV 和结果说明）。`part_*.csv` 只作为中间分片，`archives/part_*/` 保存对应的七张系统原始表和四张 `upstream_*.csv`。任务的 `manifest.json` 记录行数、列数、各阶段耗时、缓存命中及表头指纹。空表保留表头。

合并分三步：`lot_extract/merge.py` 先独立合并制造工程；`lot_extract/material_merge.py` 再按真实 `process_name` 分别合并材料工程；最后按原始 `material_lot_no` 的使用关系拼接。最终每个系统 LOT 一行，包含它使用的材料 LOT 及双方的相关信息，不按工程名直接关联不同 LOT，也不合计数量。

jioa'be输出列顺序为：系统 LOT 的批次信息 → 工序0材料工程 → 工序1及后续制造工程。工序0不再追加到表尾。

材料列按 E-Core、I-Core、卷线、其他工程排序，统一以 `工序0-` 开头，例如 `工序0-I-Core成型_4_material_lot_no`、`工序0-I-Core成型_4_机台设备1_machine_name`。同工程的不同材料 LOT 使用独立编号，工程内设备连续编号，明细保留工序关联键，工程原始字段放在 `工程记录N` 下。同一系统 LOT 重复使用同一材料时共用对应工程列组；系统用料区的原始 `material_lot_no` 保留不变。

数据库查询和最终导出仅保留顶层系统 `lot_no` 以大写 `E` 开头、且整个编号仅含一个大写 `E` 的行。例如 `E6X08010` 保留，`EE6X08010`、`E6XE8010` 排除。材料 LOT 不套用此规则、系统日期或品番条件。旧存档的最终筛选仍可由独立的 `lot_extract/presentation.py` 完成。批处理没有符合条件的 LOT 时记录零行任务，不生成空分片。

页面提交任务后仅显示当前步骤、已处理系统 LOT 数及材料缓存命中，并可取消。不显示进度条、估算百分比或剩余时间，也不再为进度条额外执行全年 LOT 总数查询。

页面“完整结果”直接提供完整 CSV 和 ZIP 的下载按钮，中间分片折叠为备用入口。完整文件通过同端口 HTTP 文件接口按块传输，支持 Range 请求，不进入 Streamlit 内存媒体缓存，不再设置 128 MiB 下载拦截；`EXPORT_DOWNLOAD_MIB` 已不再使用。实际传输速度受网络、磁盘和代理设置影响。

`app.py` 使用 Streamlit 1.58+ 的公开 `st.App` 接口同时挂载 `ui.py` 页面和下载路由，启动命令不变。下载凭证仅在登录后签发，链接一小时有效，页面刷新会续期；新查询或退出登录会撤销旧凭证，服务重启后也失效。请勿分享下载链接。每个服务进程仅运行一个页面导出任务，退出登录及关闭服务都会请求取消后台任务。

默认每批 500 个系统 LOT（`EXPORT_BATCH_LOTS`），单个 CSV 分片不超过 32 MiB（`EXPORT_PART_MIB`）。每批原始记录默认最多 100,000 行（`MAX_SOURCE_ROWS`）、原始 CSV 最多 640 MiB（`MAX_CSV_MIB`），宽表最多 400 万单元格（`MAX_WIDE_CELLS`）和 16,384 列。超限时自动二分 LOT 批次重试；单个 LOT 仍超限会明确失败，不截断记录。批处理不以 5,000 LOT 限制整个年度任务；`MAX_QUERY_LOTS` 只用于旧的单次取数入口。

最终拼接由 `lot_extract/final_export.py` 完成：收集所有分片列名，逐行按列名对齐，缺列补空，只输出一个表头，不合计数量、不重复转义原值。完整结果和中间分片均保持“批次信息 → 工序0 → 制造工程”的顺序。拼接和打包按行或按块写磁盘，不受单批总单元格预算限制；完整 CSV 超过 Excel 的行/列限制时显示提示，不截断数据。

## 手动工具

```powershell
python export_batches.py --start 2026-01-01 --end 2026-12-31 --product DFEC --batch-size 500
python -m lot_extract.final_export "outputs/queries/DFEC/已完成的分批任务目录"
python get_bylot.py LOT123 --product DFEC
python merge_csv.py "outputs/queries/DFEC/查询编号" --output "outputs/宽表.csv"
python -m lot_extract.presentation "已有合并表.csv" --output "仅E批次.csv"
```

`get_bylot.py` 单独运行时只取数和存档。省略 LOT 参数会提示输入。 `merge_csv.py` 单独运行时读取指定目录的原始 CSV，沿用系统的合并规则。页面和两个手动入口共用 `lot_extract/` 内的业务代码。

`presentation.py` 也能独立筛选已有最终 CSV；不传 `--output` 时生成同目录的 `*_E_only.csv`，禁止覆盖输入文件。

`final_export.py` 的独立入口可以拼接此前已完成、只有分片的任务，不重新查询数据库。要求清单状态为完成，且目录尚无 `final.csv` 或 `result.zip`，不会覆盖已有最终文件。

## 年度任务注意事项

同一任务取数使用一个只读、可重复读事务快照，材料原始记录缓存到系统临时目录的 SQLite；只复用本任务内已经完整读取的数据，查不到的材料也会记为已查询。取数完成后删除临时缓存并释放数据库快照，再进行本地拼接和打包。每个分片合并完写磁盘并释放本批数据。

长时间快照可能推迟 PostgreSQL 旧版本回收。年度导出建议连接报表库或只读副本，并由数据库管理员核对 `lot_no`、日期及品番索引。程序仍保留单条语句超时；本地验证不代表生产库全年导出性能验收。

取消或失败不会删除已完成分片及其原始存档。完整 CSV 与 ZIP 各自完整写入后才发布；打包失败时已经发布的完整 CSV 仍保留。可以用 `merge_csv.py` 重做已完成分片；当前版本不支持服务重启后恢复旧数据库快照，也不自动续查未完成部分。重新查询会创建新任务目录。
