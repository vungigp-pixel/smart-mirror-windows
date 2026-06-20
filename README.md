# Smart Mirror for Windows

Smart Mirror là chương trình Python đồng bộ một chiều trên Windows 11:

```text
source (chỉ đọc) ──────────────> replica (được cập nhật)
                                  │
                                  └── file dư/xóa → quarantine
```

Chương trình dùng SQLite để lưu manifest và NTFS USN Change Journal để chỉ đọc
những thay đổi mới. Vì vậy chế độ chạy liên tục không phải quét toàn bộ cây thư
mục sau mỗi vài giây.

> [!CAUTION]
> Đây là công cụ mirror, không phải backup có lịch sử phiên bản. Thay đổi xấu
> tại nguồn, bao gồm ransomware, vẫn có thể được truyền sang replica. Hãy duy
> trì thêm một bản backup độc lập và luôn chạy `sync --dry-run` trước lần đầu.

## Nguyên tắc hoạt động

1. `init` quét source và replica, tạo manifest SQLite và checkpoint USN.
2. NTFS tiếp tục ghi các thao tác tạo, sửa, xóa và rename vào USN Journal.
3. `sync` hoặc `run` chỉ đọc phần journal mới kể từ checkpoint.
4. Chương trình cập nhật manifest và lập kế hoạch copy/move/quarantine.
5. Trước thao tác phá hủy, đường dẫn và NTFS file ID được kiểm tra lại.
6. Theo chu kỳ, chương trình quét đầy đủ để đối soát manifest với filesystem.

Database chỉ là chỉ mục tăng tốc, không được coi là nguồn sự thật tuyệt đối.

## Bảo vệ dữ liệu

- Code chính không xóa, move hoặc ghi nội dung vào source.
- Nếu source biến mất hoặc root/volume identity thay đổi, chương trình dừng.
- Source và replica không được trùng nhau hoặc chứa nhau.
- Database, quarantine và log phải nằm ngoài source và replica.
- File được copy vào file tạm, `fsync`, sau đó thay thế bằng `os.replace`.
- File dư tại replica được move sang quarantine thay vì xóa ngay.
- Replica được xem là vùng do chương trình quản lý. Trong khi `sync` đang chạy,
  không chỉnh sửa trực tiếp replica; chương trình định kỳ bỏ qua các USN event
  do chính nó vừa tạo để tránh journal đầy trong lần copy đầu.
- SHA-256 được tính trong lượt copy hoặc khi cần xác minh nội dung.
- Rename/move được nhận diện bằng NTFS file ID để tránh copy lại không cần thiết.
- Nếu USN Journal reset hoặc wrap, chương trình ép đối soát có xác minh hash.
- SQLite dùng WAL và `synchronous=FULL`.

Các thao tác ghi/xóa chỉ hướng tới replica, quarantine, database và file log.

## Yêu cầu

- Windows 11.
- Python 3.10 trở lên; đã kiểm thử với Python 3.12.
- Source và replica nằm trên volume NTFS có ký tự ổ đĩa.
- PowerShell hoặc Command Prompt chạy bằng quyền Administrator để đọc USN.
- Chỉ sử dụng thư viện chuẩn của Python, không cần cài package ngoài.

## Cài đặt

Clone repository và chuyển vào thư mục dự án:

```powershell
git clone https://github.com/vungigp-pixel/smart-mirror-windows.git
cd .\smart-mirror-windows
python --version
```

Tạo file cấu hình cục bộ:

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

`config.json` đã được `.gitignore` loại khỏi repository để tránh publish đường
dẫn riêng của máy.

## Cấu hình

Ví dụ đầy đủ:

```json
{
  "source": "D:\\DATA",
  "replica": "F:\\DATA1",
  "database": "F:\\SmartMirrorState\\manifest.sqlite3",
  "quarantine": "F:\\SmartMirrorTrash",
  "poll_seconds": 30,
  "full_reconcile_hours": 168,
  "trash_retention_days": 30,
  "hash_mode": "on_copy",
  "copy_buffer_mb": 4,
  "max_copy_retries": 3,
  "exclude_dirs": [
    "$RECYCLE.BIN",
    "System Volume Information"
  ]
}
```

### Ý nghĩa từng biến

