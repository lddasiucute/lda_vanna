# Phân tích: có nên chuyển cơ sở dữ liệu sang Oracle?

Ngày 05/08/2026 — Lê Đức Anh, Trung tâm DMS

---

## Kết luận ngắn

**Nếu lý do là hiệu năng: không nên.** Cơ sở dữ liệu hiện chiếm dưới 1% độ trễ
của hệ thống. Đổi sang Oracle là tối ưu vào chỗ không đau, và nếu triển khai
bằng mã nguồn hiện có thì hệ thống sẽ **chậm đi**, không nhanh lên.

**Nếu lý do là yêu cầu nghiệp vụ** (dữ liệu nằm sẵn trên Oracle, hoặc quy định
bắt buộc dùng Oracle): làm được, nhưng phải sửa ba nhóm việc trước, ước tính
2–3 ngày công. Chi tiết ở Mục 4.

---

## 1. Cơ sở dữ liệu không phải nút thắt

Hệ thống có hai nhánh xử lý tách biệt. Năm trong sáu câu hỏi của bộ kiểm thử
được nhận dạng theo mẫu và sinh thẳng câu lệnh SQL, **không gọi mô hình ngôn
ngữ**. Chỉ một câu đi qua tầng suy luận.

Số liệu đo tại mức 50 người dùng đồng thời, cấu hình tốt nhất (1.140 request,
100% trả về dữ liệu thật):

| Nhánh | Số lần | Trung vị | p95 |
|---|---:|---:|---:|
| Năm câu chỉ chạm Postgres | 954 | 0,10 – 0,11 giây | 0,24 – 0,37 giây |
| Câu gọi mô hình ngôn ngữ | 186 | 45,33 giây | 47,79 giây |

Nói cách khác, **toàn bộ độ trễ của hệ thống nằm ở tầng suy luận**. Cơ sở dữ
liệu trả lời trong một phần tư giây ngay cả khi có 50 người dùng cùng lúc.

Phép thử ngược cho thấy rõ giới hạn của việc đổi DB: giả sử Oracle nhanh **vô
hạn**, tức mọi truy vấn trả về trong 0 giây, thì p95 tổng thể của hệ thống chỉ
giảm từ 46,34 xuống khoảng 46,2 giây — cải thiện **dưới 1%**.

Để so sánh, hai thay đổi đã thực hiện trong đợt tối ưu vừa rồi, **không đổi cơ
sở dữ liệu**, cho kết quả:

| Thay đổi | Hiệu quả |
|---|---|
| Connection pool + bỏ chặn vòng lặp sự kiện | Thông lượng +533% |
| Đặt `OLLAMA_NUM_PARALLEL`=4 | Thông lượng +37% |
| Bật GPU tích hợp Radeon 780M | Thông lượng +93% (ở mức 30 người dùng) |

Ba việc này đều miễn phí và đều tác động vào đúng nút thắt.

---

## 2. Lợi ích duy nhất có thật: khoảng cách địa lý

Cơ sở dữ liệu hiện đặt trên Neon, region `ap-southeast-1` (Singapore). Mỗi truy
vấn phải đi trọn một vòng quốc tế, đo được **0,105 giây** khi connection pool đã
ấm.

Một instance Oracle đặt tại chỗ (on-prem) sẽ cắt phần lớn con số này, có thể
xuống dưới 10 mili-giây. Đây là lợi ích thật, không phải suy đoán.

Nhưng cần đặt đúng tỉ lệ: đó là tiết kiệm khoảng **0,1 giây trong một request
mất 45 giây**. Người dùng sẽ không cảm nhận được.

Lợi ích này chỉ trở nên đáng kể nếu tập câu hỏi thực tế nghiêng hẳn về nhóm
tra cứu đơn giản không cần suy luận — khi đó phần lớn request nằm trong khoảng
0,1 giây và việc cắt độ trễ mạng có ý nghĩa tương đối lớn.

---

## 3. Đổi ngay bằng mã nguồn hiện có sẽ làm hệ thống chậm đi

Đây là điểm quan trọng nhất của văn bản này.

Lớp `OracleRunner` trong `src/vanna/integrations/oracle/sql_runner.py` đang mắc
**đúng hai lỗi** mà đợt tối ưu vừa rồi đã gỡ khỏi `PostgresRunner`:

**Lỗi 1 — mở kết nối mới cho từng truy vấn.** Dòng 50 gọi
`oracledb.connect(...)`, dòng 75 đóng lại. Không có connection pool. Đây chính
là nguyên nhân khiến mỗi truy vấn tốn khoảng 4 giây trước khi tối ưu; sau khi
thêm pool, con số này còn 0,105 giây.

**Lỗi 2 — chặn vòng lặp sự kiện.** Hàm `async def run_sql` gọi thẳng thư viện
đồng bộ `oracledb`, không đẩy sang thread pool. Máy chủ chỉ có một vòng lặp sự
kiện, nên trong suốt thời gian chờ cơ sở dữ liệu thì **toàn bộ tiến trình đóng
băng** — mọi người dùng khác bị phục vụ nối đuôi nhau.

