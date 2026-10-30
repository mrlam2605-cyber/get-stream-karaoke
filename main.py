import os
import logging
import re
import sys
import time
import traceback
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query
from pydantic import BaseModel
import yt_dlp

# Đường dẫn tự động tìm file cookies.txt cùng thư mục với main.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("yt-extractor")

app = FastAPI(title="YouTube Stream Extractor")
START_TIME = time.time()

class ExtractedMedia(BaseModel):
    videoUrl: Optional[str] = None
    audioUrl: Optional[str] = None
    isProgressive: bool = False

def extract_video_id(url_or_id: str) -> str:
    clean = url_or_id.strip()
    if re.match(r"^[a-zA-Z0-9_-]{11}$", clean):
        return clean
    match = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})", clean)
    if match:
        return match.group(1)
    if "v=" in clean:
        return clean.split("v=")[1].split("&")[0][:11]
    return clean[-11:]

def process_formats(formats: List[Dict[str, Any]]) -> ExtractedMedia:
    video_only = []
    audio_only = []
    progressive = []

    for f in formats:
        stream_url = f.get('url')
        if not stream_url:
            continue

        protocol = f.get('protocol', '')
        if 'm3u8' in protocol or 'mhtml' in protocol:
            continue

        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')

        has_video = vcodec not in ('none', None)
        has_audio = acodec not in ('none', None)

        if has_video and not has_audio:
            video_only.append(f)
        elif has_audio and not has_video:
            audio_only.append(f)
        elif has_video and has_audio:
            progressive.append(f)

    logger.info(f"-> Thống kê formats: VideoOnly={len(video_only)}, AudioOnly={len(audio_only)}, Progressive={len(progressive)}")

    # 1. Tìm video riêng + audio riêng chất lượng cao nhất
    best_video = max(video_only, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
    best_audio = max(audio_only, key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), default=None)

    if best_video and best_audio:
        logger.info(f"-> Chọn được cặp tách rời: Video {best_video.get('height')}p (itag {best_video.get('format_id')}), Audio {best_audio.get('abr')}kbps (itag {best_audio.get('format_id')})")
        return ExtractedMedia(
            videoUrl=best_video.get('url'),
            audioUrl=best_audio.get('url'),
            isProgressive=False
        )

    # 2. Fallback sang progressive nếu không đủ cả 2 luồng riêng biệt
    best_prog = max(progressive, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
    if best_prog:
        logger.info(f"-> Fallback sang Progressive stream: {best_prog.get('height')}p (itag {best_prog.get('format_id')})")
        return ExtractedMedia(
            videoUrl=best_prog.get('url'),
            audioUrl=None,
            isProgressive=True
        )

    return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)

@app.get("/extract", response_model=ExtractedMedia)
def extract_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info(f"=== BẮT ĐẦU TRÍCH XUẤT: {video_id} ===")

    strategies = [
        {'player_client': ['android', 'ios']},
        {'player_client': ['tv_embedded', 'mweb']},
        {'player_client': ['web']}
    ]

    for index, strategy in enumerate(strategies, start=1):
        logger.info(f"[Chiến lược #{index}] Thử nghiệm client: {strategy['player_client']}")
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'ignoreerrors': True,
            'geo_bypass': True,
            'socket_timeout': 10,
            # THÊM DÒNG NÀY:
            'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
            'extractor_args': {
                'youtube': strategy
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if not info:
                    logger.warning(f"[Chiến lược #{index}] ydl.extract_info trả về None")
                    continue

                formats = info.get('formats', [])
                if not formats:
                    logger.warning(f"[Chiến lược #{index}] Không tìm thấy format nào trong info")
                    continue

                media = process_formats(formats)
                if media.videoUrl:
                    logger.info(f"[Chiến lược #{index}] Thành công! Trả về kết quả.")
                    return media

        except Exception as e:
            logger.error(f"[Chiến lược #{index}] Ngoại lệ xảy ra: {str(e)}")
            logger.error(traceback.format_exc())
            continue

    logger.error(f"=== TẤT CẢ CHIẾN LƯỢC ĐỀU THẤT BẠI CHO VIDEO {video_id} ===")
    return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)

@app.get("/debug")
def debug_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    """Endpoint trả thẳng thông tin format và lỗi ra JSON trên trình duyệt để kiểm tra"""
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"
    logs = []
    
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        # THÊM DÒNG NÀY:
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}}
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            formats = info.get('formats', []) if info else []
            summary = [
                {
                    "itag": f.get("format_id"),
                    "ext": f.get("ext"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "height": f.get("height"),
                    "has_url": bool(f.get("url"))
                }
                for f in formats
            ]
            return {
                "video_id": video_id,
                "total_formats": len(formats),
                "formats_summary": summary
            }
    except Exception as e:
        return {
            "video_id": video_id,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc()
        }

@app.get("/ping")
def ping():
    return {"status": "alive", "uptime": f"{int(time.time() - START_TIME)}s"}