| Biến | Ý nghĩa |
|---|---|
| `source` | Thư mục nguồn. Chương trình chính chỉ đọc dữ liệu tại đây. |
| `replica` | Thư mục đích phải phản chiếu source. File tại đây có thể được tạo, ghi đè, move hoặc chuyển vào quarantine. |
| `database` | File SQLite chứa manifest, SHA-256, NTFS file ID, checkpoint USN và lịch sử thao tác. Chương trình tự tạo file này. |
| `quarantine` | Nơi giữ file bị loại khỏi replica. Phải cùng volume với replica để `os.replace` hoạt động nguyên tử. |
| `poll_seconds` | Thời gian nghỉ giữa hai vòng đọc USN trong chế độ `run`; không phải chu kỳ quét toàn bộ. Giá trị nhỏ nhất là 1 giây. |
| `full_reconcile_hours` | Khoảng thời gian giữa hai lần quét đối soát đầy đủ. `168` giờ tương đương 7 ngày. |
| `trash_retention_days` | Số ngày giữ dữ liệu quarantine trước khi xóa vĩnh viễn. `0` hiện có nghĩa là không tự dọn. |
| `hash_mode` | `on_copy` tính/xác minh SHA-256; `never` chỉ dựa vào metadata và giảm mức bảo đảm nội dung. Khuyến nghị `on_copy`. |
| `copy_buffer_mb` | Kích thước mỗi khối đọc/ghi. Đây không phải giới hạn kích thước file. |
| `max_copy_retries` | Số lần thử copy khi file bị khóa hoặc thay đổi trong lúc đọc. |
| `exclude_dirs` | Danh sách đường dẫn thư mục tương đối cần bỏ qua. Symbolic link và junction luôn bị bỏ qua. |

Trong JSON, dấu `\` trong đường dẫn phải được viết thành `\\`, ví dụ
`D:\\DATA`.

Nên đồng bộ một thư mục dữ liệu riêng thay vì toàn bộ root volume. Nếu source là
toàn bộ `D:\`, tuyệt đối không đặt database hoặc log trên ổ D.

## Chuẩn bị USN Change Journal

Mở PowerShell bằng quyền Administrator và kiểm tra journal:

```powershell
fsutil usn queryjournal D:
fsutil usn queryjournal F:
```

Nếu Windows báo `The volume change journal is not active`, kích hoạt journal:

```powershell
fsutil usn createjournal m=268435456 a=67108864 D:
fsutil usn createjournal m=268435456 a=67108864 F:
```

Ví dụ trên đặt kích thước tối đa 256 MB và allocation delta 64 MB cho mỗi ổ.
Chế độ `run` yêu cầu journal đọc được trên cả source và replica.

Nếu checkpoint cũ hơn `FirstUsn` hoặc Windows trả về lỗi 1181, chương trình tự
đánh dấu journal đã wrap và chuyển sang đối soát có xác minh thay vì dừng bằng
traceback.

## Các chế độ chạy

Cú pháp chung:

```powershell
python .\smart_mirror.py <init|sync|reconcile|run|status> --config .\config.json
```

### `init` — tạo baseline lần đầu

```powershell
python .\smart_mirror.py init --config .\config.json
```

`init` thực hiện:

- kiểm tra source/replica và quan hệ giữa các đường dẫn;
- tự tạo database và thư mục replica nếu cần;
- quét metadata của source và replica;
- lưu volume serial, root file ID và checkpoint USN;
- không copy, ghi đè, move hoặc quarantine file trong lần gọi này.

Nên chạy `init` rõ ràng trước lần đồng bộ đầu tiên. Nếu chưa có database, `sync`
cũng có thể tự khởi tạo baseline.

### `sync` — đồng bộ một lần

Xem trước kế hoạch:

```powershell
python .\smart_mirror.py sync --config .\config.json --dry-run
```

Chạy thật:

```powershell
python .\smart_mirror.py sync --config .\config.json
```

`sync` đọc phần USN mới, cập nhật manifest, lập kế hoạch và thực hiện tối đa 10
vòng cho đến khi hai phía hội tụ. Các hành động có thể gồm:

- `mkdir`: tạo thư mục tại replica;
- `copy`: copy file mới hoặc ghi đè file khác nội dung;
- `verify`: xác minh SHA-256;
- `move`: đổi tên hoặc di chuyển bên trong replica;
- `replace_type`: xử lý xung đột file/thư mục;
- `trash`: chuyển dữ liệu dư tại replica vào quarantine.

Kết quả ví dụ:

```json
{"planned": 5, "completed": 5, "failed": 0}
```

`--dry-run` không thay đổi replica/quarantine, nhưng vẫn có thể tạo hoặc cập nhật
database, checkpoint và thư mục replica.

### `reconcile` — ép đối soát đầy đủ

```powershell
python .\smart_mirror.py reconcile --config .\config.json
```

Lệnh này quét lại toàn bộ source và replica, làm mới checkpoint và yêu cầu xác
minh lại hash. Nó chỉ cập nhật database; chưa áp dụng kế hoạch. Sau đó chạy:

```powershell
python .\smart_mirror.py sync --config .\config.json --dry-run
python .\smart_mirror.py sync --config .\config.json
```

Không cần chạy `reconcile` thường xuyên vì chế độ `run` tự đối soát theo
`full_reconcile_hours` và tự xử lý trường hợp journal reset/wrap.

### `run` — chạy liên tục

```powershell
python .\smart_mirror.py run `
  --config .\config.json `
  --log F:\SmartMirrorState\smart_mirror.log
