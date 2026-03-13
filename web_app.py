import asyncio
import json
import os
import queue
import sys
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import chatgpt_register

app = FastAPI(title="ChatGPT Register Web UI")

# Setup templates
os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

class ConfigModel(BaseModel):
    total_accounts: int
    duckmail_api_base: str
    duckmail_bearer: str
    proxy: str
    output_file: str
    enable_oauth: bool
    oauth_required: bool
    oauth_issuer: str
    oauth_client_id: str
    oauth_redirect_uri: str
    ak_file: str
    rk_file: str
    token_json_dir: str
    upload_api_url: str
    upload_api_token: str

class StartTaskModel(BaseModel):
    total_accounts: int
    max_workers: int
    proxy: str

# -------------------------------------------------------------------
# stdout interceptor for SSE
# -------------------------------------------------------------------
class BroadcastStdout:
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
        self.listeners = []
        self.lock = threading.Lock()
        
    def write(self, text):
        self.original_stdout.write(text)
        with self.lock:
            for q in self.listeners:
                try:
                    q.put_nowait(text)
                except queue.Full:
                    pass
                
    def flush(self):
        self.original_stdout.flush()

    def isatty(self):
        return False

# hook stdout
if not isinstance(sys.stdout, BroadcastStdout):
    broadcaster = BroadcastStdout(sys.stdout)
    sys.stdout = broadcaster
else:
    broadcaster = sys.stdout

# Task state
current_task = {
    "is_running": False
}

def reload_cli_config():
    config = chatgpt_register._load_config()
    chatgpt_register.DUCKMAIL_API_BASE = config["duckmail_api_base"]
    chatgpt_register.DUCKMAIL_BEARER = config["duckmail_bearer"]
    chatgpt_register.DEFAULT_TOTAL_ACCOUNTS = config["total_accounts"]
    chatgpt_register.DEFAULT_PROXY = config["proxy"]
    chatgpt_register.DEFAULT_OUTPUT_FILE = config["output_file"]
    chatgpt_register.ENABLE_OAUTH = chatgpt_register._as_bool(config.get("enable_oauth", True))
    chatgpt_register.OAUTH_REQUIRED = chatgpt_register._as_bool(config.get("oauth_required", True))
    chatgpt_register.OAUTH_ISSUER = config["oauth_issuer"].rstrip("/")
    chatgpt_register.OAUTH_CLIENT_ID = config["oauth_client_id"]
    chatgpt_register.OAUTH_REDIRECT_URI = config["oauth_redirect_uri"]
    chatgpt_register.AK_FILE = config["ak_file"]
    chatgpt_register.RK_FILE = config["rk_file"]
    chatgpt_register.TOKEN_JSON_DIR = config["token_json_dir"]
    chatgpt_register.UPLOAD_API_URL = config["upload_api_url"]
    chatgpt_register.UPLOAD_API_TOKEN = config["upload_api_token"]

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/config")
async def get_config():
    return chatgpt_register._load_config()

@app.post("/api/config")
async def save_config(config: ConfigModel):
    config_path = os.path.join(os.path.dirname(os.path.abspath(chatgpt_register.__file__)), "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config.dict(), f, indent=4, ensure_ascii=False)
    reload_cli_config()
    return {"status": "success"}

@app.get("/api/stats")
async def get_stats():
    config = chatgpt_register._load_config()
    out_file = config["output_file"]
    ak_file = config["ak_file"]
    token_dir = config["token_json_dir"]
    
    accounts_count = 0
    if os.path.exists(out_file):
        with open(out_file, "r", encoding="utf-8") as f:
            accounts_count = len([line for line in f if line.strip()])
            
    ak_count = 0
    if os.path.exists(ak_file):
        with open(ak_file, "r", encoding="utf-8") as f:
            ak_count = len([line for line in f if line.strip()])
            
    token_files = 0
    if os.path.exists(token_dir):
        token_files = len([f for f in os.listdir(token_dir) if f.endswith(".json")])
        
    return {
        "accounts_count": accounts_count,
        "ak_count": ak_count,
        "token_files": token_files,
        "is_running": current_task["is_running"]
    }

@app.get("/api/accounts")
async def get_accounts():
    config = chatgpt_register._load_config()
    out_file = config["output_file"]
    content = ""
    if os.path.exists(out_file):
        with open(out_file, "r", encoding="utf-8") as f:
            content = f.read()
    return {"content": content}

def _run_task_thread(total_accounts, max_workers, proxy):
    try:
        current_task["is_running"] = True
        config = chatgpt_register._load_config()
        output_file = config["output_file"]
        
        reload_cli_config()
        chatgpt_register._setup_file_logger()
        
        chatgpt_register.run_batch(
            total_accounts=total_accounts,
            output_file=output_file,
            max_workers=max_workers,
            proxy=proxy if proxy else None
        )
    except Exception as e:
        print(f"\n[Error] Task Thread exception: {e}")
    finally:
        current_task["is_running"] = False
        print("\n[Info] Task Thread finished.")

@app.post("/api/start")
async def start_task(req: StartTaskModel):
    if current_task["is_running"]:
        return {"status": "error", "message": "Task is already running"}
    
    t = threading.Thread(target=_run_task_thread, args=(req.total_accounts, req.max_workers, req.proxy))
    t.daemon = True
    t.start()
    return {"status": "success", "message": "Task started"}

@app.get("/api/logs")
async def stream_logs():
    async def event_generator():
        q = queue.Queue(maxsize=1000)
        with broadcaster.lock:
            broadcaster.listeners.append(q)
        try:
            while True:
                try:
                    # Non-blocking get inside async loop using run_in_executor
                    msg = await asyncio.get_event_loop().run_in_executor(None, q.get, True, 1.0)
                    if msg:
                        lines = msg.split('\n')
                        for i, line in enumerate(lines):
                            if i < len(lines) - 1 or line:
                                content = line + ('' if i == len(lines)-1 and not msg.endswith('\n') else '\n')
                                yield f"data: {json.dumps(content)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with broadcaster.lock:
                if q in broadcaster.listeners:
                    broadcaster.listeners.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run("web_app:app", host="0.0.0.0", port=8000, reload=False)
