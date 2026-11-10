"""
merge_wide_all_cols.py — 6 张 prass 表横向合并为「1 批次 = 1 行」全列宽表
说明：缺陷/物料/工具/作业员 4 张子表整批级汇总为列；
      prass_lot_process 8 道工序按 pass_order 展开成 工序N_字段 列。
用法：把 6 个 csv 与本脚本放同一目录，运行  python merge_wide_all_cols.py
"""
import pandas as pd, os, sys

KEY = "lot_no"
SEPC = " | "
LOT_CSV, PROC_CSV = "prass_lot.csv", "prass_lot_process.csv"
SUB = {"缺陷":"prass_process_defect.csv","物料":"prass_process_material.csv",
       "工具":"prass_process_tool.csv","作业员":"prass_process_worker.csv"}
PROC_FIELDS = ["process_key","process_name","process_code","pass_order",
               "input_qty","output_qty","defect_qty",
               "actual_start_datetime","actual_finish_datetime"]
SUB_FIELDS = {"缺陷":["defect_name","defect_code"],"物料":["material_class_name","material_item_no"],
              "工具":["tool_name"],"作业员":["worker_name"]}
SUB_SUM = {"缺陷":["defect_qty"]}

def uniq_join(s):
    seen = []
    for v in s.tolist():
        if pd.isna(v) or v in seen: continue
        seen.append(v)
    return SEPC.join(map(str, seen))

def main(base_dir="."):
    rd = lambda f: pd.read_csv(os.path.join(base_dir, f))
    lot, proc = rd(LOT_CSV), rd(PROC_CSV)
    subs = {k: rd(v) for k, v in SUB.items()}

    row, colgroup = {}, {}
    for c in lot.columns:                      # 批次基础列
        row[c] = lot.iloc[0][c]; colgroup[c] = "批次"

    for i, (_, r) in enumerate(proc.sort_values("pass_order").iterrows(), 1):  # 工序展开为列
        for f in PROC_FIELDS:
            if f in proc.columns:
                col = f"工序{i}_{f}"; row[col] = r[f]; colgroup[col] = f"工序{i}"

    for name, df in subs.items():               # 子表整批级汇总为列
        for f in SUB_FIELDS[name]:
            col = f"{name}_{f}汇总"
            row[col] = uniq_join(df[f]) if f in df.columns else ""
            colgroup[col] = name
        for sc in SUB_SUM.get(name, []):
            col = f"{name}_{sc}合计"
            row[col] = int(df[sc].sum()) if sc in df.columns else ""
            colgroup[col] = name

    final = pd.DataFrame([row])
    out = os.path.join(base_dir, "批次全字段宽表.csv")
    final.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"✅ 已生成 {final.shape[0]} 行 x {final.shape[1]} 列  ->  {out}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
