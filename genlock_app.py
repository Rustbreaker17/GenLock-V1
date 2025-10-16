# genlock_app.py  (replace your existing file with this)
import tkinter as tk
import threading
import subprocess
import sys
import os
import time

try:
    import psutil
except Exception:
    psutil = None

APP_SCRIPT = "genlock_core.py"  # name of the core script (or its exe when frozen)

def find_genlock_process():
    """Return a psutil.Process for an existing genlock_core process, or None."""
    if psutil is None:
        return None
    for proc in psutil.process_iter(attrs=['pid', 'name', 'cmdline']):
        try:
            name = (proc.info.get('name') or "").lower()
            cmdline = proc.info.get('cmdline') or []
            # check for reference to genlock_core in name or command line
            if any('genlock_core' in str(part).lower() for part in cmdline):
                return proc
            if 'genlock_core' in name:
                return proc
            # if frozen, the exe might be named genlock_app or genlock_core
            if 'genlock_app' in name or 'genlock' in name:
                # make sure it's not this launcher process itself
                if proc.pid != os.getpid():
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

def update_status_label():
    proc = find_genlock_process()
    if proc:
        status_label.config(text=f"Running (pid {proc.pid})", fg="#00b894")
        start_btn.config(text="Stop GenLock", bg="#e74c3c")
    else:
        status_label.config(text="Not running", fg="white")
        start_btn.config(text="Start GenLock", bg="#00b894")
    # schedule next update
    root.after(1000, update_status_label)

def start_genlock_background():
    """Start genlock_core in background using subprocess.Popen."""
    python = sys.executable
    script_path = os.path.join(os.path.dirname(__file__), APP_SCRIPT)
    # If running from a frozen exe, try to use genlock_core.exe if present
    if not os.path.exists(script_path):
        # look for genlock_core.exe in same folder
        exe_path = os.path.join(os.path.dirname(__file__), "genlock_core.exe")
        if os.path.exists(exe_path):
            script_path = exe_path
        else:
            # maybe the core is installed elsewhere — still try APP_SCRIPT anyway
            script_path = os.path.join(os.path.dirname(__file__), APP_SCRIPT)

    try:
        # use Popen so launcher doesn't block; redirect output to devnull
        subprocess.Popen([python, script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)  # give process a moment to start
    except Exception as e:
        status_label.config(text=f"Start failed: {e}", fg="#ffcc00")

def stop_genlock():
    """Terminate the existing genlock process if found."""
    if psutil is None:
        status_label.config(text="psutil not installed (can't stop)", fg="#ffcc00")
        return
    proc = find_genlock_process()
    if not proc:
        status_label.config(text="Not running", fg="white")
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
        status_label.config(text="Stopped", fg="#ff3b30")
    except Exception as e:
        status_label.config(text=f"Stop failed: {e}", fg="#ffcc00")

def start_stop_handler():
    """Called when the Start/Stop button is clicked."""
    proc = find_genlock_process()
    if proc:
        # it's running -> stop it
        threading.Thread(target=stop_genlock, daemon=True).start()
    else:
        # not running -> start it
        threading.Thread(target=start_genlock_background, daemon=True).start()

# ---------- UI ----------
root = tk.Tk()
root.title("GenLock App")
root.geometry("380x230")
root.configure(bg="#1e1e1e")

label = tk.Label(root, text="🔒 GenLock System", font=("Segoe UI", 16, "bold"), bg="#1e1e1e", fg="white")
label.pack(pady=18)

start_btn = tk.Button(root, text="Start GenLock", command=start_stop_handler, bg="#00b894", fg="white", font=("Segoe UI", 12), width=20)
start_btn.pack(pady=8)

status_label = tk.Label(root, text="Checking...", font=("Segoe UI", 10), bg="#1e1e1e", fg="white")
status_label.pack(pady=10)

note = tk.Label(root, text="Start will run the core GenLock process in background.\nUse Stop to safely terminate it.", font=("Segoe UI", 9), bg="#1e1e1e", fg="#bdbdbd")
note.pack(pady=6)

# initial check + periodic updates
root.after(500, update_status_label)
root.mainloop()
# --- begin single-instance + control socket (put this at top of genlock_core.py) ---
import socket
import threading
import sys
import time

# Configuration for single-instance and control
_SINGLETON_HOST = "127.0.0.1"
_SINGLETON_PORT = 54321  # port used to ensure single instance; change if conflict
_CONTROL_PORT = 54322    # optional control port for a graceful stop

# Try to claim the singleton port. If bind fails => another instance is running.
_singleton_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_singleton_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    _singleton_sock.bind((_SINGLETON_HOST, _SINGLETON_PORT))
    _singleton_sock.listen(1)
except OSError:
    # Another instance is already running — exit cleanly.
    print("Another GenLock instance is already running. Exiting.")
    sys.exit(0)

# Optional: start a control thread to accept simple commands (like "stop")
def _control_thread():
    ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctrl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        ctrl.bind((_SINGLETON_HOST, _CONTROL_PORT))
        ctrl.listen(1)
    except OSError:
        # If control port not available, just skip control thread.
        return
    while True:
        try:
            conn, _ = ctrl.accept()
            data = conn.recv(1024).decode(errors='ignore').strip().lower()
            if data == "stop":
                conn.send(b"stopping")
                conn.close()
                # graceful shutdown: raise SystemExit or call a shutdown flag
                print("Stop command received, exiting.")
                try:
                    # attempt graceful exit
                    sys.exit(0)
                except SystemExit:
                    # if sys.exit suppressed, force exit
                    os._exit(0)
            else:
                conn.send(b"unknown")
                conn.close()
        except Exception:
            # continue listening
            pass

# start control thread (daemon so it won't block exit)
import threading, os
t = threading.Thread(target=_control_thread, daemon=True)
t.start()
# --- end single-instance + control socket ---
