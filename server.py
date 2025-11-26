import os
import shutil
import subprocess
import time
import math
import tempfile
import json
import re
import random
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

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="."), name="static")

# --- SECRETS MANAGEMENT (CLOUD-READY) ---
# 1. Check Environment Variables (Render/Cloud)
SERVER_API_KEY = os.environ.get("GOOGLE_API_KEY")

# 2. If not found, try local file (Dev)
if not SERVER_API_KEY:
    SECRETS_FILE = "secrets.json"
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, "r") as f:
                data = json.load(f)
                SERVER_API_KEY = data.get("GOOGLE_API_KEY")
            print(f"[SUCCESS] Loaded API Key from {SECRETS_FILE}")
        except Exception as e:
            print(f"[WARNING] Could not load secrets file: {e}")

def resolve_api_key(user_key: Optional[str] = None) -> str:
    final_key = user_key if user_key and user_key.strip() else SERVER_API_KEY
    if not final_key:
        raise HTTPException(status_code=400, detail="No API Key found. Please enter one in the sidebar or configure secrets.")
    return final_key

# --- AUTO-SETUP FFmpeg ---
def ensure_ffmpeg_exists():
    current_folder = os.getcwd()
    ffmpeg_local = os.path.join(current_folder, "ffmpeg.exe")
    if not os.path.exists(ffmpeg_local):
        try:
            ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
            shutil.copy(ffmpeg_src, ffmpeg_local)
        except Exception as e:
            print(f"FFmpeg setup warning: {e}")
    return ffmpeg_local

FFMPEG_PATH = ensure_ffmpeg_exists()

# --- SILENCE YOUTUBE WARNINGS ---
class MyLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass 
    def error(self, msg): print(msg) 

# --- DATA MODELS ---
class ChatRequest(BaseModel):
    notes: str
    message: str
    api_key: Optional[str] = None

class QuizRequest(BaseModel):
    notes: str
    api_key: Optional[str] = None

class MindMapRequest(BaseModel):
    notes: str
    api_key: Optional[str] = None

class PDFRequest(BaseModel):
    notes: str

# --- HELPER FUNCTIONS ---
# (Keep all previous helper functions same as before, condensing for brevity in this view)
def get_system_prompt(detail_level, context_type, part_info="", custom_focus=""):
    base = f"You are an expert Academic Tutor. {part_info} "
    if custom_focus:
        base += f"\nIMPORTANT: The user specifically requested: '{custom_focus}'. PRIORITIZE THIS IN THE NOTES.\n"
    base += """
    STRUCTURE REQUIREMENTS:
    1. Start immediately with the content. Do NOT say "Here are your notes".
    2. Start with a '## ⚡ TL;DR' section. Inside it, provide:
       - **Core Topic**: (1 sentence)
       - **Exam Probability**: (High/Medium/Low)
       - **Difficulty**: (1-10 scale)
    3. Then, provide the main notes below using clear Markdown headers (##) and bullet points.\n
    """
    if "Summary" in detail_level: return base + f"Create a CONCISE SUMMARY of this {context_type}."
    elif "Exhaustive" in detail_level: return base + f"Create EXHAUSTIVE NOTES of this {context_type}. Include minute-by-minute details."
    else: return base + f"Create STANDARD STUDY NOTES of this {context_type}."

def get_media_duration(file_path):
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.m4a', '.mp3']: clip = AudioFileClip(file_path)
        else: clip = VideoFileClip(file_path)
        duration = clip.duration; clip.close(); return duration
    except: return 0

def cut_media_fast(input_path, output_path, start_time, end_time):
    ffmpeg_exe = os.path.abspath("ffmpeg.exe")
    cmd = [ffmpeg_exe, "-y", "-i", input_path, "-ss", str(start_time), "-to", str(end_time), "-c", "copy", output_path]
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
    try:
        temp_dir = tempfile.gettempdir()
        ext = "mp4" if mode == "video" else "m4a"
        out_path = os.path.join(temp_dir, f"yt_{mode}_{int(time.time())}.{ext}")
        ffmpeg_local = os.path.abspath("ffmpeg.exe")
        fmt = 'best[ext=mp4][height<=720]' if mode == "video" else 'bestaudio[ext=m4a]/bestaudio'
        ydl_opts = {'format': fmt, 'outtmpl': out_path, 'ffmpeg_location': ffmpeg_local, 'quiet': True, 'no_warnings': True, 'logger': MyLogger()}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
        return out_path
    except Exception as e: return None

class ModernPDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 20); self.set_text_color(44, 62, 80); self.cell(0, 10, 'Lecture Notes', 0, 1, 'L')
        self.set_font('Helvetica', 'I', 10); self.set_text_color(127, 140, 141); self.cell(0, 10, 'Generated by Lecture-to-Notes Pro', 0, 0, 'R')
        self.ln(12); self.set_draw_color(233, 64, 87); self.set_line_width(1.5); self.line(10, 32, 200, 32); self.ln(10)
    def footer(self):
        self.set_y(-15); self.set_font('Helvetica', 'I', 8); self.set_text_color(150, 150, 150); self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')
    def chapter_title(self, title):
        self.set_font('Helvetica', 'B', 14); self.set_text_color(255, 255, 255); self.set_fill_color(44, 62, 80)
        clean_title = title.replace('#', '').strip().encode('windows-1252', 'replace').decode('windows-1252')
        self.cell(0, 10, f"  {clean_title}", 0, 1, 'L', 1); self.ln(5)
    def chapter_body(self, body):
        self.set_font('Helvetica', '', 11); self.set_text_color(20, 20, 20)
        for line in body.split('\n'):
            line = line.strip(); 
            if not line: 
                self.ln(2); continue
            safe_line = line.replace('•', chr(149)).replace('—', '-')
            if '**' in safe_line:
                parts = safe_line.split('**')
                for i, part in enumerate(parts):
                    if i % 2 == 0: self.set_font('Helvetica', '', 11)
                    else: self.set_font('Helvetica', 'B', 11)
                    try: self.write(6, part.encode('windows-1252', 'replace').decode('windows-1252'))
                    except: self.write(6, part)
                self.ln(6)
            else:
                self.set_font('Helvetica', '', 11)
                if safe_line.startswith(chr(149)) or safe_line.startswith('-'): self.set_x(15)
                else: self.set_x(10)
                try: self.multi_cell(0, 6, safe_line.encode('windows-1252', 'replace').decode('windows-1252'))
                except: self.multi_cell(0, 6, safe_line)
        self.ln(4)

def convert_markdown_to_pdf(markdown_text):
    pdf = ModernPDF(); pdf.add_page(); pdf.set_auto_page_break(auto=True, margin=15)
    current_body = ""; clean_text = markdown_text.replace('📼', '').replace('📄', '').replace('?', '')
    for line in clean_text.split('\n'):
        if line.startswith('#'):
            if current_body: pdf.chapter_body(current_body); current_body = ""
            pdf.chapter_title(line)
        else: current_body += line + "\n"
    if current_body: pdf.chapter_body(current_body)
    return pdf.output(dest='S').encode('latin-1', 'replace')

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
    genai.configure(api_key=valid_key)
    final_notes = []; temp_file_path = None; is_downloaded = False
    try:
        if url:
            if mode == "transcript":
                vid = get_video_id(url); txt = get_transcript(vid)
                if txt:
                    model = genai.GenerativeModel("gemini-2.5-pro")
                    res = model.generate_content([get_system_prompt(detail_level, "transcript", "", custom_focus), txt])
                    return JSONResponse(content={"status": "success", "notes": res.text})
                else: mode = "audio"
            if mode in ["audio", "video"]:
                temp_file_path = download_youtube_media(url, mode); is_downloaded = True
                if not temp_file_path: raise HTTPException(status_code=400, detail="Download failed.")
        elif file:
            _, ext = os.path.splitext(file.filename)
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp: shutil.copyfileobj(file.file, tmp); temp_file_path = tmp.name
        
        if temp_file_path:
            if os.path.splitext(temp_file_path)[1].lower() in ['.txt', '.md']:
                with open(temp_file_path, 'r', encoding='utf-8') as f: 
                    model = genai.GenerativeModel("gemini-2.5-pro")
                    res = model.generate_content([get_system_prompt(detail_level, "transcript", "", custom_focus), f.read()])
                    final_notes.append(res.text)
            else:
                model = genai.GenerativeModel("gemini-2.5-pro"); dur = get_media_duration(temp_file_path)
                if dur == 0: raise HTTPException(status_code=400, detail="Invalid media.")
                chunk = 2400; chunks = math.ceil(dur / chunk)
                for i in range(chunks):
                    start = i * chunk; end = min((i + 1) * chunk, dur); ext = os.path.splitext(temp_file_path)[1]; c_path = f"temp_chunk_{i}{ext}"
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
                synth_model = genai.GenerativeModel("gemini-2.5-pro")
                combined_text = "\n\n".join(final_notes)
                synth_prompt = f"""SYNTHESIZE these parts into ONE cohesive study guide. Remove 'Part X' breaks. Combine TL;DRs. Rules: Maintain depth. Prioritize custom focus: '{custom_focus}'. RAW NOTES:\n{combined_text}"""
                synth_response = synth_model.generate_content(synth_prompt)
                final_notes = [synth_response.text]
            except: pass

        return JSONResponse(content={"status": "success", "notes": "\n\n".join(final_notes)})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally:
        if is_downloaded and temp_file_path and os.path.exists(temp_file_path):
            try: os.unlink(temp_file_path)
            except: pass

