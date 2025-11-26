import os
import shutil
import subprocess
import time
import math
import tempfile
import json
import re
import random
import platform
from io import BytesIO
from typing import Optional, List

# --- FRAMEWORK IMPORTS ---
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- CORE LOGIC IMPORTS ---
import google.generativeai as genai
import imageio_ffmpeg
import yt_dlp
from moviepy.video.io.VideoFileClip import VideoFileClip
from moviepy.audio.io.AudioFileClip import AudioFileClip
from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs
from fpdf import FPDF

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="."), name="static")

# --- SECRETS ---
SERVER_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not SERVER_API_KEY:
    possible_keys = ["google_key", "google_api_key", "google_key.txt"]
    for name in possible_keys:
        path = f"/etc/secrets/{name}"
        if os.path.exists(path):
            try:
                with open(path, "r") as f: SERVER_API_KEY = f.read().strip()
                break
            except: pass

def resolve_api_key(user_key: Optional[str] = None) -> str:
    final_key = user_key if user_key and user_key.strip() else SERVER_API_KEY
    if not final_key: return ""
    return final_key

# --- FFmpeg ---
def get_ffmpeg_command():
    if shutil.which("ffmpeg"): return "ffmpeg"
    return "ffmpeg" 

# --- HELPER FUNCTIONS ---
def get_system_prompt(detail_level, context_type, part_info="", custom_focus=""):
    base = f"You are an expert Academic Tutor. {part_info} "
    if custom_focus: base += f"\nIMPORTANT: The user specifically requested: '{custom_focus}'. PRIORITIZE THIS.\n"
    base += "STRUCTURE REQUIREMENTS:\n1. Start with a '## ⚡ TL;DR' section.\n2. Then, provide the main notes using Markdown headers (##) and bullet points."
    return base

def get_media_duration(file_path):
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.m4a', '.mp3']: clip = AudioFileClip(file_path)
        else: clip = VideoFileClip(file_path)
        duration = clip.duration; clip.close(); return duration
    except: return 0

