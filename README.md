# 制造批次追溯与自助数据抽取系统 V0.3

从零依据根目录 V0.3 开发文档重建。旧实现、旧配置、旧依赖环境与旧本地 Git 历史已清除。原开发文档保持原文；当前实现与验证记录见 [docs/implementation-v03.md](docs/implementation-v03.md)。

## 启动（PowerShell）

```powershell
cd D:\Codeing\data_get
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 必须自行设置共享访问密码；这里输入的密码仅供本机运行使用。
$env:APP_PASSWORD = Read-Host '请设置应用访问密码'
$env:DATA_SOURCE_MODE = 'mock'
.\.venv\Scripts\python.exe -m streamlit run app.py
```

访问终端显示的本机地址，通常为 http://localhost:8501。输入自己设置的密码登录，LOT 输入 `ROOT_A`，点击“开始追溯”；确认路径和告警后，选择工序、数据集与字段，点击“生成数据集”。下载使用本次已生成的完整 CSV。按 Ctrl+C 停止应用。

也可以复制 `.env.example` 为 `.env` 并填写 `APP_PASSWORD`。环境变量优先于 `.env`；Streamlit secrets 也支持 `APP_PASSWORD`。没有密码时不允许初始化查询或下载。不要将 `.env` 或 `.streamlit/secrets.toml` 提交到 Git。

## 实现流程

1. 数据源工厂为每次提交建立一次读取会话。
2. 按层批量读取材料，执行向上游的 BFS 追溯。
3. 事件事实只保存一次，不同投料事件与不同路径前缀分别保留。
4. 读取所有内部 LOT 的工序，并在工序全表按引用键核验材料归属。
5. 每种粒度独立输出，不把设备、材料、不良明细横向相乘。
6. 正式生成时重查全部基础事实，结束读取事务后生成 CSV，再一起发布新图和结果。

基准：5 节点（4 内部、1 未匹配边界），6 事件，8 路径前缀，5 工序，6 材料。模拟设备与不良各 2 条。未匹配内部 LOT 仅表示追溯边界，不能解释为已确认外部原料。

## 目录

| 文件/目录 | 用途 |
|---|---|
| `app.py` | 加载环境、认证、组织页面 |
| `src/ui/` | 密码入口、会话状态、双表单和结果展示 |
| `src/services/trace_service.py` | BFS、事件与路径、工序引用核验、累计限额 |
| `src/services/query_service.py` | 正式生成、新快照、工序筛选、独立数据集 |
| `src/data_access/` | Mock CSV 与 PostgreSQL 只读适配器 |
| `src/models.py` | 请求、结果、限额与数据源协议 |
| `src/catalog.py` | 固定标识列和字段目录白名单 |
| `src/processing/records.py` | 稳定 JSON ID、重复事实检查、引用状态 |
| `src/exporting/` | CSV 编码、大小限制、公式保护、安全文件名 |
| `config/` | 模拟/真实字段目录及保护限额 |
| `data/demo/` | 文档第 9.2 节的原始模拟 CSV |
| `tests/` | pytest 业务测试和 Streamlit AppTest |

## PostgreSQL 接入位置

```powershell
.\.venv\Scripts\python.exe -m pip install "psycopg[binary]"
# 在本地 .env 中填写 DATABASE_URL=postgresql+psycopg://...
$env:DATA_SOURCE_MODE = 'real'
.\.venv\Scripts\python.exe -m streamlit run app.py
```

先核对 `src/data_access/postgres.py` 的 `TABLES`：`public.prass_lot`、`public.prass_process_material`、`public.prass_lot_process`。目前只预留这三张核心表的已知映射，尚未连接生产环境。未核实的设备、人员、工具、不良、检验等真实字段不开放，`config/fields_real.yaml` 的可选字段保持为空。

每个操作使用一条连接、一笔 READ ONLY / REPEATABLE READ 事务；查询参数绑定，列与表名来自固定白名单。批量参数最多每批 500 个，返回行数受剩余额度限制。每次 SQL 设置剩余时限对应的 statement_timeout，连接超时 10 秒。账号也必须具备只读权限。

`real` 缺配置会明确报 DataSourceNotConfigured，不回退模拟数据。SQL Server 仅是文档中提到的授权 ETL 对账源，此应用不直接连接它。真实库字段类型、ETL 刷新窗口、关联键和业务含义仍需现场验证。

## 限额、状态与导出

- `config/limits.yaml` 固定每次操作的深度/唯一节点/唯一事件/路径行数/原始材料/各来源行数、总时限和文件大小。
- 深度边界的内部 LOT 仍检查材料，恰在最大深度的叶节点可正常完成。
- 循环保存一次闭环到达并停止该路径，不自行认为数据缺失。
- 超限结果标记 INCOMPLETE_TRACE，仅页面诊断，禁止生成 CSV。读取失败与正常空结果分开。
- 工序筛选不截断事件和路径；缺失、冲突或无法核验工序引用的材料始终保留。
- 下载保存好的字节，草稿修改、展开列表或切换预览不再查询。
- CSV 为 UTF-8 BOM。公式起始文本添加单引号保护；Excel 导入时将 LOT 设为文本以保留前导零。
- 每次登录失败达到 5 次后，本会话锁定 30 秒；共享密码不具备个人审计或跨会话全局限流能力。部署限定内网/VPN，按公司要求启用 HTTPS。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy src app.py --ignore-missing-imports --check-untyped-defs
```

开发使用安装在本机 Codex 的 Streamlit 官方 `developing-with-streamlit` Skill，以及 obra/superpowers 的计划、测试驱动、调试、审查和完成验证技能。这些属于开发工具，不是应用运行依赖。

尚未完成 M5 真实业务样本核验及 M6 生产部署验收；模拟通过不代表生产数据正确。首版不提供 root_summary、未知检测字段、ETL 自动修复或跨表随意连接。
