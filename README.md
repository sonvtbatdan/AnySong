# AnySong

Ứng dụng desktop chạy hoàn toàn trên máy bạn: tải một bài hát lên, mô tả phong cách mong muốn, AI sẽ tạo bản cover theo phong cách đó. Hoặc giữ nguyên giọng hát gốc và chỉ thay nhạc nền. Cũng có thể tạo nhạc mới từ một câu mô tả.

Mô hình nhạc là [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5), mã nguồn mở, chạy local, miễn phí, hỗ trợ tiếng Việt tốt trong nhóm open-source hiện tại. Không cần API key, không gửi nhạc của bạn lên bất kỳ máy chủ nào.

## Tính năng

- **Cover**: sinh lại toàn bộ bài (giai điệu, nhạc cụ, giọng hát) theo style bạn mô tả. Nhanh, nhưng giọng hát là giọng AI, không phải giọng gốc.
- **Giữ nguyên melody gốc**: tách giọng hát gốc, sinh nhạc nền mới khớp độ dài và BPM, rồi mix lại. Giữ đúng giọng ca sĩ. Có chế độ thử nghiệm "Complete AI" bám lời và nhịp hơn nhưng để AI hát lại.
- **Tạo nhạc mới (tab Create)**: từ prompt (và lời, nếu muốn), không cần file gốc. Thời lượng 10 đến 300 giây.
- **Tự tìm lời** theo tên file qua LRCLIB. Nếu không có, nhận dạng lời bằng Whisper chạy local.
- **Tự động nhờ Gemini** (tuỳ chọn): viết caption và chèn tag cấu trúc (`[Verse]`, `[Chorus]`...) vào lời.
- Chọn giọng nam / nữ / song ca, BPM (tự phát hiện hoặc nhập tay), 1 đến 4 bản sinh để chọn bản đẹp nhất, xuất MP3 / WAV / FLAC.
- Lịch sử các bản đã tạo, lưu style hay dùng, nghe thử ngay trong app.

## Yêu cầu