```

Mỗi vòng chương trình:

```text
đọc USN → cập nhật database → đồng bộ nếu cần → ngủ poll_seconds → lặp lại
```

Chu kỳ giữa hai lần bắt đầu bằng thời gian xử lý cộng `poll_seconds`. Thay đổi
phát sinh khi chương trình tắt vẫn còn trong journal và được đọc khi chạy lại.
Cửa sổ terminal phải tiếp tục mở; nhấn `Ctrl+C` để dừng.

Không nên để `run --dry-run` chạy lâu vì cùng một kế hoạch sẽ được in lại sau
mỗi chu kỳ nhưng không được áp dụng.

### `status` — xem trạng thái cache

```powershell
python .\smart_mirror.py status --config .\config.json
```

Ví dụ:

```json
{
  "last_full_scan": "2026-06-19T10:30:00+00:00",
  "source_entries": 12000,
  "replica_entries": 11990,
  "planned_actions": 10,
  "reconcile_required": false
}
```

`status` không đọc journal và không quét filesystem; nó chỉ phản ánh database
hiện tại. Muốn có kế hoạch cập nhật nhất, dùng `sync --dry-run`.

## Tùy chọn dòng lệnh

| Tùy chọn | Công dụng |
|---|---|
| `--config PATH` | File cấu hình; mặc định là `config.json` trong thư mục hiện tại. |
| `--dry-run` | Không thay đổi replica hoặc quarantine; phù hợp nhất với `sync`. |
| `--log PATH` | Ghi log ra file. Log bắt buộc nằm ngoài source và replica. |
| `--verbose` | Bật log chi tiết hơn. |

## Trình tự sử dụng khuyến nghị

```powershell
# 1. Khởi tạo database và baseline
python .\smart_mirror.py init --config .\config.json

# 2. Xem trạng thái
python .\smart_mirror.py status --config .\config.json

# 3. Kiểm tra kế hoạch, chưa thay đổi replica
python .\smart_mirror.py sync --config .\config.json --dry-run

# 4. Đồng bộ thật lần đầu
python .\smart_mirror.py sync --config .\config.json

# 5. Chạy liên tục
python .\smart_mirror.py run --config .\config.json `
  --log F:\SmartMirrorState\smart_mirror.log
```

## Database và khôi phục trạng thái

Chương trình tự tạo database SQLite; không cần tạo thủ công. Database lưu:

- manifest source và replica;
- kích thước, `mtime`, SHA-256 và NTFS file ID;
- volume/root identity;
- checkpoint USN;
- lịch sử thao tác và lỗi.

Không xóa database khi chương trình đang chạy. Nếu database mất hoặc hỏng:

1. dừng chế độ `run`;
2. đổi tên/xóa database cũ;
3. chạy lại `init`;
4. chạy `sync --dry-run` và kiểm tra kỹ trước khi đồng bộ thật.

## Quarantine

Khi một file chỉ còn ở replica, chương trình move nó vào:

```text
<quarantine>\YYYY-MM-DD\<đường-dẫn-tương-đối>
```

Sau `trash_retention_days`, bucket cũ được xóa vĩnh viễn. Có thể phục hồi thủ
công từ quarantine trước thời hạn. Không đặt dữ liệu cần giữ độc lập trong
replica vì mirror sẽ coi đó là dữ liệu dư.

## Kiểm thử

Unit test, chỉ dùng thư mục tạm:

```powershell
python -m unittest -v .\test_smart_mirror.py
```

Integration test USN có tạo rồi tự dọn các thư mục mang tên riêng trên ổ F:

```powershell
python .\integration_test_usn.py
```

Hãy đọc file kiểm thử và chạy PowerShell Administrator trước khi dùng integration
test trên máy khác.

## Xử lý sự cố

### `Access is denied` khi đọc USN

Mở PowerShell bằng **Run as administrator**, kiểm tra lại bằng
`fsutil usn queryjournal` rồi chạy chương trình trong chính cửa sổ đó.

### `The volume change journal is not active`

Kích hoạt journal bằng các lệnh `fsutil usn createjournal` ở phần chuẩn bị USN,
sau đó chạy lại `reconcile` để lưu checkpoint mới.

### `root identity changed` hoặc `volume identity changed`

Chương trình đang chặn đồng bộ vì thư mục gốc hoặc ổ đĩa không còn đúng identity
đã lưu. Không xóa database để bỏ qua ngay. Hãy xác minh ký tự ổ và dữ liệu vật
lý trước, sau đó mới quyết định tạo baseline mới.

### Có thao tác `failed`

Kiểm tra file log, quyền truy cập, dung lượng trống và file đang bị ứng dụng
khóa. Chương trình không coi thao tác thất bại là thành công và sẽ thử lại ở
chu kỳ sau.

## Giới hạn

- Chỉ hỗ trợ Windows/NTFS cho chế độ USN liên tục.
- Không copy symbolic link hoặc junction.
- Hard link chưa được hỗ trợ; đối soát sẽ dừng thay vì tạo manifest sai.
- Không bảo toàn ACL, owner hoặc alternate data streams.
- File đang bị khóa độc quyền có thể không copy được.
- Không sử dụng VSS nên không bảo đảm snapshot nhất quán cho database hoặc file
  đang được nhiều tiến trình cập nhật đồng thời.
- Không thay thế một giải pháp backup có version và bản sao offline.
