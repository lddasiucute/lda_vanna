"""Kiểm tra bộ phân loại câu hỏi trong / ngoài phạm vi dữ liệu.

`_off_topic_message` trong `main.py` quyết định câu nào được đưa xuống tầng SQL
và câu nào bị trả lời thẳng là ngoài phạm vi. Nó là một bộ phân loại theo từ
khoá, nên rất dễ hỏng âm thầm: nhận nhầm một câu dữ liệu thành câu linh tinh
thì người dùng mất hẳn tính năng, còn nhận nhầm chiều ngược lại thì mô hình bị
gọi vô ích cho những câu không liên quan.

Chạy:

    python loadtest/thu_phan_loai_cau_hoi.py

Không cần máy chủ, không cần cơ sở dữ liệu — chỉ nạp `main.py` rồi gọi hàm.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import main  # noqa: E402
from vanna.core.llm import LlmMessage, LlmRequest  # noqa: E402
from vanna.core.user import User  # noqa: E402

_svc = main.ChartAwareOllamaLlmService.__new__(main.ChartAwareOllamaLlmService)
_user = User(id="thu", email="thu@local", group_memberships=["read_qlsp"])


def phan_loai(cau: str) -> str | None:
    """None nghĩa là câu hỏi được coi là hỏi dữ liệu và sẽ đi xuống tầng SQL."""
    request = LlmRequest(
        messages=[LlmMessage(role="user", content=cau)], user=_user
    )
    return main.ChartAwareOllamaLlmService._off_topic_message(_svc, request)


# (câu hỏi, có phải câu hỏi dữ liệu không)
CA_THU = [
    # --- trong phạm vi ---
    ("Co bao nhieu nguoi dung trong he thong?", True),
    ("Dem so nguoi dung theo trang thai", True),
    ("Liet ke 5 don vi dau tien theo ten", True),
    ("Co bao nhieu su kien trong spdv_ticket_events?", True),
    ("Thong ke ticket theo muc uu tien", True),
    # Bản cũ nhận sai: từ khoá khai báo kèm dấu cách ở cuối nên trượt khi từ
    # đó đứng cuối câu.
    ("Co bao nhieu bang", True),
    ("Cho toi xem cac cot", True),
    ("Giup toi tim", True),
    ("Tinh tong", True),
    ("Dem", True),
    # Bản cũ thiếu hẳn các từ này.
    ("Doanh so thang nay the nao", True),
    ("Ty le ticket dong dung han", True),
    ("Top 10 don vi", True),
    ("Xep hang theo doanh thu", True),
    ("May nguoi dang hoat dong", True),
    # Có dấu đầy đủ vẫn phải nhận đúng.
    ("Có bao nhiêu bảng?", True),
    ("Thống kê ticket theo mức ưu tiên", True),
    ("Liệt kê đơn vị theo doanh số", True),
    # --- ngoài phạm vi ---
    ("Hom nay troi the nao?", False),
    ("Ke cho toi mot cau chuyen cuoi", False),
    ("Ke chuyen di", False),
    ("Ban co nguoi yeu chua?", False),
    ("Viet giup toi mot bai tho", False),
    # Bỏ dấu làm các từ này trùng với từ dữ liệu — đây là hai ca khó nhất:
    # "bằng" trùng "bảng", "tổng thống" trùng "tổng".
    ("2 + 2 bang may?", False),
    ("Ai la tong thong My?", False),
    ("Bang cap cua ban la gi", False),
]

LOI_CHAO = ["Chao", "Xin chao", "hello", "Chao ban!", "Hi", "hey"]
CAM_ON = ["Cam on", "cam on nhe", "thanks", "Thank you"]


def main_thu() -> int:
    sai = 0

    print(f"{'':2s} {'câu hỏi':46s} {'mong đợi':10s} {'thực tế':10s}")
    print("-" * 72)
    for cau, la_du_lieu in CA_THU:
        ket_qua = phan_loai(cau)
        thuc_te = "dữ liệu" if ket_qua is None else "ngoài"
        mong_doi = "dữ liệu" if la_du_lieu else "ngoài"
        khop = thuc_te == mong_doi
        sai += 0 if khop else 1
        print(f"{'  ' if khop else 'XX'} {cau[:44]:46s} {mong_doi:10s} {thuc_te:10s}")

    print("\n--- lời chào ---")
    for cau in LOI_CHAO:
        ket_qua = phan_loai(cau)
        khop = ket_qua is not None and "Chào bạn" in ket_qua
        sai += 0 if khop else 1
        print(f"{'  ' if khop else 'XX'} {cau:20s} -> {(ket_qua or 'None')[:56]}")

    print("\n--- cảm ơn ---")
    for cau in CAM_ON:
        ket_qua = phan_loai(cau)
        khop = ket_qua is not None and "Không có gì" in ket_qua
        sai += 0 if khop else 1
        print(f"{'  ' if khop else 'XX'} {cau:20s} -> {(ket_qua or 'None')[:56]}")

    tong = len(CA_THU) + len(LOI_CHAO) + len(CAM_ON)
    print(f"\nBackend đang cấu hình: {main.DATABASE_BACKEND}")
    print(f"{tong - sai}/{tong} ca đạt.")
    return 1 if sai else 0


if __name__ == "__main__":
    raise SystemExit(main_thu())
