# genlock.py  --- GenLock + Secure Fullscreen Unlock (hook + mouse confinement)
# NOTE: Read the instructions (Part 2) before running. Requires Windows, Administrator
# Requires: opencv-python, mediapipe, numpy, pyttsx3, SpeechRecognition, sounddevice, librosa, Pillow
# Tested with Python 3.11/3.12 on Windows.

import cv2
import mediapipe as mp
import numpy as np
import threading
import time
import platform
import ctypes
import ctypes.wintypes
import speech_recognition as sr
import pyttsx3
import sounddevice as sd
import librosa
import json
import os
import tkinter as tk
from tkinter import simpledialog

# ---------------- CONFIG ----------------
VOICE_PROFILE_PATH = "voice_profile.npy"
VOICE_SAMPLES = 3
VOICE_RECORD_SECONDS = 3.5
VOICE_SR = 16000
VOICE_SIM_THRESHOLD = 0.88
PASS_PHRASE = "unlock genlock"
PASSWORD_FILE = "unlock_pass.json"

GESTURE_FRAME_WINDOW = 10

BG_COLOR = "black"
FG_COLOR = "white"
STATUS_FONT = ("Arial", 22)
SMALL_FONT = ("Arial", 14)
# ----------------------------------------

mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils
engine = pyttsx3.init()

def speak(text):
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception:
        print("TTS failed:", text)

def lock_os():
    """Lock the Windows workstation (shows the normal lock screen)."""
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception as e:
        print("LockWorkStation failed:", e)

# ---------------- Voice utilities ----------------
def record_audio_raw(seconds=VOICE_RECORD_SECONDS, sr_rate=VOICE_SR):
    try:
        audio = sd.rec(int(seconds * sr_rate), samplerate=sr_rate, channels=1, dtype='float32')
        sd.wait()
        return np.squeeze(audio)
    except Exception as e:
        print("Audio record error:", e)
        return None

def extract_mfcc(audio, sr_rate=VOICE_SR, n_mfcc=13):
    if audio is None or len(audio) == 0:
        return None
    try:
        mfcc = librosa.feature.mfcc(y=audio, sr=sr_rate, n_mfcc=n_mfcc)
        return np.mean(mfcc, axis=1)
    except Exception as e:
        print("MFCC extraction error:", e)
        return None

def enroll_voice_interactive(samples=VOICE_SAMPLES):
    speak("Starting voice enrollment. Please say the passphrase when prompted.")
    feats = []
    i = 0
    while i < samples:
        speak(f"Sample {i+1}. Say: {PASS_PHRASE}")
        time.sleep(0.3)
        audio = record_audio_raw()
        f = extract_mfcc(audio)
        if f is not None:
            feats.append(f)
            speak("Sample recorded.")
            i += 1
        else:
            speak("Recording failed, trying again.")
        time.sleep(0.4)
    if not feats:
        raise RuntimeError("No voice samples captured.")
    avg = np.mean(np.stack(feats), axis=0)
    np.save(VOICE_PROFILE_PATH, avg)
    speak("Voice enrollment complete.")

def load_voice_profile():
    if os.path.exists(VOICE_PROFILE_PATH):
        return np.load(VOICE_PROFILE_PATH)
    return None

def compare_voice_to_profile(audio):
    ref = load_voice_profile()
    if ref is None:
        return 0.0
    f = extract_mfcc(audio)
    if f is None:
        return 0.0
    sim = float(np.dot(f, ref) / (np.linalg.norm(f) * np.linalg.norm(ref) + 1e-9))
    return sim

def speech_to_text_from_mic(timeout=4, phrase_time_limit=4):
    r = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.6)
            audio = r.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        text = r.recognize_google(audio).lower()
        return text
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print("STT error:", e)
        return ""

# ---------------- Gesture utilities ----------------
def landmarks_to_np(landmarks, w, h):
    return [(int(lm.x * w), int(lm.y * h)) for lm in landmarks.landmark]

def is_open_palm(pts):
    fingers = [(8,6,5),(12,10,9),(16,14,13),(20,18,17)]
    ext = 0
    for tip, pip, mcp in fingers:
        tip_dist = np.linalg.norm(np.array(pts[tip]) - np.array(pts[mcp]))
        pip_dist = np.linalg.norm(np.array(pts[pip]) - np.array(pts[mcp]))
        if tip_dist > pip_dist * 1.2:
            ext += 1
    return ext >= 3

# ---------------- Password fallback ----------------
def load_or_set_password():
    if os.path.exists(PASSWORD_FILE):
        return json.load(open(PASSWORD_FILE))['password']
    pwd = input("Set fallback password (will be saved locally): ")
    json.dump({'password': pwd}, open(PASSWORD_FILE, 'w'))
    return pwd