| Thành phần | Yêu cầu |
|---|---|
| Hệ điều hành | Windows 10/11 (các script cài đặt và khởi động viết cho Windows) |
| GPU | NVIDIA, khuyến nghị 16 GB VRAM trở lên (xem phần Hiệu năng) |
| Python | 3.11 hoặc 3.12 |
| Công cụ | [Git](https://git-scm.com/), [uv](https://docs.astral.sh/uv/), `ffmpeg` và `ffprobe` nằm trong PATH |
| Ổ cứng | Vài GB cho thư viện và vài chục GB cho model weight |

Cài `uv` nếu chưa có:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Cài đặt và chạy

```powershell
git clone https://github.com/sonvtbatdan/AnySong.git
cd AnySong
```

Sau đó nhấp đúp **`run.bat`**. Lần chạy đầu tiên nó tự cài những gì còn thiếu:

1. venv cho backend (vài giây).
2. Engine ACE-Step 1.5: clone repo vào `engine/` rồi `uv sync`, tải vài GB thư viện (có thể mất vài chục phút).
3. venv cho `gemini_service` (tuỳ chọn).

Sau đó app mở thành một cửa sổ desktop. Đóng cửa sổ là tắt toàn bộ tiến trình nền.

Model weight (nhiều GB) chỉ được tải vào lần tạo nhạc đầu tiên, không tải lúc cài đặt.

### Cài thủ công từng bước

```powershell
.\scripts\setup_engine.ps1          # clone + uv sync engine ACE-Step
.\scripts\setup_backend.ps1         # venv cho backend
.\scripts\setup_gemini_service.ps1  # (tuỳ chọn) venv cho gemini_service
.\scripts\start.ps1                 # chạy ở chế độ dev, mở trình duyệt tại http://127.0.0.1:8877
```

`start.ps1` mở các tiến trình trong cửa sổ PowerShell riêng nên tiện xem log khi gỡ lỗi. `run.bat` dùng `app_launcher.py`, chạy mọi thứ ẩn và ghi log vào `logs/`.

## Cách dùng

1. Chọn file nhạc gốc (wav / mp3 / m4a / flac). Tên bài và ca sĩ được đoán từ tên file để tìm lời, hoặc bạn tự nhập.
2. Kiểm tra và sửa lời. Để trống nghĩa là hoà tấu không lời.
3. Mô tả style bằng một câu đầy đủ (thể loại, nhạc cụ, không khí, cách sản xuất). Model được train trên caption dạng câu văn nên mô tả càng rõ càng khớp. Hoặc để trống và bật "Tự động nhờ Gemini".
4. Chọn chế độ: cover thường (nhanh) hoặc "Giữ nguyên melody gốc" (chậm hơn, giữ giọng gốc).
5. Bấm "Tạo bản cover" và đợi. Thời gian tuỳ độ dài bài, chế độ và GPU, từ vài chục giây đến vài phút.

Cover thường giới hạn bài gốc tối đa 90 giây. Chế độ giữ melody không bị giới hạn này.

## Kiến trúc

```
AnySong/
├── app_launcher.py   Chạy mọi thứ ẩn + mở cửa sổ pywebview
├── run.bat           Điểm vào cho người dùng: tự cài đặt rồi gọi app_launcher
├── backend/          FastAPI: nhận upload + prompt, điều phối engine, trả audio
├── frontend/         Một trang HTML/JS (upload, lời, style, player, lịch sử)
├── gemini_service/   (tuỳ chọn) tự động hoá gemini.google.com để viết prompt
├── scripts/          Script cài đặt và khởi động cho Windows
├── engine/           ACE-Step 1.5 (clone về khi cài, không nằm trong repo)
├── outputs/          Kết quả tạo ra + lịch sử (không nằm trong repo)
└── logs/             Log của engine/backend khi chạy bằng run.bat (không nằm trong repo)
```

Backend là lớp điều phối nhẹ. Nó chỉ phụ thuộc ML nặng ở Whisper (nhận dạng lời), còn nhạc do engine ACE-Step sinh ra qua HTTP. Các cổng mặc định:

| Dịch vụ | Cổng | Ghi chú |
|---|---|---|
| AnySong backend + UI | 8877 | |
| Engine `acestep-v15-xl-turbo` | 8001 | Cover, sinh nhạc nền, tạo nhạc mới |
| Engine `acestep-v15-base` | 8002 | Chỉ dùng cho bước Extract, backend tự bật khi cần và tắt ngay sau đó |
| gemini_service | 8004 | Tuỳ chọn |

### Vì sao tách 2 engine

Mỗi tiến trình engine chỉ nạp đúng một model suốt vòng đời, vì nạp hai model vào cùng một tiến trình từng gây crash trên Windows. Bước Extract chỉ chạy được trên model Base. Base và Turbo cùng nằm trong VRAM thì card 16 GB không đủ (hoặc phải offload qua CPU và chậm hơn hàng chục lần), nên engine Base chỉ được bật trong vài giây của bước Extract rồi tắt, để Turbo luôn chạy ở chất lượng đầy đủ.

### Pipeline "giữ nguyên melody gốc"

1. **Extract** (model Base, 64 bước + ADG): tách track được chọn, thường là vocals. Đây vẫn là diffusion chứ không phải tách âm thanh thuần tuý như Demucs, nên chạy ở mức chất lượng cao nhất để giảm sai lệch. Kết quả được cache theo nội dung file.
2. **Sinh nhạc nền mới** (model Turbo, `text2music`): khớp độ dài và BPM (phát hiện bằng `librosa`), dùng lời dạng tag cấu trúc (`[Intro]` / `[Build]` / `[Climax]`...) co giãn theo độ dài để nhạc không chạy đều đều.
3. **ffmpeg mix** hai track thành một file.

### gemini_service (tuỳ chọn)

Điều khiển một cửa sổ pywebview chạy gemini.google.com thật, đăng nhập bằng tài khoản Google của bạn, không qua API hay billing. Lần đầu chạy sẽ hiện cửa sổ Gemini để bạn đăng nhập một lần, sau đó cửa sổ tự ẩn và nhớ phiên. Nếu không cài, tắt ô "Tự động nhờ Gemini" trong giao diện là dùng app bình thường.

Phiên đăng nhập lưu trong `gemini_service/.webview_data/` và đã được gitignore. Không bao giờ commit thư mục này vì nó chứa cookie đăng nhập thật.

## Cấu hình

Biến môi trường (đều có giá trị mặc định hợp lý):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `ANYSONG_ENGINE_URL_TURBO` | `http://127.0.0.1:8001` | Địa chỉ engine Turbo |
| `ANYSONG_ENGINE_URL_BASE` | `http://127.0.0.1:8002` | Địa chỉ engine Base |
| `ANYSONG_GEMINI_SERVICE_URL` | `http://127.0.0.1:8004` | Địa chỉ gemini_service |
| `ANYSONG_OUTPUT_DIR` | `./outputs` | Nơi lưu kết quả và lịch sử |
| `ANYSONG_WHISPER_MODEL` | `small` | Cỡ model Whisper |
| `ANYSONG_WHISPER_DEVICE` | `cpu` | `cpu` hoặc `cuda` |
| `ANYSONG_WHISPER_COMPUTE_TYPE` | `int8` (CPU) / `float16` (CUDA) | Kiểu tính toán Whisper |

## Hiệu năng và hạn chế đã biết

- Đã kiểm chứng trên card 16 GB. Với engine Turbo chạy riêng và không offload, một bài 259 giây sinh xong trong khoảng 4 giây diffusion. Card ít VRAM hơn có thể cần bật `ACESTEP_OFFLOAD_TO_CPU` trong `app_launcher.py`, đổi lại sẽ chậm đi nhiều.
- LM 5Hz của ACE-Step bị tắt (`ACESTEP_INIT_LLM=false`) vì trên Windows nó chưa có backend dùng được: không có Triton thì chạy cực chậm, có Triton thì treo lúc khởi tạo. Bật lại khi ACE-Step hỗ trợ Windows đầy đủ.
- ACE-Step là model sinh nhạc, không phải voice cloning. Chỉ chế độ "giữ nguyên melody gốc" mới giữ được giọng ca sĩ, và vì bước tách vẫn là diffusion nên không hoàn toàn lossless.
- Chế độ "Tách + Mix" sinh nhạc nền độc lập với lời nên có thể lệch nhịp trên bài dài. Khi đó thử "Complete AI".

## Khắc phục sự cố

- **Backend không khởi động**: mở `logs/backend.log`, hoặc chạy `scripts\start.ps1` để xem lỗi trực tiếp.
- **Tạo nhạc thất bại**: xem `logs/engine_turbo.log` và `logs/engine_base.log`.
- **Tính năng Gemini báo lỗi**: kiểm tra `logs/gemini_service.log`, hoặc xoá `gemini_service/.webview_data/` rồi đăng nhập lại.
- **Dọn kết quả**: xoá bản trong lịch sử qua menu ⋮ trên giao diện để xoá luôn file trên đĩa.

## Lưu ý pháp lý

Chỉ dùng với nhạc bạn có quyền sử dụng. Bản cover tạo ra vẫn là tác phẩm phái sinh của bài gốc, quyền tác giả của bài gốc không mất đi. Tôn trọng giấy phép của [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) và các model đi kèm khi phân phối lại kết quả.
