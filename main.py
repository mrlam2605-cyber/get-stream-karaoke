import re
import time
from typing import Optional
from fastapi import FastAPI, Query
from pydantic import BaseModel
import yt_dlp

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

@app.get("/extract", response_model=ExtractedMedia)
def extract_stream(url: str = Query(..., description="YouTube Video URL or Video ID")):
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}"

    # Danh sách các tổ hợp client tốt nhất hiện nay cho IP Datacenter
    strategies = [
        {'player_client': ['ios']},
        {'player_client': ['android']},
        {'player_client': ['tv_embedded', 'mweb']}
    ]

    for strategy in strategies:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'ignoreerrors': True,
            'geo_bypass': True,
            'socket_timeout': 10,
            'extractor_args': {
                'youtube': strategy
            }
        }

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
                    # Bỏ qua m3u8 và manifest phân đoạn
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

                # 1. Tìm video riêng + audio riêng chất lượng cao nhất
                best_video = max(video_only, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
                best_audio = max(audio_only, key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), default=None)

                if best_video and best_audio:
                    return ExtractedMedia(
                        videoUrl=best_video.get('url'),
                        audioUrl=best_audio.get('url'),
                        isProgressive=False
                    )

                # 2. Fallback sang stream Progressive (có cả hình + tiếng)
                best_prog = max(progressive, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), default=None)
                if best_prog:
                    return ExtractedMedia(
                        videoUrl=best_prog.get('url'),
                        audioUrl=None,
                        isProgressive=True
                    )

        except Exception as e:
            print(f"[Extractor Debug] Thử client {strategy} thất bại: {e}")
            continue

    return ExtractedMedia(videoUrl=None, audioUrl=None, isProgressive=False)

@app.get("/ping")
def ping():
    return {"status": "alive", "uptime": f"{int(time.time() - START_TIME)}s"}