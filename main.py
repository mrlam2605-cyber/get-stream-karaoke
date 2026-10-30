import os
import re
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import yt_dlp
import time


app = FastAPI(title="YouTube Stream Extractor")

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


START_TIME = time.time()

@app.get("/ping")
def ping():
    uptime_seconds = int(time.time() - START_TIME)
    return {
        "status": "alive",
        "uptime": f"{uptime_seconds}s"
    }

@app.get("/extract", response_model=ExtractedMedia)
def extract_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        # Giả lập client để hạn chế tối đa bị YouTube block IP datacenter
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            formats = info.get('formats', [])

            video_only_formats = []
            audio_only_formats = []
            progressive_formats = []

            for f in formats:
                # Bỏ qua các định dạng manifest (m3u8, mpd) nếu chỉ muốn lấy direct direct HTTP stream url
                protocol = f.get('protocol', '')
                if 'm3u8' in protocol or 'mhtml' in protocol:
                    continue

                url_stream = f.get('url')
                if not url_stream:
                    continue

                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')

                has_video = vcodec not in ('none', None)
                has_audio = acodec not in ('none', None)

                if has_video and not has_audio:
                    video_only_formats.append(f)
                elif has_audio and not has_video:
                    audio_only_formats.append(f)
                elif has_video and has_audio:
                    progressive_formats.append(f)

            # 1. Tìm video-only cao nhất và audio-only cao nhất
            best_video = max(video_only_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
            best_audio = max(audio_only_formats, key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), default=None)

            if best_video and best_audio:
                return ExtractedMedia(
                    videoUrl=best_video.get('url'),
                    audioUrl=best_audio.get('url'),
                    isProgressive=False
                )

            # 2. Fallback sang stream Progressive (gộp cả hình và tiếng)
            best_progressive = max(progressive_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
            if best_progressive:
                return ExtractedMedia(
                    videoUrl=best_progressive.get('url'),
                    audioUrl=None,
                    isProgressive=True
                )

            return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)

    except Exception as e:
        # Xử lý khi có lỗi hoặc bị chặn
        return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)

@app.get("/health")
def health_check():
    return {"status": "ok"}