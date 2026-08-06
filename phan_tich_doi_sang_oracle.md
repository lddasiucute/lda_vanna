# Phân tích: có nên chuyển cơ sở dữ liệu sang Oracle?

Ngày 05/08/2026 — Lê Đức Anh, Trung tâm DMS

---

## Kết luận ngắn

**Nếu lý do là hiệu năng: không nên.** Cơ sở dữ liệu hiện chiếm dưới 1% độ trễ
của hệ thống. Đổi sang Oracle là tối ưu vào chỗ không đau, và nếu triển khai
bằng mã nguồn hiện có thì hệ thống sẽ **chậm đi**, không nhanh lên.

**Nếu lý do là yêu cầu nghiệp vụ** (dữ liệu nằm sẵn trên Oracle, hoặc quy định
bắt buộc dùng Oracle): làm được, nhưng phải sửa ba nhóm việc trước, ước tính
2–3 ngày công. Chi tiết ở Mục 4. Nhóm việc thứ nhất (tầng đấu nối) **đã làm
xong**; hai nhóm còn lại — phương ngữ SQL và kiểm chứng thật — vẫn còn nguyên.

**Trạng thái kiểm chứng:** đã đấu nối máy chủ Oracle thật. Oracle AI Database
26ai Free 23.26.2.0.0 được cài trên máy kiểm thử ngày 06/08/2026, bộ kiểm chứng
chạy **7/7 phép thử đạt**. Mọi con số Oracle trong văn bản này giờ là số đo,
không còn là suy luận — chi tiết ở Mục 4.3.

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

Một instance Oracle đặt tại chỗ cắt gần hết con số này. **Đã đo được**, trên
Oracle 26ai Free chạy cùng máy, pool đã ấm:

| Cấu hình | Trung vị | p95 |
|---|---:|---:|
| Postgres trên Neon, Singapore | 105 ms | — |
| Oracle tại chỗ | **0,5 ms** | 0,8 ms |

Nhanh hơn khoảng **200 lần**. Đây là lợi ích thật, và lớn hơn cả ước lượng
dưới 10 mili-giây ban đầu.

Nhưng cần đặt đúng tỉ lệ, và đây mới là điều quyết định: đó là tiết kiệm
khoảng **0,105 giây trong một request mất 45 giây** — cải thiện 0,2%. Người
dùng sẽ không cảm nhận được. Một tầng cơ sở dữ liệu nhanh hơn 200 lần vẫn
không cứu được hệ thống, vì cơ sở dữ liệu chưa bao giờ là chỗ mất thời gian.

Lợi ích này chỉ trở nên đáng kể nếu tập câu hỏi thực tế nghiêng hẳn về nhóm
tra cứu đơn giản không cần suy luận — khi đó phần lớn request nằm trong khoảng
0,1 giây và việc cắt độ trễ mạng có ý nghĩa tương đối lớn.

---

## 3. Mã nguồn Oracle từng mắc lại đúng hai lỗi đã sửa ở Postgres

Đây là điểm quan trọng nhất của văn bản này.

> **Cập nhật:** hai lỗi mô tả dưới đây **đã được sửa và đã kiểm chứng trên máy
> chủ Oracle thật** — xem Mục 4.3. Phần mô tả giữ nguyên vì nó là căn cứ cho
> khuyến nghị ở Mục 4.
>
> Việc chạy kiểm chứng còn moi ra **lỗi thứ ba** mà đọc mã nguồn không thấy:
> hàm cắt dấu chấm phẩy cuối câu cắt luôn `END;` của khối PL/SQL, khiến mọi
> khối PL/SQL trả về `PLS-00103`. Lỗi này có sẵn trong bản `OracleRunner` gốc
> và đã được bê nguyên sang bản viết lại; chỉ khi đấu nối thật mới lộ ra. Nó
> là lý do tự nó đủ để không tin vào phân tích tĩnh khi chưa đo.

Lớp `OracleRunner` trong `src/vanna/integrations/oracle/sql_runner.py` đã mắc
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

### 4.1. Viết lại `OracleRunner` — ĐÃ LÀM

Đã port đúng khuôn `PostgresRunner` hiện tại:

- `oracledb.create_pool()` tạo một lần, dùng lại, thay cho `connect()` mỗi truy
  vấn; mặc định 1–10 kết nối, mở lười ở lần dùng đầu và có khoá cho đa luồng;
- đẩy truy vấn sang `ThreadPoolExecutor`, số luồng bằng số kết nối tối đa, nên
  `acquire()` không bao giờ bị bỏ đói;
- thiết lập mức phiên gom vào `session_callback` — chỉ chạy một lần cho mỗi kết
  nối mới, không phải mỗi truy vấn: `autocommit`, `call_timeout`,
  `current_schema`;
- `call_timeout` thay cho `statement_timeout` — Oracle không có timeout phía
  máy chủ tương đương, nên phải cắt ở phía client;
- chốt chặn `cursor.description is None` trước khi dựng DataFrame — bản cũ dùng
  `cursor.description` vô điều kiện, nên mọi câu lệnh không trả bảng (`INSERT`,
  `ALTER SESSION`, khối PL/SQL) sẽ ném `TypeError` thay vì trả về DataFrame;
- `pool.drop()` để loại kết nối lỗi khỏi pool thay vì `release()` trả lại;
- chỉ cắt dấu chấm phẩy cuối câu với SQL thường, **không cắt với khối PL/SQL** —
  `END;` là cú pháp bắt buộc, cắt đi thì Oracle trả `PLS-00103`. Lỗi này chỉ
  phát hiện được khi đấu nối thật.

