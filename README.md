# 制造数据抽取项目

页面支持按 LOT 号、批次实际结束日期、品番精确查询。品番字段可选 `murata_type` 或 `toko_type`，填写多个条件时取交集。日期包含开始日和结束日，按 `DB_TIMEZONE` 判断自然日，默认 `Asia/Shanghai`。

## 启动

在项目目录安装依赖，将 `.env.example` 复制为 `.env` 后填入 PostgreSQL 只读账号和 `APP_PASSWORD`。已有 `.env` 时沿用原文件。六张表须位于配置的 `DB_SCHEMA`（默认 `public`）。

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

使用终端给出的地址进入页面，输入 `APP_PASSWORD` 登录。查询成功后点击「下载合并宽表 CSV」。每个 LOT 一行，工序和明细按现有 `merge_csv.py` 规则展开。

## 原始文件

后端默认保存到 `outputs/queries/<UTC时间_查询ID>/`，每次成功读取对应六张原始表各一份 CSV。空表保留表头，未命中 LOT 的查询也会留存六份表头。可用 `EXPORT_DIR` 配置目录。原始文件供后端查看，页面仅提供合并宽表下载。

单次最多读取 500 个 LOT、合计 100,000 条源记录和 64 MiB 原始 CSV。宽表超过列数、单元格数或文件大小上限时请缩小查询范围。若源记录无法按现有键区分，原始 CSV 已保存，可在后端核对后再合并。

`python get_bylot.py <LOT>` 可直接在命令行存档某个 LOT 的六张原始表。`python merge_csv.py <CSV目录> --output <宽表.csv>` 可离线合并某次存档。
