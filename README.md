# Cắt video → Neo4j

App chạy bằng Docker trên máy tính:

- **Đầu vào:** một file video (mp4, mov, mkv, avi, webm...).
- **Đầu ra:** video được cắt thành nhiều đoạn `.mp4`, và **mỗi đoạn trở thành một node `Segment` trong Neo4j** — có ID riêng, thời điểm bắt đầu/kết thúc, số khung hình, đường dẫn file, ảnh thumbnail và dấu vân tay hình ảnh. Neo4j biết đoạn nào thuộc video nào, đoạn nào đứng trước/sau, và đoạn nào giống nhau.

Có 2 cách cắt:

| Cách | Hoạt động | Khi nào dùng |
|---|---|---|
| **Theo cảnh** (`scene`) | ffmpeg tự dò chỗ chuyển cảnh (hình thay đổi đột ngột) và cắt tại đó | Phim, vlog, video nhiều cảnh quay |
| **Mỗi N giây** (`fixed`) | Cắt đều, ví dụ 10 giây một đoạn | Camera giám sát, video quay liền một cảnh |

---

## 1. Cài đặt & chạy

Yêu cầu: **Docker Desktop** (Windows/macOS) hoặc Docker + Docker Compose (Linux).

```bash
cd video-neo4j
docker compose up --build
```

Lần đầu mất vài phút để tải image. Khi thấy log `Đã kết nối Neo4j` là xong. Mở:

| Địa chỉ | Là gì |
|---|---|
| http://localhost:8000 | Web app: tải video lên, xem các đoạn đã cắt |
| http://localhost:7474 | Neo4j Browser — đăng nhập `neo4j` / `password123` |

Tắt: `Ctrl + C` hoặc `docker compose down`.
Xoá sạch dữ liệu Neo4j: `docker compose down -v` (file video trong `./data` vẫn còn, xoá tay nếu muốn).

### Dùng web app
1. Chọn file video, chọn cách cắt, bấm **Tải lên & cắt**.
2. Thanh tiến độ chạy; xong sẽ hiện lưới các đoạn kèm thumbnail. Bấm ▶ để xem từng đoạn.
3. Bấm **Chép Cypher** rồi dán vào Neo4j Browser để xem đồ thị.
4. Kết quả chưa ưng? Bấm **Cắt lại** để đổi độ nhạy / cách cắt (đoạn cũ trong Neo4j được thay thế).

### Dùng dòng lệnh (không cần web)
Bỏ video vào thư mục `./data/input/` rồi chạy:

```bash
# cắt theo cảnh
docker compose run --rm app python cli.py /data/input/video.mp4

# cắt mỗi 10 giây
docker compose run --rm app python cli.py /data/input/video.mp4 --mode fixed --seconds 10

# dò cảnh nhạy hơn, mỗi đoạn tối thiểu 2 giây
docker compose run --rm app python cli.py /data/input/video.mp4 --threshold 0.2 --min-len 2
```

---

## 2. Neo4j "nhận diện" đoạn cắt như thế nào

```
(:Video)──[:HAS_SEGMENT {order}]──▶(:Segment #1)──[:NEXT]──▶(:Segment #2)──[:NEXT]──▶(:Segment #3)
                                          │
                                   [:SIMILAR_TO {distance}]
                                          │
                                  (:Segment của video khác)
```

**Node `Video`**

| Thuộc tính | Ý nghĩa |
|---|---|
| `id` | Mã video (12 ký tự, duy nhất) |
| `filename` | Tên file gốc |
| `duration`, `fps`, `width`, `height`, `video_codec`, `has_audio` | Thông số video |
| `status` | `queued` / `processing` / `done` / `error` |
| `split_mode`, `split_params` | Cách cắt đã dùng |
| `segment_count`, `created_at`, `finished_at` | Thống kê |

**Node `Segment`** — mỗi đoạn cắt là một node

| Thuộc tính | Ý nghĩa |
|---|---|
| `id` | `<video_id>_<số thứ tự>`, ví dụ `a1b2c3d4e5f6_0003` — **duy nhất** (có constraint) |
| `video_id`, `index` | Thuộc video nào, là đoạn thứ mấy |
| `start_time`, `end_time`, `duration` | Tính bằng giây trong video gốc |
| `start_tc`, `end_tc` | Dạng dễ đọc `00:01:15.500` |
| `start_frame`, `end_frame` | Số khung hình trong video gốc |
| `file_path`, `url` | File đoạn cắt trong container / link xem qua web app |
| `thumbnail_url` | Ảnh đại diện (khung hình giữa đoạn) |
| `dhash` | Dấu vân tay hình ảnh 64-bit (hex) — dùng để nhận ra đoạn trùng/giống |
| `flat` | `true` nếu khung hình gần như một màu (màn đen...) — không dùng để so trùng |

**Quan hệ**