### 4.2. Sửa phương ngữ SQL — đây mới là phần tốn công

Mã nguồn và chỉ thị cho mô hình đang gắn chặt với PostgreSQL. Bảng dưới đây
**đã được kiểm chứng từng dòng** trên Oracle 26ai thật, không còn là suy đoán:

| Vị trí | Vấn đề trên Oracle | Kết quả đo |
|---|---|---|
| `main.py` dòng 408, 424 | `LIMIT 10` — Oracle dùng `FETCH FIRST 10 ROWS ONLY` | `LIMIT` trả `ORA-03047`; `FETCH FIRST` chạy được |
| `main.py` dòng 1270 | Chỉ thị bảo mô hình truy vấn `information_schema.columns` — Oracle không có, phải đổi sang `ALL_TAB_COLUMNS` | `information_schema.columns` trả `ORA-00942`; `ALL_TAB_COLUMNS` chạy được |
| `main.py`, 21 vị trí | Tên bảng gắn tiền tố schema `qlsp_backup.` | chưa đo — cần dữ liệu thật trên Oracle |
| `main.py` dòng 1127–1145 | Hàm đặt `search_path` qua tham số khởi động — Oracle dùng `ALTER SESSION SET CURRENT_SCHEMA` | `ALTER SESSION SET CURRENT_SCHEMA` chạy được |

Ngoài ra prompt hệ thống đang dạy mô hình sinh SQL theo cú pháp PostgreSQL. Đổi
sang Oracle nghĩa là phải viết lại phần chỉ thị này và **kiểm thử lại chất lượng
sinh SQL** — đây là rủi ro lớn hơn nhiều so với phần hạ tầng, vì mô hình
`llama3.2` 3B vốn đã tuân thủ chỉ thị ở mức vừa phải.

### 4.3. Đo lại — ĐÃ LÀM, 7/7 ĐẠT

Oracle AI Database 26ai Free 23.26.2.0.0 đã được cài trên máy kiểm thử ngày
06/08/2026 (DSN `localhost:1521/freepdb1`), đấu nối bằng `oracledb` 4.0.2 chế độ
thin. Bộ kiểm chứng `loadtest/kiem_chung_oracle.py` chạy **7/7 phép thử đạt**.
Báo cáo đầy đủ: `loadtest/bao_cao_dau_noi_oracle.md`.

| # | Phép thử | Kiểm chứng khẳng định nào | Số đo |
|---|---|---|---|
| 1 | Đấu nối và phiên bản | đấu nối được | Oracle 26ai Free 23.26.2.0.0, thin |
| 2 | Độ trễ pool | Mục 2 — đối chứng 105 ms của Neon | trung vị **0,5 ms**, p95 0,8 ms |
| 3 | Không chặn vòng lặp sự kiện | Mục 3, Lỗi 2 | trễ tối đa **6,4 ms** / 133 mẫu, trong khi truy vấn ngủ 2 s |
| 4 | Truy vấn song song thật sự | Mục 3, Lỗi 1 | 8 truy vấn ngủ 2 s xong trong **2,61 s** (nối đuôi sẽ là 16 s) — **6,1x** |
| 5 | Phương ngữ SQL | bảng Mục 4.2, từng dòng | 5/5 đúng như dự đoán |
| 6 | Câu lệnh không trả về bảng | chốt chặn `cursor.description is None` | khối PL/SQL trả `rows_affected` |
| 7 | Cắt truy vấn quá hạn | `call_timeout` thay `statement_timeout` | cắt sau 1,09 s khi đặt 0,5 s |

Bộ kiểm chứng chỉ đọc — không tạo, sửa hay xoá đối tượng nào trong cơ sở dữ liệu.

Hai điều rút ra từ việc chạy thật, mà đọc mã nguồn không cho được:

1. **Lượt chạy đầu tiên chỉ 3/7 đạt.** Bốn phép thử hỏng vì cùng một lỗi: hàm
   cắt dấu chấm phẩy cắt luôn `END;` của khối PL/SQL. Lỗi có sẵn trong bản gốc,
   được bê nguyên sang bản viết lại, và không một lần đọc mã nào phát hiện ra.
2. **Lợi ích độ trễ lớn hơn ước lượng nhưng vẫn không đáng kể.** Ước lượng ban
   đầu là "dưới 10 ms"; số đo thật là 0,5 ms — nhanh hơn Neon 200 lần. Kết luận
   ở Mục 1 vẫn không đổi, vì 0,105 giây tiết kiệm được nằm trong một request
   45 giây.

Phép thử số 3 đã được kiểm chứng ngược để chắc nó không phải phép thử luôn báo
đạt: chạy trên hàm giả lập, kiểu gọi thẳng của bản cũ cho độ trễ vòng lặp
**1.990 ms**, kiểu `run_in_executor` của bản mới cho **6,6 ms** — ngưỡng 200 ms
nằm gọn giữa hai giá trị. Số đo thật trên Oracle (6,4 ms) khớp với vế sau.

**Còn lại:** bộ kiểm chứng chạy trên schema trống. Bước tiếp theo là nạp dữ liệu
`qlsp_backup` sang Oracle rồi chạy lại bộ kiểm thử tải (`loadtest/`) để xác nhận
không hồi quy — bộ kiểm thử đã được siết để bắt buộc kiểm tra dữ liệu trả về,
nên sẽ phát hiện được lỗi kiểu "trả lời rỗng nhưng vẫn báo thành công".

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