@app.post("/chat")
async def chat_api(req: ChatRequest):
    valid_key = resolve_api_key(req.api_key)
    try:
        genai.configure(api_key=valid_key)
        model = genai.GenerativeModel("gemini-2.5-pro")
        prompt = f"Context:\n{req.notes}\n\nUser Question: {req.message}\n\nAnswer as a helpful tutor:"
        response = model.generate_content(prompt)
        return JSONResponse(content={"response": response.text})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-quiz")
async def generate_quiz_api(req: QuizRequest):
    valid_key = resolve_api_key(req.api_key)
    try:
        genai.configure(api_key=valid_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = f"""Create 5 multiple choice questions. RULES: 1. answer_index MUST be 0-3 integer. 2. options list MUST have 4 strings. 3. No A) prefixes. OUTPUT RAW JSON: [ {{\"question\": \"?\", \"options\": [\"Opt1\", \"Opt2\", \"Opt3\", \"Opt4\"], \"answer_index\": 0 }} ]. NOTES: {req.notes[:15000]}"""
        res = model.generate_content(prompt); text = res.text; s = text.find('['); e = text.rfind(']') + 1
        if s != -1 and e != -1:
            quiz_data = json.loads(text[s:e])
            for q in quiz_data:
                q['options'] = [re.sub(r'^[A-D]\)\s*', '', opt) for opt in q['options']]
                if 0 <= q['answer_index'] < len(q['options']):
                    correct_text = q['options'][q['answer_index']]
                    random.shuffle(q['options'])
                    q['answer_index'] = q['options'].index(correct_text)
            return JSONResponse(content=quiz_data)
        raise Exception("Invalid JSON")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-mindmap")
async def generate_mindmap_api(req: MindMapRequest):
    valid_key = resolve_api_key(req.api_key)
    try:
        genai.configure(api_key=valid_key)
        model = genai.GenerativeModel("gemini-2.5-pro")
        prompt = f"""Create Graphviz DOT code. Start: digraph G {{ graph [rankdir=LR, splines=ortho]; node [shape=box, style="filled", fillcolor="#ffffff", fontname="Arial", fontcolor="#000000", penwidth=1, margin=0.2]; edge [color="#666666"]; 2. COLORS: Root="#FFD700", L1="#D1C4E9", L2="#B3E5FC", L3="#E1F5FE". 3. Max 6 words/label. 4. OUTPUT ONLY RAW CODE inside dot tags. NOTES: {req.notes[:15000]}"""
        res = model.generate_content(prompt)
        return JSONResponse(content={"dot_code": res.text.replace("```dot", "").replace("```", "").replace("graphviz", "").strip()})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-pdf")
async def generate_pdf_api(req: PDFRequest):
    try:
        pdf_bytes = convert_markdown_to_pdf(req.notes)
        return Response(content=bytes(pdf_bytes), media_type="application/pdf")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, timeout_keep_alive=600)