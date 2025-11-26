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

# --- CONFIGURATION ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="."), name="static")

# --- SECRETS MANAGEMENT ---
SERVER_API_KEY = os.environ.get("GOOGLE_API_KEY")

if not SERVER_API_KEY:
    possible_keys = ["google_key", "google_api_key", "google_key.txt"]
    for name in possible_keys:
        path = f"/etc/secrets/{name}"
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    SERVER_API_KEY = f.read().strip()
                print(f"Loaded API Key from {name}")
                break
            except: pass

def resolve_api_key(user_key: Optional[str] = None) -> str:
    final_key = user_key if user_key and user_key.strip() else SERVER_API_KEY
    if not final_key: return ""
    return final_key

# --- FFmpeg SETUP ---
def get_ffmpeg_command():
    if shutil.which("ffmpeg"): return "ffmpeg"
    return "ffmpeg" 

# --- HELPER FUNCTIONS ---
def get_system_prompt(detail_level, context_type, part_info="", custom_focus=""):
    base = f"You are an expert Academic Tutor. {part_info} "
    if custom_focus:
        base += f"\nIMPORTANT: The user specifically requested: '{custom_focus}'. PRIORITIZE THIS.\n"
    base += """
    STRUCTURE REQUIREMENTS:
    1. Start with a '## ⚡ TL;DR' section (Core Topic, Exam Probability, Difficulty).
    2. Then, provide the main notes using Markdown headers (##) and bullet points.
    """
    if "Summary" in detail_level: return base + f"Create a CONCISE SUMMARY of this {context_type}."
    elif "Exhaustive" in detail_level: return base + f"Create EXHAUSTIVE NOTES of this {context_type}."
    else: return base + f"Create STANDARD STUDY NOTES of this {context_type}."

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
    except Exception:
        return None

def download_youtube_media(url, mode="audio"):
    temp_dir = tempfile.gettempdir()
    ext = "mp4" if mode == "video" else "m4a"
    out_path = os.path.join(temp_dir, f"yt_{mode}_{int(time.time())}.{ext}")
    
    fmt = 'best[ext=mp4][height<=720]' if mode == "video" else 'bestaudio[ext=m4a]/bestaudio'
    
    ydl_opts = {
        'format': fmt,
        'outtmpl': out_path,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        # --- iOS MODE (No Cookies) ---
        # We REMOVED the cookie logic because the geo-mismatch was triggering the block.
        # Instead, we mimic an iOS device (iPhone), which often bypasses these checks.
        'extractor_args': {
            'youtube': {
                'player_client': ['ios']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: 
            ydl.download([url])
        return out_path
    except Exception as e:
        raise Exception(f"YouTube Download Error: {str(e)}")

# --- API ENDPOINTS ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    try:
        with open("index.html", "r", encoding="utf-8") as f: return f.read()
    except FileNotFoundError: return "Error: index.html not found"

@app.get("/api-status")
async def get_api_status():
    return {"has_key": SERVER_API_KEY is not None}

@app.post("/process-lecture")
async def process_lecture_api(
    file: UploadFile = File(None),
    url: Optional[str] = Form(None),
    mode: str = Form("transcript"), 
    api_key: Optional[str] = Form(None),
    detail_level: str = Form(...),
    custom_focus: str = Form("")
):
    valid_key = resolve_api_key(api_key)
    if not valid_key: raise HTTPException(status_code=400, detail="API Key Required")
    
    genai.configure(api_key=valid_key)
    final_notes = []; temp_file_path = None; is_downloaded = False
    
    try:
        if url:
            if mode == "transcript":
                vid = get_video_id(url)
                if not vid: raise HTTPException(status_code=400, detail="Could not parse YouTube URL.")
                txt = get_transcript(vid)
                if txt:
                    model = genai.GenerativeModel("gemini-2.0-flash-lite")
                    res = model.generate_content([get_system_prompt(detail_level, "transcript", "", custom_focus), txt])
                    return JSONResponse(content={"status": "success", "notes": res.text})
                else: 
                    print("Transcript failed, falling back to audio download...")
                    mode = "audio"
            
            if mode in ["audio", "video"]:
                try:
                    temp_file_path = download_youtube_media(url, mode)
                    is_downloaded = True
                except Exception as e:
                    raise HTTPException(status_code=400, detail=str(e))

        elif file:
            _, ext = os.path.splitext(file.filename)
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp: shutil.copyfileobj(file.file, tmp); temp_file_path = tmp.name
        
        if temp_file_path:
            if os.path.splitext(temp_file_path)[1].lower() in ['.txt', '.md']:
                with open(temp_file_path, 'r', encoding='utf-8') as f: 
                    model = genai.GenerativeModel("gemini-2.0-flash-lite")
                    res = model.generate_content([get_system_prompt(detail_level, "transcript", "", custom_focus), f.read()])
                    final_notes.append(res.text)
            else:
                model = genai.GenerativeModel("gemini-2.0-flash-lite")
                dur = get_media_duration(temp_file_path)
                if dur == 0: raise HTTPException(status_code=400, detail="Invalid media file (0 duration).")
                
                chunk = 1200; chunks = math.ceil(dur / chunk)
                for i in range(chunks):
                    start = i * chunk; end = min((i + 1) * chunk, dur)
                    ext = os.path.splitext(temp_file_path)[1]
                    c_path = f"temp_chunk_{i}{ext}"
                    cut_media_fast(temp_file_path, c_path, start, end)
                    try:
                        v_file = genai.upload_file(path=c_path)
                        while v_file.state.name == "PROCESSING": time.sleep(2); v_file = genai.get_file(v_file.name)
                        c_type = "video" if mode == "video" else "audio"
                        res = model.generate_content([v_file, get_system_prompt(detail_level, c_type, f"Part {i+1}/{chunks}", custom_focus)])
                        final_notes.append(res.text)
                    finally: 
                        if os.path.exists(c_path): os.unlink(c_path)
        
        if len(final_notes) > 1:
            try:
                synth_model = genai.GenerativeModel("gemini-2.0-flash-lite")
                combined_text = "\n\n".join(final_notes)
                synth_prompt = f"""SYNTHESIZE these parts into ONE cohesive study guide. Remove 'Part X' breaks. Combine TL;DRs. Rules: Maintain depth. Prioritize custom focus: '{custom_focus}'. RAW NOTES:\n{combined_text}"""
                synth_response = synth_model.generate_content(synth_prompt)
                final_notes = [synth_response.text]
            except: pass

        return JSONResponse(content={"status": "success", "notes": "\n\n".join(final_notes)})
    except Exception as e: raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")
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