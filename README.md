# AnySong

Tải 1 bài hát lên, mô tả style bạn muốn, AI tạo ra bản cover theo style đó -
hoặc giữ nguyên giọng hát gốc, chỉ đổi nhạc nền.

## Kiến trúc

```
AnySong/
├── backend/          FastAPI - nhận upload + prompt, điều phối các engine, trả audio về
├── frontend/          1 trang HTML/JS (upload + lyrics + style + player + lịch sử)
├── engine/            ACE-Step 1.5 (clone riêng, không nằm trong repo này)
├── gemini_service/    (tuỳ chọn) tự động hoá gemini.google.com để viết/chuẩn hoá prompt
└── scripts/            script setup + start cho Windows (PowerShell)
```

`backend/` không có dependency ML nặng nào ngoài Whisper (fallback tách lời) - nó
là lớp orchestrator: nhận file + prompt từ trình duyệt, gọi sang các service chạy
riêng (ACE-Step engine(s), gemini_service), poll tới khi xong, trả kết quả về.

Model nhạc: **ACE-Step 1.5** - mã nguồn mở, chạy local free, hỗ trợ tiếng Việt tốt
trong nhóm open-source hiện tại.

### 2 engine ACE-Step riêng biệt

Chạy 2 process engine **tách biệt hoàn toàn**, mỗi process chỉ load đúng 1 model
suốt vòng đời (load 2 model vào cùng 1 process từng gây crash trên Windows):
- **port 8001** - `acestep-v15-xl-turbo`, LM bật (`thinking`) - dùng cho cover
  thường + bước sinh nhạc nền của pipeline giữ-melody.
- **port 8002** - `acestep-v15-base`, LM tắt - chỉ dùng cho bước Extract (tách
  track) của pipeline giữ-melody, vì Extract chỉ chạy được trên model Base.

### 2 chế độ tạo nhạc

- **Cover** (mặc định): 1 lệnh `task_type=cover` trên model turbo - sinh lại toàn
  bộ (giai điệu + nhạc cụ + giọng hát) bằng diffusion dựa theo audio gốc. Giọng
  hát **không được giữ nguyên** vì ACE-Step là model tạo nhạc, không phải
  voice-cloning.
- **Giữ nguyên melody gốc**: pipeline 3 bước đảm bảo giữ đúng giọng gốc:
  1. **Extract** (model Base, 64 bước + ADG) tách track được chọn (thường là
     vocals) - vẫn là diffusion (không phải tách âm thanh thuần tuý như Demucs)
     nên chạy ở mức chất lượng cao nhất để giảm sai lệch.
  2. **Sinh nhạc nền mới** hoàn toàn từ đầu (model Turbo, `task_type=text2music`,
     LM-enhanced) - khớp độ dài + BPM (tự phát hiện bằng `librosa`) với track đã
     tách, và dùng lyrics dạng structure-tag (`[Intro]`/`[Build]`/`[Climax]`...)
     co giãn theo độ dài thay vì 1 tag `[Instrumental]` phẳng, để tránh nhạc chạy
     đều đều không cấu trúc.
  3. **ffmpeg mix** 2 track lại thành 1 file.

### gemini_service (tuỳ chọn)

Tự động hoá gemini.google.com thật qua 1 cửa sổ pywebview ẩn (đăng nhập bằng tài
khoản Google của bạn, không qua API/billing) để viết caption + chèn structure tag
vào lyrics khi bấm "Tạo bản cover" - khỏi phải copy/dán tay qua lại. Xem
`gemini_service/service.py` để biết chi tiết kỹ thuật (JS injection, WS_EX_LAYERED
để ẩn cửa sổ). Không bắt buộc - nếu không setup, tắt checkbox "Tự động nhờ
Gemini" trong UI là dùng app bình thường.

## Chạy nhanh

Double-click **`run.bat`** - tự cài đặt mọi thứ còn thiếu ở lần chạy đầu tiên
(engine ACE-Step, venv backend, venv gemini_service), rồi khởi động toàn bộ.
Không cần mở PowerShell thủ công.

Yêu cầu: Python 3.11-3.12, Git, GPU NVIDIA (khuyến nghị ≥12GB VRAM cho cả 2
engine), `ffmpeg`/`ffprobe` trong PATH.

## Cài đặt / chạy thủ công (nếu muốn kiểm soát từng bước)

```powershell
# 1. Cài engine ACE-Step 1.5 (clone + uv sync - tải vài GB dependency lần đầu)
.\scripts\setup_engine.ps1

# 2. Cài venv riêng cho backend AnySong (nhẹ, vài giây)
.\scripts\setup_backend.ps1

# 3. (Tuỳ chọn) Cài venv cho gemini_service - cần cho tính năng tự động nhờ Gemini
.\scripts\setup_gemini_service.ps1

# 4. Khởi động
.\scripts\start.ps1
```

`run.bat` thực chất chỉ gọi các script trên theo đúng thứ tự. `start.ps1` sẽ:
1. Mở 2 cửa sổ ACE-Step engine (`8001` xl-turbo, `8002` base) - lần đầu chạy sẽ
   tự tải model weight (nhiều GB).
2. Nếu đã setup, mở gemini_service (`8004`) - **lần đầu chạy sẽ hiện 1 cửa sổ
   Gemini thật, đăng nhập Google trong đó 1 lần**, sau đó tự ẩn và nhớ phiên cho
   các lần chạy sau.
3. Đợi 2 engine sẵn sàng.
4. Chạy AnySong backend + frontend tại `http://127.0.0.1:8877` và mở trình duyệt.

## Cách dùng

1. Chọn file nhạc gốc (wav/mp3/m4a/flac) - tên bài/ca sĩ được tự đoán từ tên file
   để tìm lời (LRCLIB), hoặc tự nhập tay.
2. Xác nhận/sửa lời bài hát - để trống = hoà tấu không lời.
3. Mô tả style, hoặc để trống + bấm "Tạo bản cover" (nếu bật "Tự động nhờ
   Gemini") để AI tự viết.
4. Chọn chế độ: cover thường (nhanh) hay "Giữ nguyên melody gốc" (chậm hơn,
   giữ đúng giọng hát).
5. Bấm "Tạo bản cover" và đợi (vài chục giây tới vài phút tuỳ độ dài bài hát,
   chế độ, và GPU).

## Ghi chú

- `engine/` và `gemini_service/.webview_data/` bị gitignore - project ngoài /
  phiên đăng nhập Google, không phải code/dữ liệu của AnySong.
- File kết quả lưu ở `outputs/` (cũng gitignore) - xoá qua menu ⋮ trong lịch sử
  trên UI để dọn sạch cả file trên đĩa, không chỉ khỏi danh sách.
- Model weight lớn chỉ tải khi engine chạy generation lần đầu, không tải sẵn khi
  setup.
