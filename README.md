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

每次查询保存到 `outputs/queries/<产品>/<查询编号>/`。可用 `EXPORT_DIR` 指定存档根目录。系统 LOT 的七份原始表和前工程的四份 `upstream_*.csv` 在同一个目录。前工程四表包括批次、工序、不良和机台设备。空表保留表头。

页面下载每个系统 LOT 一行的宽表。前工程列用原始材料类别名加序号，例如 `DFEC-I-Core_1_材料LOT`。同一系统 LOT 中，重复用到同一材料时共用列组，每条用料记录保留对该组的引用。材料没有批次记录时，保留编号和查询状态。

页面默认限制 5,000 个系统 LOT、100,000 条原始记录、64 MiB 原始 CSV 和 400 万宽表单元格。可通过 `MAX_QUERY_LOTS`、`MAX_SOURCE_ROWS`、`MAX_CSV_MIB`、`MAX_WIDE_CELLS` 调整。读取过程中检查行数和大小，超限会中止并提示缩小范围。

## 手动工具

```powershell
python get_bylot.py LOT123 --product DFEC
python merge_csv.py "outputs/queries/DFEC/查询编号" --output "outputs/宽表.csv"
```

`get_bylot.py` 单独运行时只取数和存档。省略 LOT 参数会提示输入。 `merge_csv.py` 单独运行时读取指定目录的原始 CSV，沿用系统的合并规则。页面和两个手动入口共用 `lot_extract/` 内的业务代码。
