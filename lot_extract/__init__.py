"""页面与手动工具共用的业务包。

阅读顺序：database 查询，batch_export 批量导出，merge 展开制造工序并挂接材料。
material_merge 包含工序0规则与展开，final_export 拼接最终 CSV。
表结构、LOT 筛选与 CSV 处理集中在 tables，材料展示配置见 material_rules.json。
"""
