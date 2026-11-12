"""七张原始表的统一约定，供数据库导出和离线 CSV 合并共同使用。

更换产品库时，需要保持这些表名、关联字段和业务含义一致。
这里的候选键用于发现歧义记录，并不代表已检查数据库中的真实主键约束。
"""

LOT = "prass_lot"
PROCESS = "prass_lot_process"
DEFECT = "prass_process_defect"
MATERIAL = "prass_process_material"
MACHINE = "prass_process_machine"
UPSTREAM_PREFIX = "upstream_"
# 前工程只读取批次、工序、不良和机台，不继续递归查询材料的材料。
UPSTREAM_TABLES = (LOT, PROCESS, DEFECT, MACHINE)

# 值的结构为（中文明细名，候选键字段）。
# process_key 区分具体工序执行；pass_order 只表示道次，不能单独充当工序身份。
# 此字典的插入顺序也用于导出顺序和宽表中各类明细的排列。
TABLES = {
    LOT: ("批次", ("lot_no",)),
    PROCESS: ("工序", ("lot_no", "process_key")),
    DEFECT: ("不良", ("lot_no", "process_key", "pass_order", "defect_code")),
    MATERIAL: ("材料", ("lot_no", "process_key", "pass_order", "seq")),
    "prass_process_tool": ("工具", ("lot_no", "process_key", "pass_order", "tool_code")),
    "prass_process_worker": ("人员", ("lot_no", "process_key", "pass_order", "worker_code")),
    MACHINE: ("机台设备", ("lot_no", "process_key", "pass_order", "machine_code")),
}

EXTRA_FIELDS = {
    PROCESS: {"pass_order"},
    DEFECT: {"defect_name", "defect_qty"},
    MATERIAL: {"material_lot_no"},
}


class ExportSizeError(ValueError):
    """完整批次超出资源预算，可缩小 LOT 批次重试；不代表原始数据有误。"""


def validate_columns(table: str, fields: list[str]) -> None:
    """检查关联/合并所需的最少字段，并拒绝会造成取值歧义的重复列名。

    允许出现更多业务字段：取数采用 SELECT *，合并时会继续处理这些原始字段。
    本函数只校验表头，记录级的候选键重复由 merge.read_tables 检查。
    """
    needed = set(TABLES[table][1]) | EXTRA_FIELDS.get(table, set())
    missing = needed - set(fields)
    if missing:
        raise ValueError(f"{table} 缺少字段：{', '.join(sorted(missing))}")
    if len(fields) != len(set(fields)):
        raise ValueError(f"{table} 存在重复列名")
