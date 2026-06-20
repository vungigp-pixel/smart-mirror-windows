# Smart Mirror for Windows

Smart Mirror là chương trình Python đồng bộ một chiều trên Windows 11:

```text
source (chỉ đọc) ──────────────> replica (được cập nhật)
                                  │
                                  └── file dư/xóa → quarantine
```

Chương trình dùng SQLite để lưu manifest và chỉ đọc NTFS USN Change Journal của
source. Replica là vùng do chương trình quản lý và không cần quét hoặc đọc USN
toàn volume đích. Vì vậy HDD replica dung lượng lớn không tạo thêm tải journal.

> [!CAUTION]
> Đây là công cụ mirror, không phải backup có lịch sử phiên bản. Thay đổi xấu
> tại nguồn, bao gồm ransomware, vẫn có thể được truyền sang replica. Hãy duy
> trì thêm một bản backup độc lập và luôn chạy `sync --dry-run` trước lần đầu.

## Nguyên tắc hoạt động

1. `init` chỉ quét source, tạo manifest A và checkpoint USN của source.
2. Manifest B ban đầu chỉ có root nếu replica trống.
3. Lần sync đầu copy từng mục và ghi manifest B sau mỗi thao tác thành công.
4. NTFS ghi thay đổi của source vào USN Journal.
5. `sync` hoặc `run` đọc phần journal A mới và so với trạng thái B đã áp dụng.
6. Theo chu kỳ, chương trình chỉ quét lại source để đối soát.

Khi USN báo một thư mục mới, chương trình chỉ scan subtree đó. File tạm biến mất
giữa lúc lập kế hoạch và copy được ghi là `skipped`, không ép quét lại toàn A.

Database chỉ là chỉ mục tăng tốc, không được coi là nguồn sự thật tuyệt đối.

## Bảo vệ dữ liệu

- Code chính không xóa, move hoặc ghi nội dung vào source.
- Nếu source biến mất hoặc root/volume identity thay đổi, chương trình dừng.
- Source và replica không được trùng nhau hoặc chứa nhau.
- Quarantine và log phải nằm ngoài source/replica. Database phải nằm ngoài
  replica; nếu đặt trong source, nó phải ở một thư mục con chuyên dụng và toàn
  bộ thư mục đó được tự động loại khỏi scan/USN.
- File được copy vào file tạm, `fsync`, sau đó thay thế bằng `os.replace`.
- Trên Windows, thao tác file dùng đường dẫn mở rộng `\\?\` để hỗ trợ tên NTFS
  trùng thiết bị DOS như `nul`, `con`, `aux`, `prn` và để dọn file tạm ReadOnly.
- File dư tại replica được move sang quarantine thay vì xóa ngay.
- Replica là vùng chuyên dụng do chương trình quản lý. Không chỉnh sửa trực tiếp
  hoặc đặt dữ liệu độc lập trong replica; chương trình không đọc USN hay quét B.
- SHA-256 được tính trong lượt copy hoặc khi cần xác minh nội dung.
- Rename/move được nhận diện bằng NTFS file ID để tránh copy lại không cần thiết.
- Nếu USN Journal reset hoặc wrap, chương trình ép đối soát có xác minh hash.
- SQLite dùng WAL và `synchronous=FULL`.

Các thao tác ghi/xóa chỉ hướng tới replica, quarantine, database và file log.

## Yêu cầu

- Windows 11.
- Python 3.10 trở lên; đã kiểm thử với Python 3.12.
- Source nằm trên volume NTFS có ký tự ổ đĩa.
- Replica nên là NTFS; không cần bật hoặc đọc USN trên volume replica.
- PowerShell hoặc Command Prompt chạy bằng quyền Administrator để đọc USN source.
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
| `replica` | Thư mục đích chuyên dụng. File tại đây có thể được tạo, ghi đè, move hoặc chuyển vào quarantine. Không chỉnh sửa B ngoài chương trình. |
| `database` | File SQLite chứa manifest A, trạng thái B đã áp dụng, SHA-256, file ID, checkpoint USN A và lịch sử thao tác. Nếu nằm trong source, thư mục cha của file được tự động loại trừ hoàn toàn. |
| `quarantine` | Nơi giữ file bị loại khỏi replica. Phải cùng volume với replica để `os.replace` hoạt động nguyên tử. |
| `poll_seconds` | Thời gian nghỉ giữa hai vòng đọc USN trong chế độ `run`; không phải chu kỳ quét toàn bộ. Giá trị nhỏ nhất là 1 giây. |
| `full_reconcile_hours` | Khoảng thời gian giữa hai lần quét lại source. Không quét replica. `168` giờ tương đương 7 ngày. |
| `trash_retention_days` | Số ngày giữ dữ liệu quarantine trước khi xóa vĩnh viễn. `0` hiện có nghĩa là không tự dọn. |
| `hash_mode` | `on_copy` tính/xác minh SHA-256; `never` chỉ dựa vào metadata và giảm mức bảo đảm nội dung. Khuyến nghị `on_copy`. |
| `copy_buffer_mb` | Kích thước mỗi khối đọc/ghi. Đây không phải giới hạn kích thước file. |
| `max_copy_retries` | Số lần thử copy khi file bị khóa hoặc thay đổi trong lúc đọc. |
| `exclude_dirs` | Danh sách đường dẫn thư mục tương đối cần bỏ qua. Symbolic link và junction luôn bị bỏ qua. |

Trong JSON, dấu `\` trong đường dẫn phải được viết thành `\\`, ví dụ
`D:\\DATA`.

Nên đồng bộ một thư mục dữ liệu riêng thay vì toàn bộ root volume. Nếu source là
toàn bộ `D:\`, không đặt log trên ổ D. Database có thể nằm trên D nếu dùng một
thư mục con chuyên dụng như `D:\SmartMirrorState\manifest.sqlite3`; không đặt
database trực tiếp tại root source và không lưu dữ liệu khác trong thư mục đó.

## Chuẩn bị USN Change Journal

Mở PowerShell bằng quyền Administrator và kiểm tra journal của source, ví dụ D:

```powershell
fsutil usn queryjournal D:
```

Nếu Windows báo `The volume change journal is not active`, kích hoạt journal:

```powershell
fsutil usn createjournal m=268435456 a=67108864 D:
```

Ví dụ trên đặt kích thước tối đa 256 MB và allocation delta 64 MB cho source.
Chế độ `run` không mở USN Journal của replica.

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
- yêu cầu replica trống nếu database chưa có trạng thái B;
- chỉ quét metadata source;
- tạo manifest B rỗng và lưu checkpoint USN source;
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

`sync` đọc phần USN mới của source, cập nhật manifest, lập kế hoạch và thực hiện tối đa 10
vòng cho đến khi hai phía hội tụ. Các hành động có thể gồm:

- `mkdir`: tạo thư mục tại replica;
- `copy`: copy file mới hoặc ghi đè file khác nội dung;
- `verify`: xác minh SHA-256;
- `move`: đổi tên hoặc di chuyển bên trong replica;
- `replace_type`: xử lý xung đột file/thư mục;
- `trash`: chuyển dữ liệu dư tại replica vào quarantine.

Kết quả ví dụ:

```json
{"planned": 5, "completed": 4, "skipped": 1, "failed": 0}
```

`skipped` thường là file `.tmp` đã được ứng dụng nguồn đổi tên hoặc xóa trước
khi tới lượt copy. Đây không phải lỗi đồng bộ; manifest A được cập nhật lại.

`--dry-run` không thay đổi replica/quarantine, nhưng vẫn có thể tạo hoặc cập nhật
database, checkpoint và thư mục replica.

### `reconcile` — ép đối soát đầy đủ

```powershell
python .\smart_mirror.py reconcile --config .\config.json
```

Lệnh này chỉ quét lại toàn bộ source, làm mới checkpoint A và yêu cầu xác minh
hash nguồn khi cần. Nó không quét replica và chưa áp dụng kế hoạch. Sau đó chạy:

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
  "replica_tracking_mode": "managed_expected_state",
  "planned_actions": 10,
  "reconcile_required": false
}
```

