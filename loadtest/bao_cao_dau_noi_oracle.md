# Báo cáo kiểm chứng đấu nối Oracle
Sinh tự động bởi `loadtest/kiem_chung_oracle.py` lúc 06/08/2026 10:29

## Cấu hình đấu nối

| Tham số | Giá trị |
|---|---|
| DSN | localhost:1521/freepdb1 |
| User | SYSTEM |
| Schema | (mặc định của user) |
| Pool | 1–8 kết nối |
| call_timeout | 30.000 ms |

## Tổng hợp

**7/7 phép thử đạt.**

| Phép thử | Kết quả |
|---|---|
| Đấu nối và phiên bản máy chủ | ĐẠT |
| Độ trễ trước và sau khi pool ấm | ĐẠT |
| Không chặn vòng lặp sự kiện | ĐẠT |
| Truy vấn song song thật sự | ĐẠT |
| Phương ngữ SQL | ĐẠT |
| Câu lệnh không trả về bảng | ĐẠT |
| Cắt truy vấn quá hạn (call_timeout) | ĐẠT |

## Chi tiết

### Đấu nối và phiên bản máy chủ — ĐẠT

- Oracle AI Database 26ai Free Release 23.26.2.0.0 - Develop, Learn, and Run for Free
- chế độ oracledb: thin

### Độ trễ trước và sau khi pool ấm — ĐẠT

- lượt đầu (mở kết nối + truy vấn): 1.0 ms
- pool đã ấm, 20 lượt: trung vị 0.5 ms, p95 0.8 ms
- pool tiết kiệm được: 0.5 ms mỗi truy vấn

### Không chặn vòng lặp sự kiện — ĐẠT

- truy vấn ngủ 2.0s chạy thật: 2.01s
- độ trễ vòng lặp sự kiện: tối đa 6.4 ms trên 133 mẫu

### Truy vấn song song thật sự — ĐẠT

- 8 truy vấn ngủ 2.0s đồng thời: 2.61s
- nếu nối đuôi nhau sẽ là: 16.0s
- hệ số song song đạt được: 6.1x

### Phương ngữ SQL — ĐẠT

- [v] LIMIT 10 (cú pháp PostgreSQL hiện có trong main.py:408,424) -> lỗi: DatabaseError: ORA-03047: number '10' is not syntactically valid following 'SELECT 1 FROM dual LIMIT '
- [v] FETCH FIRST 10 ROWS ONLY (cú pháp Oracle thay thế) -> chạy được
- [v] information_schema.columns (prompt hiện dạy mô hình dùng, main.py:1270) -> lỗi: DatabaseError: ORA-00942: table or view "INFORMATION_SCHEMA"."COLUMNS" does not exist
- [v] ALL_TAB_COLUMNS (bảng từ điển thay thế trên Oracle) -> chạy được
- [v] ALTER SESSION SET CURRENT_SCHEMA (thay cho search_path, main.py:1128-1136) -> chạy được

### Câu lệnh không trả về bảng — ĐẠT

- khối PL/SQL trả về: cột ['rows_affected'], 1 dòng

### Cắt truy vấn quá hạn (call_timeout) — ĐẠT

- bị cắt sau 1.09s (đặt call_timeout 0,5s)
- lỗi trả về: DatabaseError: DPY-4011: the database or network closed the connection

---

Số liệu trên thay thế phần suy luận tĩnh trong `phan_tich_doi_sang_oracle.md`; những khẳng định nào bị số liệu bác bỏ thì phải sửa lại tài liệu đó.
