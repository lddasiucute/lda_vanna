# Hướng dẫn cài đặt và chạy Vanna Text-to-SQL

Ứng dụng sử dụng:

- Vanna 2.0 và FastAPI;
- Ollama với model `llama3.2` chạy local, không tốn phí API;
- PostgreSQL trên Neon;
- giao diện chat tại `http://127.0.0.1:8000`.

## 1. Yêu cầu

- Windows 10 hoặc Windows 11;
- Python 3.11 trở lên;
- Ollama;
- một project PostgreSQL trên Neon;
- thư mục mã nguồn có `main.py`, `pyproject.toml`, `src` và
  `neon_sales_demo.sql`.

## 2. Cài Ollama

Tải Ollama tại:

<https://ollama.com/download/windows>

Đóng PowerShell cũ, mở cửa sổ mới và chạy:

```powershell
ollama --version
ollama pull llama3.2
ollama list
```

Nếu Ollama chưa tự chạy:

```powershell
ollama serve
```

Giữ cửa sổ này mở và dùng một cửa sổ PowerShell khác cho Vanna.

## 3. Tạo dữ liệu trên Neon

1. Tạo project trên Neon.
2. Mở **SQL Editor**.
3. Mở file `neon_sales_demo.sql` ở máy.
4. Copy toàn bộ nội dung vào SQL Editor và nhấn **Run**.
5. Mở mục **Tables** và kiểm tra có 10 bảng cùng view
   `sales_enriched`.

Script tạo 10.000 đơn hàng, 20.000 dòng sản phẩm và dữ liệu phân tích từ
tháng 01/2025 đến tháng 06/2026.

## 4. Tạo môi trường Python

Mở PowerShell tại thư mục `vanna`:

```powershell
cd "DUONG_DAN_DEN_THU_MUC\vanna"
python -m venv .venv
```

Nếu PowerShell chặn script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Kích hoạt môi trường:

```powershell
.\.venv\Scripts\Activate.ps1
```

## 5. Cài dependency

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[fastapi,ollama,postgres]"
python -m pip install python-dotenv
```

Không chạy `pip install ollma` vì đây là tên sai.

## 6. Cấu hình `.env`

Tạo file cấu hình:

```powershell
Copy-Item .env.example .env
```

Trong Neon, nhấn **Connect**, chọn pooled connection và copy connection
string. Điền vào `.env`:

```dotenv
OLLAMA_MODEL=llama3.2
OLLAMA_HOST=http://localhost:11434
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
```

Thay dòng `DATABASE_URL` bằng nguyên connection string Neon cung cấp.
Không gửi file `.env`, không đưa nó vào file ZIP và không commit lên Git.

## 7. Chạy ứng dụng

Kiểm tra Ollama:

```powershell
ollama list
Invoke-RestMethod http://localhost:11434/api/tags
```

Khởi động Vanna:

```powershell
python main.py
```

Khi terminal hiện:

```text
Uvicorn running on http://127.0.0.1:8000
```

mở:

<http://127.0.0.1:8000>

Giữ cửa sổ PowerShell đang chạy server. Nhấn `Ctrl+C` để dừng.

## 8. Câu hỏi kiểm thử

```text
Tính tổng doanh thu theo từng khu vực và sắp xếp giảm dần.
```

```text
Vẽ biểu đồ đường doanh thu theo từng tháng.
```

```text
Tính tỷ lệ hoàn hàng theo từng danh mục sản phẩm.
```

```text
Cho biết 10 sản phẩm có lợi nhuận cao nhất.
```

```text
So sánh doanh thu giữa các kênh Online, Retail và Partner.
```

## 9. Xử lý lỗi thường gặp

### `ollama` không được nhận diện

Đóng PowerShell, mở lại sau khi cài Ollama. Nếu vẫn lỗi, kiểm tra Ollama
đã được thêm vào `PATH`.

### Thiếu `psycopg2`

```powershell
python -m pip install psycopg2-binary
```

### `DATABASE_URL is not set`

Kiểm tra `.env` nằm cùng thư mục với `main.py` và có:

```dotenv
DATABASE_URL=postgresql://...
```

### Không kết nối được Neon

- kiểm tra đã copy đủ connection string;
- không có khoảng trắng quanh dấu `=`;
- connection string có `sslmode=require`;
- máy đang có kết nối Internet;
- project Neon không bị xóa hoặc tạm dừng bất thường.

### Port 8000 đang được sử dụng

Đóng server cũ bằng `Ctrl+C`, sau đó chạy lại `python main.py`.

## 10. Chạy lại ở những lần sau

```powershell
cd "DUONG_DAN_DEN_THU_MUC\vanna"
.\.venv\Scripts\Activate.ps1
python main.py
```

Không cần import lại `neon_sales_demo.sql` trừ khi muốn xóa và tạo lại
toàn bộ dữ liệu mẫu.
