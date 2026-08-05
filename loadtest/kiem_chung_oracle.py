"""Kiểm chứng đấu nối Oracle cho OracleRunner.

Chạy khi đã có một instance Oracle để đấu nối. Sinh ra báo cáo đấu nối tại
`loadtest/bao_cao_dau_noi_oracle.md`, kiểm chứng đúng những khẳng định trong
`phan_tich_doi_sang_oracle.md` — trước đó chúng chỉ là suy luận từ mã nguồn.

Cách chạy:

    set ORACLE_USER=...
    set ORACLE_PASSWORD=...
    set ORACLE_DSN=host:1521/service
    set ORACLE_SCHEMA=qlsp_backup          # tuỳ chọn
    python loadtest/kiem_chung_oracle.py

Bộ kiểm chứng chỉ đọc: mọi câu lệnh đều là SELECT hoặc lệnh mức phiên, không
tạo/sửa/xoá bất cứ đối tượng nào trong cơ sở dữ liệu.
"""

import asyncio
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from vanna.capabilities.sql_runner import RunSqlToolArgs  # noqa: E402
from vanna.integrations.oracle.sql_runner import OracleRunner  # noqa: E402

BAO_CAO = REPO_ROOT / "loadtest" / "bao_cao_dau_noi_oracle.md"

# Số người dùng đồng thời dùng cho phép thử song song. Đặt bằng max_connections
# để kiểm tra đúng trần của pool.
SO_KET_NOI_TOI_DA = 8
GIAY_NGU = 2.0
SO_LUOT_DO_TRE = 20


async def chay_sql(runner: OracleRunner, sql: str):
    """Gọi đúng đường mà máy chủ chat vẫn gọi.

    `OracleRunner.run_sql` không dùng tới `context`, nên truyền None là đủ để
    kiểm chứng tầng SQL mà không phải dựng cả User/AgentMemory.
    """
    return await runner.run_sql(RunSqlToolArgs(sql=sql), context=None)  # type: ignore[arg-type]


class KetQua:
    def __init__(self, ten: str):
        self.ten = ten
        self.dat: Optional[bool] = None
        self.chi_tiet: List[str] = []
        self.loi: Optional[str] = None

    def ghi(self, dong: str) -> None:
        self.chi_tiet.append(dong)
        print(f"    {dong}")

    @property
    def nhan(self) -> str:
        if self.loi is not None:
            return "LỖI"
        return "ĐẠT" if self.dat else "KHÔNG ĐẠT"


async def chay_phep_thu(ten: str, ham: Callable, *args) -> KetQua:
    kq = KetQua(ten)
    print(f"\n[{ten}]")
    try:
        await ham(kq, *args)
    except Exception as e:  # phép thử hỏng không được làm chết cả bộ kiểm chứng
        kq.loi = f"{type(e).__name__}: {e}"
        print(f"    !! {kq.loi}")
    print(f"    => {kq.nhan}")
    return kq


# ---------------------------------------------------------------- phép thử


async def pt_dau_noi(kq: KetQua, runner: OracleRunner) -> None:
    """Đấu nối được, và đọc ra phiên bản máy chủ."""
    df = await chay_sql(runner, "SELECT banner FROM v$version")
    for dong in df.iloc[:, 0].tolist():
        kq.ghi(str(dong))
    kq.ghi(f"chế độ oracledb: {'thin' if runner.oracledb.is_thin_mode() else 'thick'}")
    kq.dat = len(df) > 0