Đây không phải rủi ro lý thuyết. Chính hai lỗi này đã được đo trên
`PostgresRunner` ở đợt kiểm thử đầu tiên, tại mức 50 người dùng đồng thời:

| Chỉ số | Khi còn hai lỗi | Sau khi sửa |
|---|---:|---:|
| Thông lượng | 0,70 req/giây | 4,43 req/giây |
| Tỉ lệ lỗi | 13% | 0% |
| p95 | 139,16 giây | 77,79 giây |

`OracleRunner` cũng không đặt `statement_timeout`, nghĩa là một truy vấn chạy
mất kiểm soát sẽ giữ kết nối vô thời hạn.

---

## 4. Nếu vẫn phải đổi vì nghiệp vụ — cần làm những gì

### 4.1. Viết lại `OracleRunner` (khoảng 40 dòng, nửa ngày)

Port đúng khuôn `PostgresRunner` hiện tại:

- `oracledb.ConnectionPool` thay cho `connect()` mỗi lần, 4–16 kết nối;
- đẩy truy vấn sang `ThreadPoolExecutor`, số luồng bằng số kết nối tối đa;
- đặt timeout ở mức phiên;
- chốt chặn `cursor.description is None` trước khi dựng DataFrame — hiện dòng 67
  dùng `cursor.description` vô điều kiện, nên mọi câu lệnh không trả bảng
  (`INSERT`, `ALTER SESSION`, gọi thủ tục) sẽ ném `TypeError` thay vì trả về
  DataFrame rỗng; `PostgresRunner` đã có chốt chặn này;
- loại kết nối lỗi khỏi pool thay vì trả lại.

### 4.2. Sửa phương ngữ SQL — đây mới là phần tốn công

Mã nguồn và chỉ thị cho mô hình đang gắn chặt với PostgreSQL:

| Vị trí | Vấn đề trên Oracle |
|---|---|
| `main.py` dòng 408, 424 | `LIMIT 10` — Oracle dùng `FETCH FIRST 10 ROWS ONLY` |
| `main.py` dòng 1270 | Chỉ thị bảo mô hình truy vấn `information_schema.columns` — Oracle không có, phải đổi sang `ALL_TAB_COLUMNS` |
| `main.py`, 21 vị trí | Tên bảng gắn tiền tố schema `qlsp_backup.` |
| `main.py` dòng 1127–1145 | Hàm đặt `search_path` qua tham số khởi động — Oracle dùng `ALTER SESSION SET CURRENT_SCHEMA` |

Ngoài ra prompt hệ thống đang dạy mô hình sinh SQL theo cú pháp PostgreSQL. Đổi
sang Oracle nghĩa là phải viết lại phần chỉ thị này và **kiểm thử lại chất lượng
sinh SQL** — đây là rủi ro lớn hơn nhiều so với phần hạ tầng, vì mô hình
`llama3.2` 3B vốn đã tuân thủ chỉ thị ở mức vừa phải.

### 4.3. Đo lại

Không có cách nào kiểm chứng hai mục trên nếu chưa có một instance Oracle để
đấu nối. Sau khi có, cần chạy lại bộ kiểm thử tải hiện có (`loadtest/`) để xác
nhận không hồi quy — bộ kiểm thử đã được siết để bắt buộc kiểm tra dữ liệu trả
về, nên sẽ phát hiện được lỗi kiểu "trả lời rỗng nhưng vẫn báo thành công".

---

## 5. Khuyến nghị

1. **Không đổi cơ sở dữ liệu vì lý do hiệu năng.** Ngân sách công sức nên dồn
   vào tầng suy luận, nơi đang chiếm trên 99% độ trễ.
2. Nếu bắt buộc đổi vì nghiệp vụ, **coi đây là việc chuyển đổi nền tảng, không
   phải việc tối ưu**: dự trù 2–3 ngày công, và phần rủi ro nằm ở chất lượng
   sinh SQL chứ không ở hạ tầng kết nối.
3. Việc thực sự đáng làm tiếp theo để giảm độ trễ, theo thứ tự hiệu quả trên chi
   phí:
   - dò lại `OLLAMA_NUM_PARALLEL` sau khi đã bật GPU tích hợp (ràng buộc bộ nhớ
     đã khác, giá trị 4 chưa chắc còn tối ưu) — khoảng 20 phút;
   - xác định ngưỡng an toàn của GPU tích hợp trong khoảng 30–50 người dùng —
     khoảng 10 phút;
   - đầu tư GPU rời hoặc chuyển sang API LLM đám mây, nếu cần đạt p95 dưới 10
     giây ở mức 30–50 người dùng. Đây là con đường duy nhất còn lại, vì mọi
     phương án cấu hình không tốn chi phí đều đã thử hết.

---

## Phụ lục: nguồn số liệu

Toàn bộ con số trong văn bản này lấy từ 11 lượt đo tải bằng k6 thực hiện ngày
05/08/2026, tổng cộng 7.400 request, tiêu chí thành công bắt buộc có dữ liệu
thật trả về. Số liệu thô nằm trong thư mục `loadtest/` (`k6_*_v2.json`), phân
tích đầy đủ trong `loadtest/bao_cao_hieu_nang_ai.pdf`.
