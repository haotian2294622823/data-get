import pandas as pd
import psycopg2

# 1. DB Info (已按要求填入)
DB_SERVER = '10.65.48.57'
DB_NAME = 'dfectest26'
DB_USER = 'tok2026'
DB_PASS = 'Saitama2026'

# 需要抽取的目标业务表
TARGET_TABLES = [
    'prass_lot',
    'prass_lot_inspection',
    'prass_lot_lot',
    'prass_lot_process',
    'prass_process_defect',
    'prass_process_machine',
    'prass_process_material',
    'prass_process_tool',
    'prass_process_worker'
]

# 批次号相关字段列表
LOT_NO_COLUMNS = ['prass_lot_no', 'ps_lot_no', 'lot_no', 'lot_id', 'batch_no']

def main():
    # 获取用户输入
    lot_no = input("请输入要匹配的 lot_no 值: ").strip()
    if not lot_no:
        print("❌ lot_no 不能为空，已退出。")
        return

    print(f"🔍 正在连接数据库: {DB_SERVER}/{DB_NAME} ...")
    conn_params = {
        "host": DB_SERVER,
        "port": 5432,
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASS
    }

    try:
        conn = psycopg2.connect(**conn_params)
        
        for table in TARGET_TABLES:
            print(f"⏳ 正在处理表: {table} ...")
            
            # 获取表结构，自动识别批次号字段
            cur = conn.cursor()
            cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}'")
            existing_cols = [row[0] for row in cur.fetchall()]
            cur.close()
            
            # 提取该表中实际存在的批次号字段
            valid_cols = [col for col in LOT_NO_COLUMNS if col in existing_cols]
            
            if not valid_cols:
                print(f"   ⚠️ 未在表 {table} 中发现批次号字段，已跳过。")
                continue
                
            # 构造 OR 匹配条件（使用 %s 参数化防止 SQL 注入）
            conditions = [f"{col} = %s" for col in valid_cols]
            where_clause = f"WHERE {' OR '.join(conditions)}"
            
            # 执行查询并导出为 CSV
            query = f"SELECT * FROM {table} {where_clause}"
            df = pd.read_sql(query, conn, params=[lot_no] * len(valid_cols))
            
            if not df.empty:
                filename = f"{table}.csv"
                df.to_csv(filename, index=False, encoding='utf-8-sig')
                print(f"   ✅ 导出成功: {filename} (共 {len(df)} 条记录)")
            else:
                print(f"   🔍 表 {table} 中没有匹配到该 lot_no 的数据，已跳过。")
                
    except Exception as e:
        print(f"❌ 发生错误: {e}")
    # finally:
    #     if 'conn' in locals():
    #         conn.close()
    #         print("🔒 数据库连接已关闭。")

if __name__ == '__main__':
    main()