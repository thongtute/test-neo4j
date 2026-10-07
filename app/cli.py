"""Chạy từ dòng lệnh (không cần mở web).

Ví dụ (từ thư mục project, file nằm trong ./data/input):
    docker compose run --rm app python cli.py /data/input/video.mp4
    docker compose run --rm app python cli.py /data/input/video.mp4 --mode fixed --seconds 10
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

import pipeline
from graph import Graph


def main() -> int:
    ap = argparse.ArgumentParser(description="Cắt video và lưu các đoạn vào Neo4j")
    ap.add_argument("video", type=Path, help="đường dẫn file video")
    ap.add_argument("--mode", choices=["scene", "fixed"], default="scene",
                    help="scene = cắt theo chuyển cảnh, fixed = cắt đều mỗi N giây")
    ap.add_argument("--threshold", type=float, default=0.3, help="độ nhạy dò cảnh 0.01–0.99 (nhỏ = cắt nhiều)")
    ap.add_argument("--min-len", type=float, default=1.0, help="độ dài tối thiểu mỗi đoạn (giây), chế độ scene")
    ap.add_argument("--seconds", type=float, default=10.0, help="độ dài mỗi đoạn (giây), chế độ fixed")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.video.is_file():
        print(f"Không thấy file: {args.video}", file=sys.stderr)
        return 1

    params = pipeline.validate_params(args.mode, args.threshold, args.seconds, args.min_len)
    data_dir = Path(os.getenv("DATA_DIR", "/data"))

    graph = Graph.from_env()
    graph.wait_until_ready()
    graph.init_schema()

    video_id = pipeline.new_video_id()
    vdir = pipeline.video_dir(data_dir, video_id)
    vdir.mkdir(parents=True, exist_ok=True)
    src = vdir / f"original{args.video.suffix.lower()}"
    shutil.copy2(args.video, src)

    graph.create_video({
        "id": video_id,
        "filename": args.video.name,
        "stored_path": str(src),
        "url": f"/media/{src.relative_to(data_dir).as_posix()}",
        "size_bytes": src.stat().st_size,
        "status": "queued",
        "progress": 0,
        "created_at": pipeline.now_iso(),
    })
    try:
        result = pipeline.process_video(graph, video_id, src, params, data_dir)
    finally:
        graph.close()

    print(f"\nXong. Video id = {video_id}")
    print(f"  Số đoạn: {result['segments']}  |  Cặp đoạn giống nhau: {result['similar_links']}")
    print("  Xem trong Neo4j Browser (http://localhost:7474):")
    print(f"  MATCH p=(v:Video {{id:'{video_id}'}})-[:HAS_SEGMENT]->(s) RETURN p")
    return 0


if __name__ == "__main__":
    sys.exit(main())
