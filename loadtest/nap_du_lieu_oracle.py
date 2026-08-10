"""Nạp schema qlsp_backup từ Postgres/Neon sang Oracle.

Tạo user QLSP_BACKUP trên Oracle, dựng lại 94 bảng theo cấu trúc nguồn rồi
chép toàn bộ dữ liệu. Chạy lại được nhiều lần: mỗi lượt xoá bảng cũ rồi tạo lại.

Cách chạy:

    set ORACLE_ADMIN_USER=SYSTEM
    set ORACLE_ADMIN_PASSWORD=...
    set ORACLE_DSN=localhost:1521/freepdb1
    set QLSP_ORACLE_PASSWORD=...           # mật khẩu cho user QLSP_BACKUP
    python loadtest/nap_du_lieu_oracle.py

DATABASE_URL của Postgres nguồn đọc từ .env cạnh main.py.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import oracledb  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

SO_DONG_MOI_LO = 500

# Từ khoá Oracle không dùng làm tên cột không có dấu nháy được.
TU_KHOA_ORACLE = {
    "ACCESS", "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUDIT",
    "BETWEEN", "BY", "CHAR", "CHECK", "CLUSTER", "COLUMN", "COMMENT",
    "COMPRESS", "CONNECT", "CREATE", "CURRENT", "DATE", "DECIMAL", "DEFAULT",
    "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "EXCLUSIVE", "EXISTS",
    "FILE", "FLOAT", "FOR", "FROM", "GRANT", "GROUP", "HAVING", "IDENTIFIED",
    "IMMEDIATE", "IN", "INCREMENT", "INDEX", "INITIAL", "INSERT", "INTEGER",
    "INTERSECT", "INTO", "IS", "LEVEL", "LIKE", "LOCK", "LONG", "MAXEXTENTS",
    "MINUS", "MLSLABEL", "MODE", "MODIFY", "NOAUDIT", "NOCOMPRESS", "NOT",
    "NOWAIT", "NULL", "NUMBER", "OF", "OFFLINE", "ON", "ONLINE", "OPTION",
    "OR", "ORDER", "PCTFREE", "PRIOR", "PUBLIC", "RAW", "RENAME", "RESOURCE",
    "REVOKE", "ROW", "ROWID", "ROWNUM", "ROWS", "SELECT", "SESSION", "SET",
    "SHARE", "SIZE", "SMALLINT", "START", "SUCCESSFUL", "SYNONYM", "SYSDATE",
    "TABLE", "THEN", "TO", "TRIGGER", "UID", "UNION", "UNIQUE", "UPDATE",
    "USER", "VALIDATE", "VALUES", "VARCHAR", "VARCHAR2", "VIEW", "WHENEVER",
    "WHERE", "WITH",
}


def dsn_truc_tiep(url: str) -> str:
    """Endpoint trực tiếp của Neon; pooler từ chối tham số khởi động search_path."""
    p = urlsplit(url)
    q = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.casefold() != "options"
    ]
    return urlunsplit(
        (p.scheme, p.netloc.replace("-pooler.", "."), p.path, urlencode(q), p.fragment)
    )


def doc_env() -> dict:
    env = {}
    for dong in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        dong = dong.strip()
        if dong and not dong.startswith("#") and "=" in dong:
            k, v = dong.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ------------------------------------------------------------ ánh xạ kiểu


def kieu_oracle(cot: dict, do_dai_toi_da: int) -> str:
    """Ánh xạ kiểu Postgres sang Oracle.

    `do_dai_toi_da` là độ dài chuỗi dài nhất thực tế trong cột, dùng để quyết
    định VARCHAR2 hay CLOB — VARCHAR2 tối đa 4000 byte, mà dữ liệu tiếng Việt
    dùng UTF-8 nên một ký tự có thể chiếm 3 byte.
    """
    t = cot["data_type"]

    if t in ("text", "character varying", "USER-DEFINED", "ARRAY", "jsonb", "json"):
        if t in ("jsonb", "json", "ARRAY"):
            # Lưu nguyên văn JSON; Oracle 23ai có kiểu JSON nhưng CLOB đủ dùng
            # và không ràng buộc tầng ứng dụng phải biết kiểu riêng.
            return "CLOB" if do_dai_toi_da > 1000 else "VARCHAR2(4000)"
        if do_dai_toi_da == 0:
            return "VARCHAR2(255)"
        if do_dai_toi_da * 3 > 3900:
            return "CLOB"
        return f"VARCHAR2({max(1, min(4000, do_dai_toi_da * 3))})"

    if t == "character":
        n = cot.get("character_maximum_length") or 1
        return f"CHAR({n})"
    if t == "uuid":
        return "VARCHAR2(36)"
    if t == "boolean":
        # NUMBER(1) thay vì BOOLEAN: chạy được trên mọi bản Oracle và mọi driver.
        return "NUMBER(1)"
    if t == "smallint":
        return "NUMBER(5)"
    if t == "integer":
        return "NUMBER(10)"
    if t == "bigint":
        return "NUMBER(19)"
    if t == "real":
        return "BINARY_FLOAT"
    if t == "double precision":
        return "BINARY_DOUBLE"
    if t == "numeric":
        p, s = cot.get("numeric_precision"), cot.get("numeric_scale")
        if p:
            return f"NUMBER({p},{s or 0})"
        return "NUMBER"
    if t == "date":
        return "DATE"
    if t == "timestamp without time zone":
        return "TIMESTAMP"
    if t == "timestamp with time zone":
        return "TIMESTAMP WITH TIME ZONE"
    if t.startswith("time"):
        return "VARCHAR2(64)"

    return "VARCHAR2(4000)"


def ten_cot_oracle(ten: str) -> str:
    """Bọc dấu nháy nếu trùng từ khoá — nhưng báo lên vì SQL sinh ra sẽ phải bọc theo."""
    if ten.upper() in TU_KHOA_ORACLE:
        return f'"{ten.upper()}"'
    return ten


def chuyen_gia_tri(v):
    """Đưa giá trị Postgres về dạng oracledb nhận được."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return v


