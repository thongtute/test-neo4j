"""Quy trình: đọc video -> tìm điểm cắt -> cắt từng đoạn -> thumbnail + dấu vân tay -> ghi Neo4j."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import splitter as sp
from graph import Graph

log = logging.getLogger(__name__)

SIMILARITY_MAX_DISTANCE = int(os.getenv("SIMILARITY_MAX_DISTANCE", "8"))

# Chỉ xử lý 1 video một lúc để máy không bị quá tải; video khác ở trạng thái "queued".
_job_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_video_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_params(mode: str, threshold: float, seconds: float, min_len: float) -> dict:
    if mode not in ("scene", "fixed"):
        raise ValueError("mode phải là 'scene' hoặc 'fixed'")
    if mode == "scene":
        if not 0.01 <= threshold <= 0.99:
            raise ValueError("threshold phải trong khoảng 0.01 – 0.99")
        if not 0 <= min_len <= 600:
            raise ValueError("min_len phải trong khoảng 0 – 600 giây")
        return {"mode": "scene", "threshold": threshold, "min_len": min_len}
    if not 0.5 <= seconds <= 3600:
        raise ValueError("seconds phải trong khoảng 0.5 – 3600 giây")
    return {"mode": "fixed", "seconds": seconds}


def video_dir(data_dir: Path, video_id: str) -> Path:
    return data_dir / "videos" / video_id


def process_video(graph: Graph, video_id: str, src: Path, params: dict, data_dir: Path) -> dict:
    """Chạy toàn bộ quy trình cho 1 video. Trả về tóm tắt kết quả."""
    with _job_lock:
        return _process(graph, video_id, src, params, data_dir)


def _process(graph: Graph, video_id: str, src: Path, params: dict, data_dir: Path) -> dict:
    vdir = video_dir(data_dir, video_id)
    seg_dir, thumb_dir = vdir / "segments", vdir / "thumbs"
    try:
        graph.update_video(video_id, {
            "status": "processing", "progress": 0, "error": None,
            "split_mode": params["mode"], "split_params": json.dumps(params),
        })

        info = sp.probe(src)
        graph.update_video(video_id, info)
        duration = info["duration"]

        if params["mode"] == "scene":
            cuts = sp.detect_scene_cuts(src, params["threshold"])
            ranges = sp.ranges_from_cuts(duration, cuts, params["min_len"])
        else:
            ranges = sp.fixed_ranges(duration, params["seconds"])
        log.info("Video %s: %d đoạn", video_id, len(ranges))

        # Xoá kết quả cũ (trường hợp cắt lại)
        graph.delete_segments(video_id)
        shutil.rmtree(seg_dir, ignore_errors=True)
        shutil.rmtree(thumb_dir, ignore_errors=True)

        fps = info.get("fps")
        segments = []
        for i, (start, end) in enumerate(ranges, start=1):
            name = f"seg_{i:04d}"
            clip = seg_dir / f"{name}.mp4"
            thumb = thumb_dir / f"{name}.jpg"
            sp.cut_segment(src, start, end, clip, info["has_audio"])
            sp.make_thumbnail(clip, (end - start) / 2, thumb)
            h, flat = sp.dhash(thumb)
            rel_clip = clip.relative_to(data_dir).as_posix()
            rel_thumb = thumb.relative_to(data_dir).as_posix()
            segments.append({
                "id": f"{video_id}_{i:04d}",
                "video_id": video_id,
                "index": i,
                "start_time": start,
                "end_time": end,
                "duration": round(end - start, 3),
                "start_tc": sp.timecode(start),
                "end_tc": sp.timecode(end),
                "start_frame": int(round(start * fps)) if fps else None,
                "end_frame": int(round(end * fps)) if fps else None,
                "file_path": str(clip),
                "url": f"/media/{rel_clip}",
                "thumbnail_url": f"/media/{rel_thumb}",
                "file_size": clip.stat().st_size,
                "dhash": h,
                "flat": flat,
                "created_at": now_iso(),
            })
            graph.update_video(video_id, {"progress": int(i * 95 / len(ranges))})

        graph.add_segments(video_id, segments)
        n_similar = graph.link_similar(video_id, SIMILARITY_MAX_DISTANCE)
        graph.update_video(video_id, {
            "status": "done", "progress": 100,
            "segment_count": len(segments), "finished_at": now_iso(),
        })
        return {"video_id": video_id, "segments": len(segments), "similar_links": n_similar}
    except Exception as e:  # ghi lỗi vào Neo4j để giao diện hiển thị
        log.exception("Xử lý video %s thất bại", video_id)
        graph.update_video(video_id, {"status": "error", "error": str(e)[:1000]})
        raise