def cut_media_fast(input_path, output_path, start_time, end_time):
    cmd_exec = get_ffmpeg_command() 
    cmd = [cmd_exec, "-y", "-i", input_path, "-ss", str(start_time), "-to", str(end_time), "-c", "copy", output_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_video_id(url):
    try:
        query = urlparse(url)
        if query.hostname == 'youtu.be': return query.path[1:]
        if query.hostname in ('www.youtube.com', 'youtube.com'):
            if query.path == '/watch': return parse_qs(query.query)['v'][0]
    except: return None

def get_transcript(video_id):
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([line['text'] for line in transcript_list])
    except: return None

def download_youtube_media(url, mode="audio"):
    temp_dir = tempfile.gettempdir()
    ext = "mp4" if mode == "video" else "mp3"
    out_path = os.path.join(temp_dir, f"yt_{mode}_{int(time.time())}.{ext}")
    
    # Use 'bestaudio/best' for flexibility
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_path.replace(f".{ext}", "") + ".%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'force_ipv4': True,
        'verbose': True,
        
        # --- ANDROID MODE (Correctly Configured) ---
        'extractor_args': {
            'youtube': {
                'player_client': ['android']
            }
        }
        # Note: NO manual 'http_headers' here! Let yt-dlp handle it.
    }

    if mode == "audio":
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
        ydl_opts['outtmpl'] = out_path.replace(".mp3", "")

    # --- COOKIE LOADING ---
    try:
        if os.path.exists("/etc/secrets"):
            possible_cookies = ["youtube_cookies", "youtube_cookies.txt", "cookies", "cookies.txt"]
            for cookie_name in possible_cookies:
                read_only_path = f"/etc/secrets/{cookie_name}"
                if os.path.exists(read_only_path):
                    print(f"Found cookies at {read_only_path}")
                    writable_path = os.path.join(temp_dir, "clean_cookies.txt")
                    with open(read_only_path, 'r', encoding='utf-8') as infile:
                        content = infile.read().replace('\r\n', '\n').replace('\r', '\n')
                        if "# Netscape HTTP Cookie File" not in content:
                            content = "# Netscape HTTP Cookie File\n" + content
                    with open(writable_path, 'w', encoding='utf-8') as outfile:
                        outfile.write(content)
                    
                    ydl_opts['cookiefile'] = writable_path
                    break
    except Exception as e: print(f"Cookie error: {e}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
        final_path = out_path
        if mode == 'audio' and not os.path.exists(final_path):
             if os.path.exists(out_path + ".mp3"): final_path = out_path + ".mp3"
        return final_path
    except Exception as e:
        print(f"DL Error: {e}")
        raise Exception(f"YouTube Download Error: {str(e)}")

# --- API ENDPOINTS ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    try:
        with open("index.html", "r", encoding="utf-8") as f: return f.read()
    except: return "Error"

@app.get("/api-status")
async def get_api_status(): return {"has_key": SERVER_API_KEY is not None}

@app.post("/process-lecture")
async def process_lecture_api(file: UploadFile = File(None), url: Optional[str] = Form(None), mode: str = Form("transcript"), api_key: Optional[str] = Form(None), detail_level: str = Form(...), custom_focus: str = Form("")):
    valid_key = resolve_api_key(api_key)
    genai.configure(api_key=valid_key)
    final_notes = []; temp_file_path = None; is_downloaded = False
    try:
        if url:
            if mode == "transcript":
                vid = get_video_id(url)
                if vid:
                    txt = get_transcript(vid)
                    if txt:
                        model = genai.GenerativeModel("gemini-2.0-flash-lite")
                        res = model.generate_content([get_system_prompt(detail_level, "transcript", "", custom_focus), txt])
                        return JSONResponse(content={"status": "success", "notes": res.text})
                    else: mode = "audio"
            if mode in ["audio", "video"]:
                temp_file_path = download_youtube_media(url, mode); is_downloaded = True
        elif file:
            _, ext = os.path.splitext(file.filename)
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp: shutil.copyfileobj(file.file, tmp); temp_file_path = tmp.name
        
        if temp_file_path:
            model = genai.GenerativeModel("gemini-2.0-flash-lite")
            if os.path.splitext(temp_file_path)[1] in ['.txt','.md']:
                 with open(temp_file_path,'r',encoding='utf-8') as f: final_notes.append(model.generate_content([get_system_prompt(detail_level,"transcript","",custom_focus), f.read()]).text)
            else:
                dur = get_media_duration(temp_file_path)
                chunk = 1200; chunks = math.ceil(dur / chunk)
                for i in range(chunks):
                    start = i * chunk; end = min((i + 1) * chunk, dur)
                    c_path = f"temp_chunk_{i}{os.path.splitext(temp_file_path)[1]}"
                    cut_media_fast(temp_file_path, c_path, start, end)
                    try:
                        v_file = genai.upload_file(path=c_path)
                        while v_file.state.name == "PROCESSING": time.sleep(2); v_file = genai.get_file(v_file.name)
                        c_type = "video" if mode == "video" else "audio"
                        res = model.generate_content([v_file, get_system_prompt(detail_level, c_type, f"Part {i+1}/{chunks}", custom_focus)])
                        final_notes.append(res.text)
                    finally: 
                        if os.path.exists(c_path): os.unlink(c_path)
        return JSONResponse(content={"status": "success", "notes": "\n\n".join(final_notes)})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally:
        if is_downloaded and temp_file_path and os.path.exists(temp_file_path):
            try: os.unlink(temp_file_path)
            except: pass

@app.post("/chat")
async def chat_api(req: BaseModel): pass
@app.post("/generate-quiz")
async def generate_quiz_api(req: BaseModel): pass
@app.post("/generate-mindmap")
async def generate_mindmap_api(req: BaseModel): pass
@app.post("/generate-pdf")
async def generate_pdf_api(req: BaseModel): pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)