import atexit
import shutil
import os
import sys
import time
import json
import struct
import psutil
import pygetwindow as gw
import win32process
import keyboard
import shutil
import threading
from PIL import Image
import pystray
import subprocess

# --- App constants ---
CLIENT_ID = "1372120706443382834"
APP_NAME = "Notion RPC"
ICON_PATH = "notion.ico"

UPDATE_INTERVAL = 6
POLL_INTERVAL = 1
CLEAR_GRACE = 6
EXIT_TIMEOUT = 20
EDIT_HOLD_TIME = 8
IDLE_AFTER_UNFOCUS = 10

# ---------- Helpers for PyInstaller ----------
def resource_path(relative_path: str) -> str:
    """Return absolute path to resource, works for dev and PyInstaller .exe"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.getcwd(), relative_path)

def get_executable_path() -> str:
    """Return the real executable path (not the _MEI temp path)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])

def restart_app():
    """Safely relaunch app depending on run mode."""
    try:
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen([exe_path], close_fds=True, creationflags=DETACHED_PROCESS)
        else:
            subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0])])
        time.sleep(0.3)
        os._exit(0)
    except Exception as e:
        print(f"⚠️ Restart failed: {e}")

def clean_temp_folder():
    try:
        if hasattr(sys, "_MEIPASS"):
            temp_dir = sys._MEIPASS
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
                print(f"🧹 Cleaned up temp directory: {temp_dir}")
    except Exception as e:
        print(f"⚠️ Cleanup error: {e}")

atexit.register(clean_temp_folder)


# --- Auto add to Windows Startup ---
if getattr(sys, 'frozen', False):
    try:
        exe_name = os.path.basename(sys.executable)
        src_path = sys.executable
        startup_folder = os.path.join(
            os.getenv("APPDATA"), "Microsoft\\Windows\\Start Menu\\Programs\\Startup"
        )
        dst_path = os.path.join(startup_folder, exe_name)

        if not os.path.exists(dst_path):
            shutil.copy(src_path, dst_path)
            print(f"✅ Added {exe_name} to Windows Startup folder.")
        else:
            print("🟢 Already exists in startup folder.")
    except Exception as e:
        print(f"⚠️ Could not add to startup: {e}")

# --- Helper functions ---
def get_window_pid(hwnd):
    try:
        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None

def find_notion_window():
    for w in gw.getAllWindows():
        title = (w.title or "").strip()
        if not title:
            continue
        hwnd = getattr(w, "_hWnd", None)
        if not hwnd:
            continue
        pid = get_window_pid(hwnd)
        if not pid:
            continue
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() == "notion.exe":
                return w
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

def get_notion_title():
    w = find_notion_window()
    if not w or not w.title:
        return "Workspace"
    title = w.title.replace("– Notion", "").replace("Notion –", "").strip()
    return title or "Workspace"

def is_notion_running():
    for p in psutil.process_iter(["name"]):
        if p.info.get("name", "").lower() == "notion.exe":
            return True
    return False

def get_active_process_name():
    try:
        aw = gw.getActiveWindow()
        if not aw:
            return None
        hwnd = getattr(aw, "_hWnd", None)
        if not hwnd:
            return None
        pid = get_window_pid(hwnd)
        if not pid:
            return None
        return psutil.Process(pid).name().lower()
    except Exception:
        return None

def is_notion_focused():
    return get_active_process_name() == "notion.exe"

# --- Discord IPC ---
def open_discord_ipc():
    for i in range(10):
        path = fr"\\?\pipe\discord-ipc-{i}"
        try:
            return open(path, "r+b", buffering=0)
        except FileNotFoundError:
            continue
    raise FileNotFoundError("Discord not found — open Discord first.")

def send_ipc(pipe, opcode, payload):
    data = json.dumps(payload).encode("utf-8")
    header = struct.pack("<II", opcode, len(data))
    try:
        pipe.write(header + data)
        pipe.flush()
    except OSError:
        raise ConnectionError("Discord connection lost")