async def pt_do_tre_pool(kq: KetQua, runner: OracleRunner) -> None:
    """So độ trễ lượt đầu (mở kết nối) với các lượt sau (pool đã ấm).

    Đây là con số đối chứng trực tiếp với 0,105 giây đo được trên Postgres/Neon
    sau khi thêm pool.
    """
    t0 = time.perf_counter()
    await chay_sql(runner, "SELECT 1 FROM dual")
    nguoi = (time.perf_counter() - t0) * 1000
    kq.ghi(f"lượt đầu (mở kết nối + truy vấn): {nguoi:.1f} ms")

    do = []
    for _ in range(SO_LUOT_DO_TRE):
        t = time.perf_counter()
        await chay_sql(runner, "SELECT 1 FROM dual")
        do.append((time.perf_counter() - t) * 1000)

    do.sort()
    trung_vi = statistics.median(do)
    p95 = do[max(0, int(len(do) * 0.95) - 1)]
    kq.ghi(f"pool đã ấm, {SO_LUOT_DO_TRE} lượt: trung vị {trung_vi:.1f} ms, p95 {p95:.1f} ms")
    kq.ghi(f"pool tiết kiệm được: {nguoi - trung_vi:.1f} ms mỗi truy vấn")
    kq.dat = trung_vi < nguoi


async def _cau_lenh_ngu(runner: OracleRunner) -> str:
    """DBMS_SESSION.SLEEP có từ 18c; bản cũ hơn phải dùng DBMS_LOCK.SLEEP."""
    for goi in ("DBMS_SESSION", "DBMS_LOCK"):
        sql = f"BEGIN {goi}.SLEEP(0.1); END;"
        try:
            await chay_sql(runner, sql)
            return f"BEGIN {goi}.SLEEP({GIAY_NGU}); END;"
        except Exception:
            continue
    raise RuntimeError(
        "Không gọi được DBMS_SESSION.SLEEP lẫn DBMS_LOCK.SLEEP — "
        "user kiểm thử cần được GRANT EXECUTE một trong hai gói"
    )


async def pt_khong_chan_event_loop(kq: KetQua, runner: OracleRunner) -> None:
    """Đo độ trễ vòng lặp sự kiện trong lúc truy vấn chậm đang chạy.

    Đây là phép thử quan trọng nhất: nếu run_sql gọi thẳng thư viện đồng bộ
    (như bản OracleRunner cũ), toàn bộ vòng lặp đóng băng và độ trễ nhịp tim
    sẽ xấp xỉ thời gian truy vấn. Bản dùng thread pool phải giữ nó ở mức mili-giây.
    """
    sql_ngu = await _cau_lenh_ngu(runner)

    do_tre: List[float] = []
    dung = asyncio.Event()

    async def nhip_tim():
        while not dung.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            do_tre.append((time.perf_counter() - t - 0.01) * 1000)

    task_nhip = asyncio.create_task(nhip_tim())
    await asyncio.sleep(0.05)  # để nhịp tim chạy ổn định trước khi nạp tải

    t0 = time.perf_counter()
    await chay_sql(runner, sql_ngu)
    that_su = time.perf_counter() - t0

    dung.set()
    await task_nhip

    max_tre = max(do_tre) if do_tre else 0.0
    kq.ghi(f"truy vấn ngủ {GIAY_NGU}s chạy thật: {that_su:.2f}s")
    kq.ghi(f"độ trễ vòng lặp sự kiện: tối đa {max_tre:.1f} ms trên {len(do_tre)} mẫu")
    # Ngưỡng 200ms: rộng rãi so với mili-giây kỳ vọng, nhưng nhỏ hơn hẳn 2s
    # mà bản chặn vòng lặp sẽ tạo ra.
    kq.dat = max_tre < 200
    if not kq.dat:
        kq.ghi("CẢNH BÁO: vòng lặp sự kiện bị chặn — truy vấn không được đẩy sang thread pool")


async def pt_song_song(kq: KetQua, runner: OracleRunner) -> None:
    """N truy vấn chậm đồng thời phải mất ~thời gian của một truy vấn, không phải N lần."""
    sql_ngu = await _cau_lenh_ngu(runner)
    n = SO_KET_NOI_TOI_DA

    t0 = time.perf_counter()
    await asyncio.gather(*(chay_sql(runner, sql_ngu) for _ in range(n)))
    tong = time.perf_counter() - t0

    noi_duoi = n * GIAY_NGU
    kq.ghi(f"{n} truy vấn ngủ {GIAY_NGU}s đồng thời: {tong:.2f}s")
    kq.ghi(f"nếu nối đuôi nhau sẽ là: {noi_duoi:.1f}s")
    kq.ghi(f"hệ số song song đạt được: {noi_duoi / tong:.1f}x")
    kq.dat = tong < noi_duoi / 2


