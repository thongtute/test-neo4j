"""Các thao tác với video bằng ffmpeg/ffprobe: đọc thông tin, tìm điểm cắt, cắt đoạn, chụp thumbnail."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageStat


class FFmpegError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:]
        raise FFmpegError(f"{cmd[0]} thất bại: {tail}")
    return proc


def _parse_rate(rate: str | None) -> float | None:
    """'30000/1001' -> 29.97"""
    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        return round(n / d, 3) if d else None
    try:
        return float(rate)
    except ValueError:
        return None


def probe(path: Path) -> dict:
    """Đọc thời lượng, fps, kích thước, codec và việc có âm thanh hay không."""
    proc = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError("File không có luồng video.")

    duration = info.get("format", {}).get("duration") or video.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise FFmpegError("Không đọc được thời lượng video.")

    return {
        "duration": round(duration, 3),
        "fps": _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def detect_scene_cuts(path: Path, threshold: float) -> list[float]:
    """Dùng bộ lọc scene của ffmpeg để tìm các thời điểm chuyển cảnh (giây).

    threshold: 0..1, càng nhỏ càng nhạy (cắt nhiều hơn). 0.3 là mức hợp lý cho đa số video.
    """
    vf = f"scale=320:-2,select='gt(scene,{threshold})',showinfo"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-an", "-sn", "-vf", vf, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg dò cảnh thất bại: {(proc.stderr or '')[-800:]}")
    times = [float(t) for t in re.findall(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)", proc.stderr)]
    return sorted(set(times))


def ranges_from_cuts(duration: float, cuts: list[float], min_len: float) -> list[tuple[float, float]]:
    """Biến danh sách điểm cắt thành các khoảng [start, end), bỏ các đoạn ngắn hơn min_len."""
    kept: list[float] = []
    last = 0.0
    for t in sorted(cuts):
        if t - last >= min_len and duration - t >= min_len:
            kept.append(t)
            last = t
    bounds = [0.0, *kept, duration]
    return [(round(a, 3), round(b, 3)) for a, b in zip(bounds, bounds[1:])]


def fixed_ranges(duration: float, seconds: float) -> list[tuple[float, float]]:
    """Cắt đều mỗi `seconds` giây. Phần thừa quá ngắn (< 20% độ dài) được gộp vào đoạn cuối."""
    out: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 1e-6:
        end = min(start + seconds, duration)
        out.append((round(start, 3), round(end, 3)))
        start = end
    if len(out) >= 2 and (out[-1][1] - out[-1][0]) < seconds * 0.2:
        last = out.pop()
        out[-1] = (out[-1][0], last[1])
    return out


def cut_segment(src: Path, start: float, end: float, out: Path, has_audio: bool) -> None:
    """Cắt chính xác theo khung hình (encode lại H.264/AAC để xem được trên trình duyệt)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
    cmd += [
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ]
    _run(cmd)


def make_thumbnail(clip: Path, at: float, out: Path) -> None:
    """Chụp 1 khung hình của đoạn cắt (mặc định ở giữa đoạn)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for t in (at, 0.0):
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(t, 0):.3f}", "-i", str(clip),
            "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "3", str(out),
        ])
        if out.exists() and out.stat().st_size > 0:
            return
    raise FFmpegError(f"Không chụp được thumbnail cho {clip.name}")


def dhash(image_path: Path) -> tuple[str, bool]:
    """Dấu vân tay ảnh 64-bit (difference hash) để nhận ra các đoạn giống nhau.

    Trả về (hash dạng hex 16 ký tự, flat). flat=True nếu khung hình gần như một màu
    (màn đen, màn trắng) — những khung này không dùng để so trùng vì dễ báo nhầm.
    """
    with Image.open(image_path) as im:
        gray = im.convert("L")
        flat = ImageStat.Stat(gray).stddev[0] < 6
        px = gray.resize((9, 8), Image.LANCZOS).tobytes()
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{value:016x}", flat


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def timecode(seconds: float) -> str:
    """75.5 -> '00:01:15.500'"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
