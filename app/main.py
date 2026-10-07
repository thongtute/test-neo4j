"""Web app: tải video lên, cắt thành các đoạn và lưu vào Neo4j."""
from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import pipeline
from graph import Graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv",
               ".mpg", ".mpeg", ".3gp", ".ts", ".mts"}

graph: Graph | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global graph
    (DATA_DIR / "videos").mkdir(parents=True, exist_ok=True)
    graph = Graph.from_env()
    graph.wait_until_ready()
    graph.init_schema()
    graph.mark_interrupted()
    yield
    graph.close()


app = FastAPI(title="Video Splitter → Neo4j", lifespan=lifespan)
DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=DATA_DIR), name="media")


def _run_job(video_id: str, src: Path, params: dict) -> None:
    try:
        result = pipeline.process_video(graph, video_id, src, params, DATA_DIR)
        log.info("Xong: %s", result)
    except Exception:
        pass  # lỗi đã được ghi vào node Video


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/videos")
def upload_video(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    mode: str = Form("scene"),
    threshold: float = Form(0.3),
    seconds: float = Form(10.0),
    min_len: float = Form(1.0),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Định dạng {ext or '(không rõ)'} không được hỗ trợ.")
    try:
        params = pipeline.validate_params(mode, threshold, seconds, min_len)
    except ValueError as e:
        raise HTTPException(400, str(e))

    video_id = pipeline.new_video_id()
    vdir = pipeline.video_dir(DATA_DIR, video_id)
    vdir.mkdir(parents=True, exist_ok=True)
    src = vdir / f"original{ext}"
    with src.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=1024 * 1024)

    rel = src.relative_to(DATA_DIR).as_posix()
    graph.create_video({
        "id": video_id,
        "filename": Path(file.filename).name,
        "stored_path": str(src),
        "url": f"/media/{rel}",
        "size_bytes": src.stat().st_size,
        "status": "queued",
        "progress": 0,
        "created_at": pipeline.now_iso(),
    })
    background.add_task(_run_job, video_id, src, params)
    return {"id": video_id, "status": "queued"}


@app.get("/api/videos")
def list_videos():
    return graph.list_videos()


@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    v = graph.get_video(video_id)
    if not v:
        raise HTTPException(404, "Không tìm thấy video")
    return v


@app.get("/api/videos/{video_id}/at")
def segment_at(video_id: str, t: float):
    """Đoạn nào chứa giây thứ t?  Ví dụ: /api/videos/abc123/at?t=42"""
    seg = graph.segment_at(video_id, t)
    if not seg:
        raise HTTPException(404, "Không có đoạn nào chứa thời điểm này")
    return seg


class SplitParams(BaseModel):
    mode: str = "scene"
    threshold: float = 0.3
    seconds: float = 10.0
    min_len: float = 1.0


@app.post("/api/videos/{video_id}/resplit")
def resplit(video_id: str, body: SplitParams, background: BackgroundTasks):
    v = graph.get_video(video_id)
    if not v:
        raise HTTPException(404, "Không tìm thấy video")
    if v.get("status") in ("queued", "processing"):
        raise HTTPException(409, "Video đang được xử lý, đợi xong rồi cắt lại.")
    try:
        params = pipeline.validate_params(body.mode, body.threshold, body.seconds, body.min_len)
    except ValueError as e:
        raise HTTPException(400, str(e))
    graph.update_video(video_id, {"status": "queued", "progress": 0, "error": None})
    background.add_task(_run_job, video_id, Path(v["stored_path"]), params)
    return {"id": video_id, "status": "queued"}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    v = graph.get_video(video_id)
    if not v:
        raise HTTPException(404, "Không tìm thấy video")
    if v.get("status") == "processing":
        raise HTTPException(409, "Video đang được xử lý, đợi xong rồi xoá.")
    graph.delete_video(video_id)
    shutil.rmtree(pipeline.video_dir(DATA_DIR, video_id), ignore_errors=True)
    return {"deleted": video_id}
