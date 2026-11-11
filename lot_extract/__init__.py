"""页面与手动工具共用的业务包。

阅读顺序：schema 定义表和关联键；database 管配置和数据库读取；
service 串起查询与原始存档；merge 把存档合并成每个系统 LOT 一行的宽表。
app.py、get_bylot.py、merge_csv.py 是入口，业务规则集中在这个包内。
"""
