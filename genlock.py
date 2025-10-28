# genlock.py
import cv2
import mediapipe as mp
import numpy as np
import threading
import time
import platform
import subprocess
import ctypes
import queue
import speech_recognition as sr
import pyttsx3
import sys  # added for clean exit

# -------- configuration --------
LOCK_HOLD_SECONDS = 0.9        # how long gesture must be held (seconds)
GESTURE_FRAME_WINDOW = 10     # smoothing window (frames)
VOICE_KEY_PHRASES = ["lock laptop", "lock my laptop", "lock computer", "secure", "lock it"]  # phrases
CONFIRM_COUNTDOWN = 3         # seconds countdown before locking (set 0 to skip)
DEBOUNCE_SECONDS = 3          # minimum seconds between locks
# --------------------------------

mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils

engine = pyttsx3.init()  # text-to-speech for confirmation feedback

# thread-safe queue used to notify main thread to lock
action_q = queue.Queue()

last_lock_time = 0

def speak(text):
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print("TTS error:", e)

def lock_workstation():
    """Call platform-specific lock."""
    global last_lock_time
    now = time.time()
    if now - last_lock_time < DEBOUNCE_SECONDS:
        print("Lock suppressed (debounce).")
        return
    last_lock_time = now

    # optional confirmation countdown
    if CONFIRM_COUNTDOWN > 0:
        for i in range(CONFIRM_COUNTDOWN, 0, -1):
            speak(f"Locking in {i}")
            time.sleep(1)

    plat = platform.system().lower()
    try:
        if plat == "windows":
            # Windows
            ctypes.windll.user32.LockWorkStation()
        elif plat == "linux":
            # try loginctl or gnome-screensaver-command
            try:
                subprocess.run(["loginctl", "lock-session"], check=True)
            except Exception:
                try:
                    subprocess.run(["gnome-screensaver-command", "-l"], check=True)
                except Exception:
                    # fallback to xdg-screensaver (may not lock)
                    subprocess.run(["xdg-screensaver", "lock"])
        elif plat == "darwin":
            # macOS - use AppleScript to lock the screen
            subprocess.run(["/usr/bin/osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'])
        else:
            print("Unknown platform - cannot lock automatically.")
    except Exception as e:
        print("Lock failed:", e)

    # exit immediately after locking
    print("System locked. Exiting program...")
    speak("System locked. Exiting.")
    sys.exit(0)

# ---- Voice thread ----
def voice_listener(stop_event):
    r = sr.Recognizer()
    mic = None
    try:
        mic = sr.Microphone()
    except Exception as e:
        print("No microphone found or PyAudio not installed:", e)
        return

    with mic as source:
        r.adjust_for_ambient_noise(source, duration=1)
    print("Voice listener ready.")

    while not stop_event.is_set():
        with mic as source:
            try:
                print("Listening for command...")
                audio = r.listen(source, timeout=5, phrase_time_limit=5)
            except sr.WaitTimeoutError:
                continue
        try:
            # using google recognizer (online). For offline, use VOSK
            text = r.recognize_google(audio).lower()
            print("Heard (voice):", text)
            for phrase in VOICE_KEY_PHRASES:
                if phrase in text:
                    print("Voice trigger detected:", phrase)
                    action_q.put(("voice", text))
                    break
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print("Speech service error:", e)
        except Exception as e:
            print("Voice thread error:", e)

# ---- Gesture utilities ----
def landmarks_to_np(landmarks, w, h):
    pts = []
    for lm in landmarks.landmark:
        pts.append((int(lm.x * w), int(lm.y * h)))
    return pts

def is_finger_extended(pts, tip_idx, pip_idx, mcp_idx):
    # simple heuristic independent of orientation: compare distances
    tip = np.array(pts[tip_idx])
    pip = np.array(pts[pip_idx])
    mcp = np.array(pts[mcp_idx])
    # if distance from tip to wrist (mcp) is > distance pip->mcp, it's extended
    return np.linalg.norm(tip - mcp) > np.linalg.norm(pip - mcp) * 1.05

def is_closed_fist(pts):
    # indices for landmarks (MediaPipe):
    # thumb: tip 4, ip 3, mcp 2
    # index: tip 8, pip 6, mcp 5
    # middle: tip 12, pip 10, mcp 9
    # ring: tip 16, pip 14, mcp 13
    # pinky: tip 20, pip 18, mcp 17
    checks = []
    fingers = [(8,6,5),(12,10,9),(16,14,13),(20,18,17)]
    for tip,pip,mcp in fingers:
        checks.append(is_finger_extended(pts, tip, pip, mcp))
    # thumb check using different indices
    thumb_extended = is_finger_extended(pts, 4, 3, 2)
    extended_count = sum(checks) + (1 if thumb_extended else 0)
    # if very few fingers extended => fist
    return extended_count <= 1  # adjust threshold if needed

# ---- Main camera loop ----
def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open camera")
        return

    stop_event = threading.Event()
    voice_thread = threading.Thread(target=voice_listener, args=(stop_event,), daemon=True)
    voice_thread.start()

    mp_hand = mp_hands.Hands(static_image_mode=False,
                             max_num_hands=1,
                             min_detection_confidence=0.5,
                             min_tracking_confidence=0.5)

    frame_history = []
    gesture_start_time = None
    last_gesture_state = False

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            h, w = frame.shape[:2]
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = mp_hand.process(frame_rgb)

            gesture_detected = False
            if results.multi_hand_landmarks:
                # use first detected hand
                hand_landmarks = results.multi_hand_landmarks[0]
                pts = landmarks_to_np(hand_landmarks, w, h)
                # draw
                mp_drawing.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                try:
                    if is_closed_fist(pts):
                        gesture_detected = True
                    else:
                        gesture_detected = False
                except Exception as e:
                    # if something indexing fails
                    gesture_detected = False

            # smoothing: use last N frames
            frame_history.append(gesture_detected)
            if len(frame_history) > GESTURE_FRAME_WINDOW:
                frame_history.pop(0)
            # majority vote
            votes = sum(frame_history)
            stable = votes >= (GESTURE_FRAME_WINDOW * 0.7)  # 70% frames positive

            # manage hold time
            if stable and not last_gesture_state:
                gesture_start_time = time.time()
                last_gesture_state = True
            elif stable and last_gesture_state:
                # check hold duration
                if time.time() - gesture_start_time >= LOCK_HOLD_SECONDS:
                    # trigger lock
                    print("Gesture trigger detected (fist).")
                    action_q.put(("gesture", "fist"))
                    last_gesture_state = False
                    frame_history.clear()
            elif not stable:
                last_gesture_state = False
                gesture_start_time = None

            # process any actions from action_q
            try:
                src, payload = action_q.get_nowait()
                print("Action from:", src, payload)
                # optional audible confirmation
                speak("Lock command received")
                lock_workstation()  # exits after locking
            except queue.Empty:
                pass

            # display status
            status_text = f"Gesture={gesture_detected}  Stable={stable}"
            cv2.putText(frame, status_text, (10,30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
            cv2.imshow("GenLock - press q to quit", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break

    finally:
        stop_event.set()
        cap.release()
        cv2.destroyAllWindows()
        print("Exiting...")

if __name__ == "__main__":
    main()
