import os
import base64
import logging
import re
import sys
import time
import traceback
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query
from pydantic import BaseModel
import yt_dlp


COOKIE_FILE_PATH = "/tmp/cookies.txt"

# Lấy giá trị từ một trong hai biến môi trường
raw_cookie_data = os.getenv("COOKIES_B64") or os.getenv("COOKIES")

if raw_cookie_data:
    raw_cookie_data = raw_cookie_data.strip()
    
    # Kiểm tra xem chuỗi có phải là Base64 (bắt đầu bằng 'IyB' tức là '# ') hay text thường
    if raw_cookie_data.startswith("IyB") or not raw_cookie_data.startswith("#"):
        try:
            # Giải mã Base64 nhị phân chuẩn Netscape
            decoded_bytes = base64.b64decode(raw_cookie_data)
            with open(COOKIE_FILE_PATH, "wb") as f:
                f.write(decoded_bytes)
            print("-> [COOKIE] Đã giải mã thành công chuỗi Base64 vào /tmp/cookies.txt")
        except Exception as e:
            print(f"-> [COOKIE ERROR] Lỗi giải mã Base64: {e}")
    else:
        # Trường hợp chuỗi truyền vào là text thuần đã có sẵn ký tự '#'
        with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(raw_cookie_data)
        print("-> [COOKIE] Đã ghi nhận file cookie dạng raw text vào /tmp/cookies.txt")
else:
    print("-> [COOKIE WARNING] Không tìm thấy biến môi trường COOKIES hoặc COOKIES_B64!")

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


def get_base_ydl_opts(client_list: list) -> dict:
    has_cookie = os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0
    return {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'ignoreerrors': True,
        'geo_bypass': True,
        'socket_timeout': 15,
        'cookiefile': COOKIE_FILE_PATH if has_cookie else None,
        # Tránh crash khi một client nào đó không nhả format:
        'ignore_no_formats_error': True,
        'extractor_args': {
            'youtube': {
                'player_client': client_list
            }
        }
    }

@app.get("/extract", response_model=ExtractedMedia)
def extract_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"

    # Bộ 3 chiến lược tối ưu nhất khi đã có Cookie:
    strategies = [
        ['ios', 'mweb'],      # Nhận cookie rất tốt, không đòi PO-Token desktop
        ['android', 'ios'],   # Trích xuất stream DASH / Progressive mượt mà
        ['tv_embedded', 'mweb']
    ]

    for strategy in strategies:
        ydl_opts = get_base_ydl_opts(strategy)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if not info:
                    continue

                formats = info.get('formats', [])
                if not formats:
                    continue

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

                # 1. Ưu tiên video riêng + audio riêng
                best_video = max(video_only, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
                best_audio = max(audio_only, key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), default=None)

                if best_video and best_audio:
                    return ExtractedMedia(
                        videoUrl=best_video.get('url'),
                        audioUrl=best_audio.get('url'),
                        isProgressive=False
                    )

                # 2. Dự phòng progressive
                best_prog = max(progressive, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
                if best_prog:
                    return ExtractedMedia(
                        videoUrl=best_prog.get('url'),
                        audioUrl=None,
                        isProgressive=True
                    )
        except Exception as e:
            logger.error(f"Thử nghiệm {strategy} thất bại: {e}")
            continue

    return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)


@app.get("/debug")
def debug_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"

    # Đổi sang ['ios', 'mweb'] thay vì ['web']
    ydl_opts = get_base_ydl_opts(['ios', 'mweb'])

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            formats = info.get('formats', []) if info else []
            return {
                "video_id": video_id,
                "cookie_loaded": os.path.exists(COOKIE_FILE_PATH),
                "total_formats": len(formats),
                "formats": [
                    {
                        "itag": f.get("format_id"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "height": f.get("height"),
                        "has_url": bool(f.get("url"))
                    }
                    for f in formats
                ]
            }
    except Exception as e:
        return {
            "video_id": video_id,
            "cookie_loaded": os.path.exists(COOKIE_FILE_PATH),
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc()
        }
    
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
        'cookiefile': COOKIE_FILE_PATH if os.path.exists(COOKIE_FILE_PATH) else None,
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