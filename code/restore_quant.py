"""在本地（Mac）用 pgserver 内嵌 PostgreSQL 还原 quant 库，无需安装 Postgres.app/Homebrew。

用法（在 quant-practice 目录内）：
    python restore_quant.py [dump路径]
默认 dump 为同目录的 quant_dump_v2_2026-10-08.sql.gz（分层 schema 版）。
流程：启动内嵌 PG（数据目录 ./pgdata）→ 建 quant 库 → 用内嵌 psql 灌 dump → 回查校验。
"""
import gzip
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PGDATA = ROOT / "pgdata"
DB = "quant"
DEFAULT_DUMP = ROOT / "quant_dump_v2_2026-10-08.sql.gz"


def main() -> int:
    dump = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DUMP
    if not dump.exists():
        print(f"找不到 dump 文件：{dump}")
        print("请先把 quant_dump_v2_2026-10-08.sql.gz 放到本目录再重跑。")
        return 1

    import pgserver
    import psycopg
    from pgserver.postgres_server import POSTGRES_BIN_PATH

    print(f"1/4 启动内嵌 PostgreSQL（数据目录 {PGDATA}）…")
    server = pgserver.get_server(str(PGDATA))

    print("2/4 创建 quant 库（如已存在则跳过）…")
    with psycopg.connect(server.get_uri(), autocommit=True) as c:
        exists = c.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DB,)).fetchone()
        if not exists:
            c.execute(f'CREATE DATABASE "{DB}"')

    print(f"3/4 灌入 {dump.name}（约 1-2 分钟）…")
    psql = POSTGRES_BIN_PATH / "psql"
    uri = server.get_uri(database=DB)
    with gzip.open(dump, "rb") as f:
        sql = f.read()
    proc = subprocess.run(
        [str(psql), uri, "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode(errors="replace")[-4000:])
        print("还原失败，报错见上。若提示表已存在，说明之前灌过一半："
              "删掉本目录 pgdata 文件夹后重跑即可。")
        return 1

    print("4/4 校验…")
    with psycopg.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO data, strategy, research, public")
            n_facts = cur.execute(
                "SELECT count(*) FROM data.financial_facts").fetchone()[0]
            tables = cur.execute(
                "SELECT table_schema, count(*) FROM information_schema.tables "
                "WHERE table_schema IN ('data','strategy','research') "
                "GROUP BY 1 ORDER BY 1").fetchall()
    print(f"  financial_facts 行数：{n_facts}（预期 69434）")
    for schema, n in tables:
        print(f"  schema {schema}: {n} 张表")
    ok = n_facts == 69434 and dict(tables) == {
        "data": 17, "research": 2, "strategy": 2}
    if not ok:
        print("校验数字对不上，把上面输出贴给 Muse 看。")
        return 1

    # 刷新连接信息文件（原包里的指向沙箱路径，本地必须重写）
    sys.path.insert(0, str(ROOT))
    import db
    uri_local = db.write_connection_file()
    print(f"\n还原成功。连接信息已写入 DB_CONNECTION.txt：\n  {uri_local}")
    print("以后在项目里 `from db import get_conn` 即可连库（会自动拉起内嵌 PG）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
