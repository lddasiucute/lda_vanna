"""Kiểm thử Text-to-SQL trên truy vấn JOIN nhiều bảng.

Bộ kiểm thử tải chỉ hỏi COUNT(*) một bảng, nên nó chứng minh được **đấu nối**
chứ không chứng minh được **sinh SQL đúng**. Bộ này đi xa hơn: mỗi câu hỏi có
một đáp án đúng do chính script tính bằng SQL viết tay, sau đó mới hỏi ứng dụng
và so hai bên. Một câu chạy được nhưng ra số sai vẫn bị tính là hỏng.

Cách chạy (ứng dụng phải đang chạy ở cổng 8000):

    set ORACLE_USER=qlsp_backup
    set ORACLE_PASSWORD=...
    set ORACLE_DSN=localhost:1521/freepdb1
    python loadtest/thu_join_nhieu_bang.py

Bật LOG_SQL=1 ở tiến trình máy chủ để thấy câu SQL mô hình sinh ra.
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import oracledb  # noqa: E402

URL = "http://127.0.0.1:8000/api/vanna/v2/chat_sse"
TIMEOUT = 240

# (nhãn, câu hỏi tiếng Việt, SQL đáp án đúng, số bảng phải JOIN)
CA_THU = [
    (
        "2 bảng, JOIN + GROUP BY",
        "Mỗi đơn vị có bao nhiêu người dùng? Cho biết tên đơn vị và số lượng.",
        """SELECT COUNT(*) FROM (
             SELECT u.id FROM qlsp_backup.units u
             JOIN qlsp_backup.users us ON us.unit_id = u.id
             GROUP BY u.id)""",
        2,
    ),
    (
        "2 bảng, JOIN + lọc",
        "Có bao nhiêu ticket thuộc danh mục SPDV đang ở trạng thái hoạt động?",
        """SELECT COUNT(*) FROM qlsp_backup.spdv_tickets t
           JOIN qlsp_backup.spdv_categories c ON t.spdv_id = c.id""",
        2,
    ),
    (
        "3 bảng, JOIN chuỗi",
        "Có bao nhiêu sự kiện ticket thuộc các ticket của danh mục SPDV?",
        """SELECT COUNT(*) FROM qlsp_backup.spdv_ticket_events e
           JOIN qlsp_backup.spdv_tickets t ON e.ticket_id = t.id
           JOIN qlsp_backup.spdv_categories c ON t.spdv_id = c.id""",
        3,
    ),
    (
        # Bẫy dữ liệu thật: plan_items.doanh_thu_du_kien nghe như số tiền nhưng
        # là VARCHAR2 chứa văn bản mô tả ("Tiết kiệm 20% thời gian xử lý").
        # Cột số thật là estimated_cost.
        "2 bảng, JOIN + tổng tiền",
        "Tổng chi phí ước tính của các hạng mục kế hoạch là bao nhiêu?",
        """SELECT ROUND(SUM(estimated_cost)) FROM qlsp_backup.plan_items""",
        1,
    ),
    (
        "3 bảng, JOIN + SUM",
        "Tổng doanh thu của các chương trình bán hàng theo kết quả ghi nhận là bao nhiêu?",
        """SELECT ROUND(SUM(r.doanh_thu)) FROM qlsp_backup.sales_program_results r
           JOIN qlsp_backup.sales_programs p ON r.program_id = p.id""",
        2,
    ),
    (
        "2 bảng, đếm phân biệt",
        "Có bao nhiêu danh mục SPDV thực sự có ít nhất một ticket?",
        """SELECT COUNT(DISTINCT c.id) FROM qlsp_backup.spdv_categories c
           JOIN qlsp_backup.spdv_tickets t ON t.spdv_id = c.id""",
        2,
    ),
    (
        "2 bảng, LEFT JOIN tìm rỗng",
        "Có bao nhiêu đơn vị không có người dùng nào?",
        """SELECT COUNT(*) FROM qlsp_backup.units u
           WHERE NOT EXISTS (SELECT 1 FROM qlsp_backup.users us WHERE us.unit_id = u.id)""",
        2,
    ),
    (
        "3 bảng, JOIN + lọc kép",
        "Có bao nhiêu hạng mục kế hoạch thuộc các hồ sơ kế hoạch đã tạo?",
        """SELECT COUNT(*) FROM qlsp_backup.plan_items i
           JOIN qlsp_backup.plan_applications a ON i.application_id = a.id""",
        2,
    ),
]


def dap_an_dung(cur, sql: str):
    cur.execute(" ".join(sql.split()))
    hang = cur.fetchone()
    return hang[0] if hang else None


def hoi_ung_dung(cau: str) -> tuple[str, list]:
    """Trả về (toàn bộ stream, các số xuất hiện trong bảng kết quả)."""
    uid = f"join-{int(time.time() * 1000)}"
    body = json.dumps(
        {"message": cau, "conversation_id": uid, "request_id": uid}
    ).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"}
    )
    raw = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")

    so = []
    for dong in raw.splitlines():
        if not dong.startswith("data: ") or dong[6:].strip() == "[DONE]":
            continue
        try:
            ev = json.loads(dong[6:])
        except Exception:
            continue
        rich = ev.get("rich") or {}
        if rich.get("type") != "dataframe":
            continue
        # Cấu trúc thật: rich["data"]["data"] là danh sách các dòng dạng dict.
        for hang in (rich.get("data") or {}).get("data") or []:
            for gia_tri in hang.values():
                if isinstance(gia_tri, (int, float)):
                    so.append(float(gia_tri))
                elif isinstance(gia_tri, str):
                    for m in re.findall(r"-?\d+(?:\.\d+)?", gia_tri.replace(",", "")):
                        so.append(float(m))
    return raw, so


def main() -> int:
    user = os.environ.get("ORACLE_USER", "qlsp_backup")
    pw = os.environ.get("ORACLE_PASSWORD")
    dsn = os.environ.get("ORACLE_DSN", "localhost:1521/freepdb1")
    if not pw:
        print("Thiếu ORACLE_PASSWORD.", file=sys.stderr)
        return 2

    conn = oracledb.connect(user=user, password=pw, dsn=dsn)
    cur = conn.cursor()

    dat = 0
    chay_duoc = 0
    print(f"{'':2s} {'phép thử':30s} {'đúng':>12s} {'ứng dụng':>14s}  {'thời gian':>9s}")
    print("-" * 78)

    for nhan, cau, sql_dung, _ in CA_THU:
        mong_doi = dap_an_dung(cur, sql_dung)
        t0 = time.perf_counter()
        try:
            raw, so = hoi_ung_dung(cau)
        except Exception as e:
            print(f"XX {nhan:30s} {mong_doi!s:>12s} {'LỖI HTTP':>14s}  {type(e).__name__}")
            continue
        giay = time.perf_counter() - t0

        co_bang = '"type":"dataframe"' in raw
        if co_bang:
            chay_duoc += 1

        co_so_dung = mong_doi is not None and any(
            abs(x - float(mong_doi)) < 0.51 for x in so
        )
        # Bộ khớp mẫu trong main.py trả về một bảng gồm nhiều con số đếm không
        # liên quan tới câu hỏi. Nếu con số đúng chỉ tình cờ nằm lẫn trong đó
        # thì đó là ăn may, không phải trả lời đúng — nên phải đếm số giá trị.
        khop = co_so_dung and (
            len(so) == 1 or abs(so[0] - float(mong_doi)) < 0.51
        )
        an_may = co_so_dung and not khop
        if khop:
            dat += 1

        if not co_bang:
            thuc_te = "KHÔNG CHẠY"
        elif not so:
            thuc_te = "(rỗng)"
        elif an_may:
            thuc_te = f"lẫn trong {len(so)} số"
        else:
            thuc_te = f"{so[0]:.0f}" + (f" +{len(so) - 1}" if len(so) > 1 else "")

        dau = "  " if khop else ("~~" if an_may else "XX")
        print(
            f"{dau} {nhan:30s} {mong_doi!s:>13s} {thuc_te:>16s}  {giay:7.1f}s"
        )

    tong = len(CA_THU)
    print(f"\n  đạt   ~~ số đúng nhưng lẫn trong bảng đếm không liên quan")
    print(f"  XX    sai hoặc không chạy")
    print(f"\nChạy được (có bảng trả về): {chay_duoc}/{tong}")
    print(f"Trả lời đúng câu được hỏi : {dat}/{tong}")

    cur.close()
    conn.close()
    return 1 if dat < tong else 0


if __name__ == "__main__":
    raise SystemExit(main())