async def pt_phuong_ngu(kq: KetQua, runner: OracleRunner) -> None:
    """Kiểm chứng bảng phương ngữ SQL ở Mục 4.2 của tài liệu phân tích."""
    phep: List[Dict[str, Any]] = [
        {
            "ten": "LIMIT 10 (cú pháp PostgreSQL hiện có trong main.py:408,424)",
            "sql": "SELECT 1 FROM dual LIMIT 10",
            "mong_doi_loi": True,
        },
        {
            "ten": "FETCH FIRST 10 ROWS ONLY (cú pháp Oracle thay thế)",
            "sql": "SELECT 1 FROM dual FETCH FIRST 10 ROWS ONLY",
            "mong_doi_loi": False,
        },
        {
            "ten": "information_schema.columns (prompt hiện dạy mô hình dùng, main.py:1270)",
            "sql": "SELECT COUNT(*) FROM information_schema.columns",
            "mong_doi_loi": True,
        },
        {
            "ten": "ALL_TAB_COLUMNS (bảng từ điển thay thế trên Oracle)",
            "sql": "SELECT COUNT(*) FROM all_tab_columns",
            "mong_doi_loi": False,
        },
        {
            "ten": "ALTER SESSION SET CURRENT_SCHEMA (thay cho search_path, main.py:1128-1136)",
            "sql": f"ALTER SESSION SET CURRENT_SCHEMA = {os.environ.get('ORACLE_SCHEMA') or os.environ.get('ORACLE_USER')}",
            "mong_doi_loi": False,
        },
    ]

    dung_het = True
    for p in phep:
        try:
            await chay_sql(runner, p["sql"])
            co_loi = False
            ghi_chu = "chạy được"
        except Exception as e:
            co_loi = True
            ghi_chu = f"lỗi: {type(e).__name__}: {str(e).splitlines()[0][:90]}"

        khop = co_loi == p["mong_doi_loi"]
        dung_het = dung_het and khop
        dau = "v" if khop else "x"
        kq.ghi(f"[{dau}] {p['ten']} -> {ghi_chu}")

    kq.dat = dung_het


async def pt_cau_lenh_khong_tra_bang(kq: KetQua, runner: OracleRunner) -> None:
    """Chốt chặn cursor.description is None — bản cũ ném TypeError ở đây."""
    df = await chay_sql(runner, "BEGIN NULL; END;")
    kq.ghi(f"khối PL/SQL trả về: cột {list(df.columns)}, {len(df)} dòng")
    kq.dat = "rows_affected" in df.columns


async def pt_call_timeout(kq: KetQua, runner: OracleRunner) -> None:
    """Oracle không có statement_timeout phía máy chủ; kiểm chứng call_timeout thay thế.

    Dùng runner riêng với timeout ngắn để không đụng vào pool chính.
    """
    sql_ngu = await _cau_lenh_ngu(runner)
    ngan = OracleRunner(
        user=runner.user,
        password=runner.password,
        dsn=runner.dsn,
        min_connections=1,
        max_connections=1,
        call_timeout_ms=500,
        current_schema=runner.current_schema,
    )
    try:
        t0 = time.perf_counter()
        try:
            await chay_sql(ngan, sql_ngu)
            kq.ghi(f"truy vấn ngủ {GIAY_NGU}s KHÔNG bị cắt — call_timeout không có tác dụng")
            kq.dat = False
        except Exception as e:
            mat = time.perf_counter() - t0
            kq.ghi(f"bị cắt sau {mat:.2f}s (đặt call_timeout 0,5s)")
            kq.ghi(f"lỗi trả về: {type(e).__name__}: {str(e).splitlines()[0][:90]}")
            kq.dat = mat < GIAY_NGU
    finally:
        ngan.close()


