"""页面与手动工具共用的业务包。

阅读顺序：database 负责查询，batch_export 编排取数与存档，merge 编排宽表合并。
manufacturing_merge 展开制造工序，material_merge 展开工序0，final_export 拼接最终 CSV。
原始表与关联键见 schema，工序0的展示配置见 material_rules.json。
"""