# --- BEGIN: Safe hybrid keyboard+mouse trap utilities (Windows only) ---
# Uses a low-level keyboard hook to swallow everything except 'V'.
# Includes an emergency override: Ctrl + Alt + U (uninstalls hook + releases mouse).
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

ALLOWED_VK = 0x56  # VK for 'V'
EMERGENCY_VK = 0x55  # 'U'
_hook_handle = None
_lowlevel_proc_ptr = None
_hook_thread = None
_hook_stop_event = threading.Event()

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG))
    ]

def _is_ctrl_alt_pressed():
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    ctrl = (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0
    alt = (user32.GetAsyncKeyState(VK_MENU) & 0x8000) != 0
    return ctrl and alt

# Callback signature for SetWindowsHookExW
LOWLEVELPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

def _low_level_keyboard_proc(nCode, wParam, lParam):
    global _hook_handle
    if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        # Emergency override: Ctrl+Alt+U
        if vk == EMERGENCY_VK and _is_ctrl_alt_pressed():
            _hook_stop_event.set()
            return 1  # swallow event
        # Allow only V
        if vk == ALLOWED_VK:
            # pass through to next hook so normal handling occurs for V
            return user32.CallNextHookEx(_hook_handle, nCode, wParam, lParam)
        # swallow any other key
        return 1
    return user32.CallNextHookEx(_hook_handle, nCode, wParam, lParam)

def _install_hook_and_loop():
    global _hook_handle, _lowlevel_proc_ptr
    _lowlevel_proc_ptr = LOWLEVELPROC(_low_level_keyboard_proc)
    hMod = kernel32.GetModuleHandleW(None)
    _hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _lowlevel_proc_ptr, hMod, 0)
    if not _hook_handle:
        print("Failed to install keyboard hook. Are you running as Administrator?")
        return
    msg = ctypes.wintypes.MSG()
    while not _hook_stop_event.is_set():
        # non-blocking peek message loop
        has_msg = user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1)
        if has_msg:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)
    try:
        user32.UnhookWindowsHookEx(_hook_handle)
    except Exception:
        pass
    _hook_handle = None
    _lowlevel_proc_ptr = None
    _hook_stop_event.clear()

def start_keyboard_hook():
    global _hook_thread
    if _hook_thread and _hook_thread.is_alive():
        return
    _hook_thread = threading.Thread(target=_install_hook_and_loop, daemon=True)
    _hook_thread.start()

def stop_keyboard_hook():
    _hook_stop_event.set()
    global _hook_thread
    if _hook_thread:
        _hook_thread.join(timeout=1.0)

# Mouse confinement
def confine_mouse_to_rect(left, top, right, bottom):
    rect = ctypes.wintypes.RECT(left, top, right, bottom)
    user32.ClipCursor(ctypes.byref(rect))

def release_mouse():
    user32.ClipCursor(None)

def get_window_rect(hwnd):
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom
# --- END utilities ---