# ---------------------------------------------------------------- báo cáo


def viet_bao_cao(ket_qua: List[KetQua], cau_hinh: Dict[str, str]) -> None:
    d = []
    d.append("# Báo cáo kiểm chứng đấu nối Oracle\n")
    d.append(f"Sinh tự động bởi `loadtest/kiem_chung_oracle.py` lúc "
             f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
    d.append("\n## Cấu hình đấu nối\n\n")
    d.append("| Tham số | Giá trị |\n|---|---|\n")
    for k, v in cau_hinh.items():
        d.append(f"| {k} | {v} |\n")

    so_dat = sum(1 for k in ket_qua if k.dat and k.loi is None)
    d.append(f"\n## Tổng hợp\n\n**{so_dat}/{len(ket_qua)} phép thử đạt.**\n\n")
    d.append("| Phép thử | Kết quả |\n|---|---|\n")
    for k in ket_qua:
        d.append(f"| {k.ten} | {k.nhan} |\n")

    d.append("\n## Chi tiết\n")
    for k in ket_qua:
        d.append(f"\n### {k.ten} — {k.nhan}\n\n")
        if k.loi:
            d.append(f"```\n{k.loi}\n```\n")
        for dong in k.chi_tiet:
            d.append(f"- {dong}\n")

    d.append("\n---\n\nSố liệu trên thay thế phần suy luận tĩnh trong "
             "`phan_tich_doi_sang_oracle.md`; những khẳng định nào bị số liệu "
             "bác bỏ thì phải sửa lại tài liệu đó.\n")

    BAO_CAO.write_text("".join(d), encoding="utf-8")
    print(f"\nĐã ghi báo cáo: {BAO_CAO}")


async def main() -> int:
    user = os.environ.get("ORACLE_USER")
    password = os.environ.get("ORACLE_PASSWORD")
    dsn = os.environ.get("ORACLE_DSN")
    schema = os.environ.get("ORACLE_SCHEMA")

    if not (user and password and dsn):
        print(
            "Thiếu thông tin đấu nối. Cần đặt ORACLE_USER, ORACLE_PASSWORD, "
            "ORACLE_DSN (và ORACLE_SCHEMA nếu có).",
            file=sys.stderr,
        )
        return 2

    runner = OracleRunner(
        user=user,
        password=password,
        dsn=dsn,
        min_connections=1,
        max_connections=SO_KET_NOI_TOI_DA,
        call_timeout_ms=30000,
        current_schema=schema,
    )

    ket_qua: List[KetQua] = []
    try:
        ket_qua.append(await chay_phep_thu("Đấu nối và phiên bản máy chủ", pt_dau_noi, runner))
        ket_qua.append(await chay_phep_thu("Độ trễ trước và sau khi pool ấm", pt_do_tre_pool, runner))
        ket_qua.append(await chay_phep_thu("Không chặn vòng lặp sự kiện", pt_khong_chan_event_loop, runner))
        ket_qua.append(await chay_phep_thu("Truy vấn song song thật sự", pt_song_song, runner))
        ket_qua.append(await chay_phep_thu("Phương ngữ SQL", pt_phuong_ngu, runner))
        ket_qua.append(await chay_phep_thu("Câu lệnh không trả về bảng", pt_cau_lenh_khong_tra_bang, runner))
        ket_qua.append(await chay_phep_thu("Cắt truy vấn quá hạn (call_timeout)", pt_call_timeout, runner))
    finally:
        runner.close()

    viet_bao_cao(
        ket_qua,
        {
            "DSN": dsn,
            "User": user,
            "Schema": schema or "(mặc định của user)",
            "Pool": f"1–{SO_KET_NOI_TOI_DA} kết nối",
            "call_timeout": "30.000 ms",
        },
    )

    that_bai = [k for k in ket_qua if not k.dat or k.loi]
    print(f"\n{len(ket_qua) - len(that_bai)}/{len(ket_qua)} phép thử đạt.")
    return 1 if that_bai else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
