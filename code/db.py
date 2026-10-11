"""PostgreSQL 入口：用户单独项目也走这里连库。

库由 pgserver 托管，数据目录 ~/workspace/quant-practice/pgdata（随家目录持久化）。
服务器没起时 get_conn() 会自动拉起。连接信息写入 pgdata/../DB_CONNECTION.txt，
用户自建项目可按那里的 URI 自行连接（psycopg / SQLAlchemy / psql 均可）。
"""
import pathlib

import pgserver
import psycopg

ROOT = pathlib.Path(__file__).resolve().parent
PGDATA = ROOT / "pgdata"
_DB = "quant"

_server = None


def ensure_server():
    global _server
    if _server is None:
        _server = pgserver.get_server(str(PGDATA))
    return _server


def admin_uri() -> str:
    return ensure_server().get_uri()


def get_conn(db: str = _DB):
    """连到 quant 库（不存在则创建）。表按三层 schema 分放：
    data（原料）/ strategy（策略产出）/ research（研究结论），
    连接时设好 search_path，脚本里可继续裸写表名。"""
    srv = ensure_server()
    with psycopg.connect(srv.get_uri(), autocommit=True) as c:
        exists = c.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone()
        if not exists:
            c.execute(f'CREATE DATABASE "{db}"')
    conn = psycopg.connect(srv.get_uri(database=db))
    with conn.cursor() as cur:
        cur.execute("SET search_path TO data, strategy, research, public")
    conn.commit()
    return conn


def write_connection_file():
    uri = ensure_server().get_uri(database=_DB)
    (ROOT / "DB_CONNECTION.txt").write_text(
        "# quant 库连接信息（psycopg / SQLAlchemy 通用）\n"
        f"QUANT_PG_URI={uri}\n"
        f"PGDATA={PGDATA}\n"
    )
    return uri


if __name__ == "__main__":
    conn = get_conn()
    write_connection_file()
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        print(cur.fetchone()[0])
    conn.close()
    print("DB 就绪:", ensure_server().get_uri(database=_DB))