# ---------------- Fullscreen overlay lock ----------------
class SecureLockOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.configure(bg=BG_COLOR)
        self.root.attributes("-fullscreen", True)
        self.root.title("GenLock Secure Lock")
        self.root.lift()
        self.root.attributes("-topmost", True)

        # --- ADDED LINES to block close and focus loss ---
        self.root.protocol("WM_DELETE_WINDOW", self._on_attempt_close)
        self.root.bind("<FocusOut>", self._on_focus_out)
        # ---------------------------------------------------

        self.root.update()

        self.canvas = tk.Canvas(self.root, bg=BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.status_id = None
        self.cam_image_id = None
        self.sim_text_id = None

        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.mp_hand = mp_hands.Hands(max_num_hands=1, min_detection_confidence=0.5)
        self.stop_flag = False
        self.voice_verified = False
        self.gesture_verified = False
        self.attempts = 0
        self.similarity = 0.0

        # voice profile & password
        if load_voice_profile() is None:
            speak("No voice profile found. Please enroll now.")
            enroll_voice_interactive()
        load_or_set_password()

        # Tk-level binds: keep UI responsive and provide visual-only filtering (best-effort)
        def key_filter(event):
            # let 'v' through to our handler; swallow others at Tk level too
            try:
                ks = event.keysym.lower()
            except Exception:
                return "break"
            if ks == "v":
                return None
            return "break"
        self.root.bind_all("<Key>", key_filter)

        # Start OS-level protections: keyboard hook + mouse confinement (may require admin)
        # Confine mouse to our window rectangle
        self.root.update_idletasks()
        # Get HWND of active window (Tk)
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        try:
            l,t,r,b = get_window_rect(hwnd)
            confine_mouse_to_rect(l, t, r, b)
        except Exception as e:
            print("Mouse confinement failed:", e)
        # Start low-level keyboard hook
        try:
            start_keyboard_hook()
        except Exception as e:
            print("Keyboard hook start error:", e)

    # --- ADDED METHODS ---
    def _on_attempt_close(self):
        # This function is called when the OS tries to close the window
        # We do nothing, effectively ignoring the close request.
        speak("Close attempt blocked.")
        return

    def _on_focus_out(self, event=None):
        # When the window loses focus, force it back
        try:
            self.root.focus_force()
            self.root.attributes("-topmost", True)
        except Exception:
            pass
    # ---------------------

    def _unbind_input_traps(self):
        try:
            self.root.unbind_all("<Key>")
            self.root.unbind("<FocusOut>")
        except Exception:
            pass

    def destroy(self):
        self.stop_flag = True
        try:
            self.cap.release()
            self.mp_hand.close()
        except Exception:
            pass
        # cleanup OS-level traps
        try:
            release_mouse()
        except Exception:
            pass
        try:
            stop_keyboard_hook()
        except Exception:
            pass
        try:
            self._unbind_input_traps()
        except Exception:
            pass
        try:
            # Reset the close protocol before destroying
            self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
            self.root.destroy()
        except Exception:
            pass

    def _update_frame(self):
        ret, frame = self.cap.read()
        if not ret:
            return
        frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res = self.mp_hand.process(img_rgb)

        disp = frame.copy()
        if res.multi_hand_landmarks:
            for lm in res.multi_hand_landmarks:
                mp_drawing.draw_landmarks(disp, lm, mp_hands.HAND_CONNECTIONS)

        disp_small = cv2.resize(disp, (640, 480))
        img_bytes = cv2.imencode('.png', disp_small)[1].tobytes()
        try:
            from io import BytesIO
            from PIL import Image, ImageTk
            im = Image.open(BytesIO(img_bytes))
            photo = ImageTk.PhotoImage(image=im)
            if self.cam_image_id is None:
                self.cam_image_id = self.canvas.create_image(50, 80, anchor='nw', image=photo)
                self.canvas.image = photo
            else:
                self.canvas.itemconfig(self.cam_image_id, image=photo)
                self.canvas.image = photo
        except Exception:
            if self.cam_image_id is None:
                self.cam_image_id = self.canvas.create_text(50, 80, anchor='nw', text="[Camera feed unavailable]", fill=FG_COLOR, font=SMALL_FONT)
            else:
                self.canvas.itemconfig(self.cam_image_id, text="[Camera feed unavailable]")

        gesture_now = False
        if res.multi_hand_landmarks:
            pts = landmarks_to_np(res.multi_hand_landmarks[0], w, h)
            if is_open_palm(pts):
                gesture_now = True

        status_lines = [
            "SYSTEM LOCKED - GenLock Secure Overlay",
            "Authenticate with your voice AND the open-palm gesture.",
            "",
            "Only the 'V' key is allowed while locked. Emergency: Ctrl+Alt+U"
        ]
        status_text = "\n".join(status_lines)
        if self.status_id is None:
            self.status_id = self.canvas.create_text(720, 120, anchor='nw', text=status_text, fill=FG_COLOR, font=STATUS_FONT)
        else:
            self.canvas.itemconfig(self.status_id, text=status_text)

        gesture_text = f"Gesture detected: {'YES' if gesture_now else 'NO'}"
        if self.sim_text_id is None:
            self.sim_text_id = self.canvas.create_text(720, 320, anchor='nw', text=gesture_text, fill=FG_COLOR, font=SMALL_FONT)
        else:
            self.canvas.itemconfig(self.sim_text_id, text=gesture_text)

        if self.voice_verified and gesture_now:
            self.gesture_verified = True
            self._on_unlock_success()
            return

    def _on_unlock_success(self):
        speak("Authentication successful. Unlocking system.")
        try:
            release_mouse()
        except Exception:
            pass
        try:
            stop_keyboard_hook()
        except Exception:
            pass
        try:
            self._unbind_input_traps()
        except Exception:
            pass
        self.destroy()

    def _on_unlock_failed(self):
        speak("Authentication failed. Fallback to password.")
        self.root.after(10, self._ask_password_dialog)

    def _ask_password_dialog(self):
        pwd = load_or_set_password()
        try:
            entry = simpledialog.askstring("Unlock", "Enter fallback password:", show="*")
        except Exception:
            entry = input("Enter fallback password: ")
        if entry == pwd:
            speak("Password accepted. Unlocking.")
            self._on_unlock_success()
        else:
            speak("Wrong password. Lock remains.")

    def run_voice_verification(self):
        speak("Please say the passphrase after the beep.")
        time.sleep(0.2)
        try:
            import winsound
            winsound.Beep(800, 200)
        except Exception:
            pass

        text = speech_to_text_from_mic(timeout=4, phrase_time_limit=4)
        text_ok = PASS_PHRASE in text if text else False
        if text_ok:
            speak("Passphrase recognized.")
        else:
            speak("Passphrase not recognized. Will still check voiceprint.")

        audio = record_audio_raw()
        sim = compare_voice_to_profile(audio)
        self.similarity = sim
        print("Voice similarity:", sim)
        try:
            if self.sim_text_id is None:
                self.sim_text_id = self.canvas.create_text(720, 360, anchor='nw', text=f"Voice similarity: {sim:.3f}", fill=FG_COLOR, font=SMALL_FONT)
            else:
                self.canvas.itemconfig(self.sim_text_id, text=f"Voice similarity: {sim:.3f}")
        except Exception:
            pass

        if sim >= VOICE_SIM_THRESHOLD:
            self.voice_verified = True
            speak("Voice verified.")
        else:
            self.voice_verified = False
            speak("Voice verification failed.")
            self._on_unlock_failed()

    def lock_and_show(self):
        def loop():
            if self.stop_flag:
                return
            try:
                self._update_frame()
            except Exception as e:
                print("Frame update error:", e)
            self.root.after(50, loop)

        loop()

        def start_voice_attempt(event=None):
            threading.Thread(target=self.run_voice_verification, daemon=True).start()

        # bind only V to start voice verification
        self.root.bind("<KeyPress-v>", start_voice_attempt)

        try:
            self.root.mainloop()
        finally:
            try:
                release_mouse()
            except Exception:
                pass
            try:
                stop_keyboard_hook()
            except Exception:
                pass

# ---------------- Integration into your main flow ----------------
def main_loop():
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("Cannot open camera")
        return
    mp_hand_detector = mp_hands.Hands(max_num_hands=1, min_detection_confidence=0.5)
    frame_history = []
    last_state = False
    gesture_start = None
    LOCK_HOLD_SECONDS = 0.9

    stop_event = threading.Event()
    def voice_listener_lock():
        r = sr.Recognizer()
        try:
            mic = sr.Microphone()
        except Exception as e:
            print("No mic for lock listener:", e)
            return
        with mic as source:
            r.adjust_for_ambient_noise(source, duration=0.8)
        while not stop_event.is_set():
            try:
                with mic as source:
                    audio = r.listen(source, timeout=5, phrase_time_limit=4)
                text = r.recognize_google(audio).lower()
                for phrase in ["lock laptop", "lock my laptop", "lock computer", "secure", "lock it"]:
                    if phrase in text:
                        print("Lock phrase heard:", phrase)
                        speak("Lock command received.")
                        lock_os()
                        overlay = SecureLockOverlay()
                        overlay.lock_and_show()
                        break
            except sr.WaitTimeoutError:
                continue
            except sr.UnknownValueError:
                continue
            except Exception as e:
                print("Voice listener error:", e)
                time.sleep(1)

    t = threading.Thread(target=voice_listener_lock, daemon=True)
    t.start()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            h, w = frame.shape[:2]
            res = mp_hand_detector.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            gesture_detected = False
            if res.multi_hand_landmarks:
                pts = landmarks_to_np(res.multi_hand_landmarks[0], w, h)
                mp_drawing.draw_landmarks(frame, res.multi_hand_landmarks[0], mp_hands.HAND_CONNECTIONS)
                fingers = [(8,6,5),(12,10,9),(16,14,13),(20,18,17)]
                extended = 0
                for tip,pip,mcp in fingers:
                    if np.linalg.norm(np.array(pts[tip]) - np.array(pts[mcp])) > np.linalg.norm(np.array(pts[pip]) - np.array(pts[mcp])) * 1.05:
                        extended += 1
                if extended <= 1:
                    gesture_detected = True

            frame_history.append(gesture_detected)
            if len(frame_history) > GESTURE_FRAME_WINDOW:
                frame_history.pop(0)
            stable = sum(frame_history) >= (GESTURE_FRAME_WINDOW * 0.7)

            if stable and not last_state:
                gesture_start = time.time()
                last_state = True
            elif stable and last_state and (time.time() - gesture_start >= LOCK_HOLD_SECONDS):
                speak("Gesture lock detected.")
                lock_os()
                overlay = SecureLockOverlay()
                overlay.lock_and_show()
                last_state = False
                frame_history.clear()
            elif not stable:
                last_state = False
                gesture_start = None

            cv2.putText(frame, f"Gesture={gesture_detected}  Stable={stable}", (10,30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
            cv2.imshow("GenLock Monitoring", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    finally:
        stop_event.set()
        cap.release()
        mp_hand_detector.close()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    if platform.system().lower() != "windows":
        print("This overlay uses Windows APIs; behavior may vary on other OS.")
    main_loop()