`status` không đọc journal và không quét filesystem; `replica_entries` là số mục
B mà database tin đã áp dụng, không phải kết quả quét ổ B. Muốn có kế hoạch cập
nhật nhất, dùng `sync --dry-run`.

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

- manifest source và trạng thái replica đã áp dụng thành công;
- kích thước, `mtime`, SHA-256 và NTFS file ID;
- volume/root identity;
- checkpoint USN;
- lịch sử thao tác và lỗi.

Không xóa database khi chương trình đang chạy. Nếu database mất hoặc hỏng:

1. dừng chế độ `run`;
2. không xóa database nếu replica hiện tại còn dữ liệu;
3. nếu database mất hoàn toàn, dùng replica trống mới hoặc di chuyển dữ liệu B
   cũ ra ngoài trước khi chạy `init`;
4. chạy `sync --dry-run` và kiểm tra kỹ trước khi đồng bộ thật.

Khi chuyển database sang ổ khác, dừng `sync/run` trước. Nếu còn file `-wal` hoặc
`-shm`, dùng SQLite backup API thay vì chỉ copy file `.sqlite3`; không tạo một
database rỗng mới khi replica hiện tại đã có dữ liệu.

## Quarantine

Khi source xóa một file đã được ghi nhận trong manifest B, chương trình move bản
tương ứng ở replica vào:

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

Kích hoạt journal trên source bằng lệnh `fsutil usn createjournal` ở phần chuẩn
bị USN, sau đó chạy lại `reconcile` để lưu checkpoint mới.

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
- Không phát hiện thay đổi trực tiếp tại replica vì chương trình không quét B và
  không đọc USN F. Replica phải là vùng chuyên dụng chỉ do Smart Mirror ghi.
- `reconcile` chỉ xác minh source; muốn kiểm toán vật lý B cần công cụ/audit riêng.
- Không copy symbolic link hoặc junction.
- Hard link chưa được hỗ trợ; đối soát sẽ dừng thay vì tạo manifest sai.
- Không bảo toàn ACL, owner hoặc alternate data streams.
- File đang bị khóa độc quyền có thể không copy được.
- Không sử dụng VSS nên không bảo đảm snapshot nhất quán cho database hoặc file
  đang được nhiều tiến trình cập nhật đồng thời.
- Không thay thế một giải pháp backup có version và bản sao offline.