def clear_presence(pipe):
    try:
        send_ipc(
            pipe,
            1,
            {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": None}, "nonce": str(time.time())},
        )
    except Exception:
        pass

def send_presence(pipe, title, start_time, mode):
    icons = {
        "editing": ("writing_512", "Typing in Notion ✍️", "Plotting sth ✍️ let me cook 🔥"),
        "reading": ("reading_512", "Reading in Notion 📖", "Reading the same line for the 5th time 😵‍💫📖"),
        "idle": ("idle_512", "Idle ", "Trying to remember why I opened Notion 🧠"),
        "background": ("break_512", "Coffee break", "Taking a break from Notion ☕ AFK "),
    }
    small_img, small_text, state = icons.get(mode, icons["idle"])

    try:
        clear_presence(pipe)
        time.sleep(0.2)
        activity = {
            "name": "my brain buffer through life decisions (beta)",
            "type": 3,
            "state": state,
            "details": f"Page: {title}" if mode != "background" else "Notion Workspace",
            "timestamps": {"start": start_time},
            "assets": {
                "large_image": "notion_1024",
                "large_text": "Notion Workspace",
                "small_image": small_img,
                "small_text": small_text,
            },
        }
        send_ipc(
            pipe,
            1,
            {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity}, "nonce": f"{mode}-{time.time()}"},
        )
    except ConnectionError:
        print("⚠️ Lost Discord connection. Waiting for reconnect...")

# --- Typing Tracker ---
last_typing = 0
def on_key_event(event):
    global last_typing
    if get_active_process_name() == "notion.exe":
        last_typing = time.time()

keyboard.hook(on_key_event)

# --- Tray icon setup ---
def run_tray_icon(stop_event):
    def on_quit(icon, item):
        print("👋 Exiting via tray icon...")
        stop_event.set()
        icon.stop()

    def on_restart(icon, item):
        print("🔄 Restarting RPC...")
        try:
            icon.visible = False
            icon.stop()
        except Exception:
            pass
        restart_app()

    def open_notion(icon, item):
        os.system("start notion://")

    try:
        image = Image.open(resource_path(ICON_PATH))
    except Exception:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    menu = pystray.Menu(
        pystray.MenuItem("Open Notion", open_notion),
        pystray.MenuItem("Restart RPC", on_restart),
        pystray.MenuItem("Exit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
    icon.run()

# --- Main Logic ---
def run_rpc(stop_event):
    print("🚀 Starting Notion RPC...")
    start_time = int(time.time())
    last_focus, stable_mode, last_update = 0, "idle", 0
    pipe = None

    while not stop_event.is_set():
        try:
            if pipe is None:
                pipe = open_discord_ipc()
                send_ipc(pipe, 0, {"v": 1, "client_id": CLIENT_ID})
                print("✅ Connected to Discord IPC.\n")

            now = time.time()
            notion_running = is_notion_running()

            # 🆕 NEW: Handle Notion closed state
            if not notion_running:
                if stable_mode != "stopped":
                    print("🔴 Notion closed — clearing presence.")
                    clear_presence(pipe)
                    stable_mode = "stopped"
                time.sleep(POLL_INTERVAL)
                continue

            notion_win = find_notion_window()
            focused = is_notion_focused()

            if notion_running and not notion_win:
                mode = "background"
            elif focused and (now - last_typing) < EDIT_HOLD_TIME:
                mode = "editing"
            elif focused:
                mode = "reading"
            elif (now - last_focus) > IDLE_AFTER_UNFOCUS:
                mode = "idle"
            else:
                mode = stable_mode

            if focused:
                last_focus = now

            if mode != stable_mode or (now - last_update) >= UPDATE_INTERVAL:
                title = get_notion_title() if notion_win else "Notion Workspace"
                send_presence(pipe, title, start_time, mode)
                stable_mode, last_update = mode, now
                print(f"🟢 Mode: {mode:<10} | Focused: {focused} | Title: {title}")

        except (FileNotFoundError, ConnectionError):
            pipe = None
            time.sleep(5)
        except Exception as e:
            print(f"⚠️ Error: {e}")
        time.sleep(POLL_INTERVAL)

# --- Start threads ---
stop_event = threading.Event()
tray_thread = threading.Thread(target=run_tray_icon, args=(stop_event,), daemon=True)
rpc_thread = threading.Thread(target=run_rpc, args=(stop_event,), daemon=True)

tray_thread.start()
rpc_thread.start()

try:
    while not stop_event.is_set():
        time.sleep(1)
except KeyboardInterrupt:
    stop_event.set()
    print("\n👋 Exiting gracefully.")