# ------------------------------------------------------------ chương trình


def main() -> int:
    env = doc_env()
    pg_dsn = dsn_truc_tiep(env["DATABASE_URL"])
    schema_nguon = env.get("DATABASE_SCHEMA", "qlsp_backup")

    admin_user = os.environ.get("ORACLE_ADMIN_USER", "SYSTEM")
    admin_pw = os.environ.get("ORACLE_ADMIN_PASSWORD")
    ora_dsn = os.environ.get("ORACLE_DSN", "localhost:1521/freepdb1")
    qlsp_pw = os.environ.get("QLSP_ORACLE_PASSWORD")

    if not admin_pw or not qlsp_pw:
        print(
            "Thiếu ORACLE_ADMIN_PASSWORD hoặc QLSP_ORACLE_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    print(f"Nguồn : Postgres, schema {schema_nguon}")
    print(f"Đích  : Oracle {ora_dsn}, user QLSP_BACKUP")

    pg = psycopg2.connect(pg_dsn)
    pgc = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    pgc.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema_nguon,),
    )
    bang = [r["table_name"] for r in pgc.fetchall()]

    pgc.execute(
        """
        SELECT table_name, column_name, ordinal_position, data_type,
               character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema_nguon,),
    )
    cot_theo_bang: dict = {}
    for r in pgc.fetchall():
        cot_theo_bang.setdefault(r["table_name"], []).append(dict(r))

    print(f"Tìm thấy {len(bang)} bảng.")

    # --- tạo user đích
    admin = oracledb.connect(user=admin_user, password=admin_pw, dsn=ora_dsn)
    ac = admin.cursor()
    try:
        ac.execute("DROP USER qlsp_backup CASCADE")
        print("Đã xoá user qlsp_backup cũ.")
    except oracledb.DatabaseError:
        pass
    ac.execute(f'CREATE USER qlsp_backup IDENTIFIED BY "{qlsp_pw}"')
    ac.execute("GRANT CONNECT, RESOURCE, CREATE VIEW TO qlsp_backup")
    ac.execute("ALTER USER qlsp_backup QUOTA UNLIMITED ON USERS")
    admin.commit()
    ac.close()
    admin.close()
    print("Đã tạo user QLSP_BACKUP.")

    ora = oracledb.connect(user="qlsp_backup", password=qlsp_pw, dsn=ora_dsn)
    oc = ora.cursor()

    tu_khoa_gap = []
    tong_dong = 0
    bang_loi = []

    for i, t in enumerate(bang, 1):
        cots = cot_theo_bang.get(t, [])
        if not cots:
            continue

        # đo độ dài thực tế của các cột chuỗi để chọn VARCHAR2 hay CLOB
        do_dai = {}
        chuoi = [
            c["column_name"]
            for c in cots
            if c["data_type"] in ("text", "character varying", "USER-DEFINED",
                                  "ARRAY", "jsonb", "json")
        ]
        if chuoi:
            biu = ", ".join(
                f'COALESCE(MAX(LENGTH("{c}"::text)), 0) AS "{c}"' for c in chuoi
            )
            pgc.execute(f'SELECT {biu} FROM "{schema_nguon}"."{t}"')
            do_dai = dict(pgc.fetchone() or {})

        dinh_nghia = []
        for c in cots:
            ten = ten_cot_oracle(c["column_name"])
            if ten.startswith('"'):
                tu_khoa_gap.append(f"{t}.{c['column_name']}")
            dinh_nghia.append(
                f"  {ten} {kieu_oracle(c, int(do_dai.get(c['column_name']) or 0))}"
            )

        ddl = f"CREATE TABLE {t} (\n" + ",\n".join(dinh_nghia) + "\n)"
        try:
            oc.execute(ddl)
        except oracledb.DatabaseError as e:
            bang_loi.append((t, f"DDL: {e}"))
            print(f"  [{i:2d}/{len(bang)}] {t:40s} LỖI DDL: {str(e).splitlines()[0]}")
            continue

        # --- chép dữ liệu
        ten_cots = [c["column_name"] for c in cots]
        chon = ", ".join(f'"{c}"' for c in ten_cots)
        pgc.execute(f'SELECT {chon} FROM "{schema_nguon}"."{t}"')

        dich_cots = ", ".join(ten_cot_oracle(c) for c in ten_cots)
        cho = ", ".join(f":{n + 1}" for n in range(len(ten_cots)))
        chen = f"INSERT INTO {t} ({dich_cots}) VALUES ({cho})"

        n = 0
        while True:
            lo = pgc.fetchmany(SO_DONG_MOI_LO)
            if not lo:
                break
            du_lieu = [
                tuple(chuyen_gia_tri(r[c]) for c in ten_cots) for r in lo
            ]
            try:
                oc.executemany(chen, du_lieu)
                n += len(du_lieu)
            except oracledb.DatabaseError as e:
                bang_loi.append((t, f"INSERT: {e}"))
                print(f"  [{i:2d}/{len(bang)}] {t:40s} LỖI chèn: {str(e).splitlines()[0]}")
                break
        ora.commit()
        tong_dong += n
        print(f"  [{i:2d}/{len(bang)}] {t:40s} {n:6,d} dòng")

    # --- đối chiếu số dòng hai bên
    print("\n=== Đối chiếu số dòng ===")
    lech = []
    for t in bang:
        pgc.execute(f'SELECT COUNT(*) AS n FROM "{schema_nguon}"."{t}"')
        a = pgc.fetchone()["n"]
        try:
            oc.execute(f"SELECT COUNT(*) FROM {t}")
            b = oc.fetchone()[0]
        except oracledb.DatabaseError:
            b = -1
        if a != b:
            lech.append((t, a, b))

    if lech:
        print(f"LỆCH ở {len(lech)} bảng:")
        for t, a, b in lech:
            print(f"  {t:40s} Postgres {a:6,d}  Oracle {b:6,d}")
    else:
        print(f"Khớp toàn bộ {len(bang)} bảng, tổng {tong_dong:,} dòng.")

    if tu_khoa_gap:
        print(f"\nCẢNH BÁO: {len(tu_khoa_gap)} cột trùng từ khoá Oracle, đã bọc nháy kép:")
        for x in tu_khoa_gap[:20]:
            print(f"  {x}")
        print("  SQL sinh ra phải bọc nháy các cột này, nếu không sẽ lỗi.")

    if bang_loi:
        print(f"\n{len(bang_loi)} bảng lỗi:")
        for t, e in bang_loi[:20]:
            print(f"  {t}: {str(e).splitlines()[0][:110]}")

    oc.close()
    ora.close()
    pgc.close()
    pg.close()
    return 1 if (lech or bang_loi) else 0


if __name__ == "__main__":
    raise SystemExit(main())
