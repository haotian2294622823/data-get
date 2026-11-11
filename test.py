"""独立的数据库连通性诊断脚本，不参与页面查询和 CSV 合并。

本脚本支持多种数据库驱动；业务程序使用的是 lot_extract/database.py 中的 psycopg。
连接成功仅说明当前配置能建立连接，不代表七张业务表的字段、关联键和权限已验证。
直接运行会尝试连接配置中的数据库，请在能够访问该库的网络环境中使用。
"""

import os
import sys
from dotenv import load_dotenv

# ==================== 配置区 ====================
# ENV_PATH 是本机 .env 的位置；迁移目录或电脑时需要核对这个路径。
ENV_PATH = r"D:\Code_train\Data_Get\data-get\.env"
# ================================================

def test_connection():
    """按 URL 优先、分项配置其次的顺序测试连接，以 True/False 表示结果。"""
    # 1. 加载 .env 文件
    if not os.path.exists(ENV_PATH):
        print(f"[错误] .env 文件不存在: {ENV_PATH}")
        return False

    load_dotenv(ENV_PATH)
    print(f"[成功] 已加载 .env 文件: {ENV_PATH}")

    # 2. 读取常见数据库配置变量名（兼容多种命名习惯）
    db_type = (
        os.getenv("DB_TYPE") or os.getenv("DATABASE_TYPE") or
        os.getenv("DB_DRIVER") or ""
    ).lower()

    host = os.getenv("DB_HOST") or os.getenv("DATABASE_HOST") or "localhost"
    port = os.getenv("DB_PORT") or os.getenv("DATABASE_PORT")
    user = os.getenv("DB_USER") or os.getenv("DATABASE_USER") or os.getenv("DB_USERNAME")
    password = os.getenv("DB_PASSWORD") or os.getenv("DATABASE_PASSWORD") or os.getenv("DB_PASS")
    database = os.getenv("DB_NAME") or os.getenv("DATABASE_NAME") or os.getenv("DB_DATABASE")
    url = os.getenv("DATABASE_URL") or os.getenv("DB_URL")

    # 3. 如果提供了完整的 DATABASE_URL，优先使用。
    # 此分支依赖 SQLAlchemy 和 URL 对应的驱动；失败后直接返回，不再尝试分项配置。
    if url:
        print(f"[信息] 检测到 DATABASE_URL，使用 URL 方式连接...")
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("[成功] 数据库连接测试通过！(via DATABASE_URL)")
            return True
        except Exception as e:
            print(f"[失败] DATABASE_URL 连接失败: {e}")
            return False

    # 4. 根据 DB_TYPE 分别测试。
    # 各分支按需导入驱动，未选用的数据库无需安装对应包；默认端口也在分支内设置。
    if not db_type:
        print("[警告] 未检测到 DB_TYPE / DATABASE_URL，请检查 .env 中是否包含数据库类型标识")
        print(f"  当前读取到的变量: HOST={host}, PORT={port}, USER={user}, DB={database}")
        return False

    print(f"[信息] 数据库类型: {db_type} | Host: {host} | Port: {port} | User: {user} | DB: {database}")

    try:
        if db_type in ("mysql", "mariadb"):
            import pymysql
            port = int(port or 3306)
            conn = pymysql.connect(host=host, port=port, user=user, password=password, database=database)
            conn.close()

        elif db_type in ("postgresql", "postgres"):
            import psycopg2
            port = int(port or 5432)
            conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=database)
            conn.close()

        elif db_type in ("sqlserver", "mssql"):
            import pyodbc
            port = port or 1433
            conn_str = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={host},{port};DATABASE={database};UID={user};PWD={password}"
            conn = pyodbc.connect(conn_str)
            conn.close()

        elif db_type in ("oracle", "oracledb"):
            import oracledb
            port = int(port or 1521)
            dsn = f"{host}:{port}/{database}"
            conn = oracledb.connect(user=user, password=password, dsn=dsn)
            conn.close()
        else:
            print(f"[错误] 不支持的数据库类型: {db_type}")
            return False

        print("[成功] 数据库连接测试通过！")
        return True

    except ImportError as e:
        # 缺驱动与连接失败分开提示，便于区分本机依赖问题和网络/认证问题。
        print(f"[失败] 缺少对应数据库驱动: {e}")
        print("  请安装: pip install pymysql / psycopg2-binary / pyodbc / oracledb")
        return False
    except Exception as e:
        print(f"[失败] 数据库连接失败: {e}")
        return False


if __name__ == "__main__":
    # 进程退出码 0 表示成功、1 表示失败，便于终端或其他脚本判断结果。
    success = test_connection()
    sys.exit(0 if success else 1)