- `HAS_SEGMENT`: video → đoạn của nó.
- `NEXT`: đoạn → đoạn kế tiếp, nên có thể đi theo thứ tự thời gian.
- `SIMILAR_TO`: hai đoạn có hình ảnh gần giống (kể cả ở **hai video khác nhau**). `distance` càng nhỏ càng giống (0 = giống hệt). Ngưỡng chỉnh bằng `SIMILARITY_MAX_DISTANCE` trong `.env`.

---

## 3. Câu Cypher mẫu (dán vào Neo4j Browser)

```cypher
// Toàn bộ video và các đoạn
MATCH p=(v:Video)-[:HAS_SEGMENT]->(s:Segment) RETURN p LIMIT 300

// Các đoạn của 1 video theo đúng thứ tự
MATCH (v:Video {filename:'video.mp4'})-[:HAS_SEGMENT]->(s:Segment)
RETURN s.index, s.start_tc, s.end_tc, s.duration, s.url
ORDER BY s.index

// Giây thứ 42 của video nằm trong đoạn nào?
MATCH (v:Video {filename:'video.mp4'})-[:HAS_SEGMENT]->(s:Segment)
WHERE s.start_time <= 42 AND 42 < s.end_time
RETURN s

// Chuỗi đoạn theo thời gian
MATCH p=(first:Segment {index:1})-[:NEXT*]->(last:Segment)
WHERE first.video_id = '<video_id>' AND NOT EXISTS { (last)-[:NEXT]->() }
RETURN p

// Các đoạn giống nhau giữa các video khác nhau
MATCH (a:Segment)-[r:SIMILAR_TO]-(b:Segment)
WHERE a.video_id < b.video_id
RETURN a.id, a.start_tc, b.id, b.start_tc, r.distance
ORDER BY r.distance

// 5 đoạn dài nhất
MATCH (s:Segment) RETURN s.id, s.duration ORDER BY s.duration DESC LIMIT 5
```

Trong Neo4j Browser, để node hiện số thứ tự thay vì ID: bấm nhãn `Segment` ở khung kết quả → chọn caption `index` hoặc `start_tc`.

---

## 4. API (cho ai muốn gọi từ code khác)

| Method | Đường dẫn | Mô tả |
|---|---|---|
| POST | `/api/videos` | Tải video lên (form-data: `file`, `mode`, `threshold`, `min_len`, `seconds`) |
| GET | `/api/videos` | Danh sách video |
| GET | `/api/videos/{id}` | Chi tiết video + các đoạn + đoạn giống |
| GET | `/api/videos/{id}/at?t=42` | Đoạn chứa giây thứ 42 |
| POST | `/api/videos/{id}/resplit` | Cắt lại (JSON: `mode`, `threshold`, `min_len`, `seconds`) |
| DELETE | `/api/videos/{id}` | Xoá video, các đoạn và node trong Neo4j |

Tài liệu tương tác: http://localhost:8000/docs

Ví dụ bằng curl:
```bash
curl -F "file=@video.mp4" -F "mode=fixed" -F "seconds=5" http://localhost:8000/api/videos
```

---

## 5. Cấu trúc thư mục

```
video-neo4j/
├── docker-compose.yml     # 2 service: neo4j + app
├── .env.example           # mật khẩu, ngưỡng so trùng
├── data/                  # (tự tạo) video gốc + đoạn cắt
│   ├── input/             # bỏ video vào đây khi dùng dòng lệnh
│   └── videos/<id>/
│       ├── original.mp4
│       ├── segments/seg_0001.mp4 ...
│       └── thumbs/seg_0001.jpg ...
└── app/
    ├── Dockerfile
    ├── main.py            # web server + API (FastAPI)
    ├── pipeline.py        # quy trình xử lý 1 video
    ├── splitter.py        # ffmpeg: đọc thông tin, dò cảnh, cắt, thumbnail, dấu vân tay
    ├── graph.py           # đọc/ghi Neo4j
    ├── cli.py             # chạy bằng dòng lệnh
    └── static/index.html  # giao diện web
```

---

## 6. Gặp lỗi?

- **Cổng 7474 / 7687 / 8000 đã bị dùng** → đổi cổng bên trái trong `docker-compose.yml`, ví dụ `"8080:8000"`.
- **App báo không kết nối được Neo4j / sai mật khẩu** → bạn đã đổi `NEO4J_PASSWORD` sau lần chạy đầu. Chạy `docker compose down -v` rồi `docker compose up --build`.
- **Cắt theo cảnh ra quá ít đoạn** → giảm độ nhạy (ví dụ 0.2). **Quá nhiều đoạn** → tăng (0.4–0.5) hoặc tăng độ dài tối thiểu.
- **Video dài cắt chậm** → app encode lại từng đoạn để cắt chính xác tới từng khung hình và xem được trên trình duyệt; tốc độ phụ thuộc CPU. Mỗi lúc chỉ xử lý 1 video, video khác sẽ ở trạng thái "chờ".
- Xem log: `docker compose logs -f app`
