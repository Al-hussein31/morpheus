"""What Morpheus can do on the Mac besides opening apps: keys, typing, clicking,
screenshots, screen recording, compressing, downloading, websites, WhatsApp and
saved commands.

agent.py decides (with Clef) which of these to run; these functions only do it.
Each returns a short report line for the notification.
"""
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString, NSURL

import ax
from settings import CONFIG, personal

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.expanduser("~/Downloads/Morpheus")
RECORDINGS = os.path.expanduser("~/Movies/Morpheus")
COMMANDS_FILE = personal("commands.json")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
SHORT_MAX_S = 180              # YouTube Shorts run up to 3 minutes


def notify(text, title="Morpheus"):
    safe = text.replace('"', "'")[:200]
    subprocess.Popen(["osascript", "-e", f'display notification "{safe}" with title "{title}"'])


def stamp():
    return time.strftime("%Y-%m-%d at %H.%M.%S")


# ---- keys and typing -------------------------------------------------------

KEYCODES = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
            "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
            "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
            "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "return": 36, "tab": 48, "space": 49,
            "delete": 51, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
            "pageup": 116, "pagedown": 121, "home": 115, "end": 119}
MODS = {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
        "alt": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl}

# Spoken name -> keys. Clef picks the name; nothing outside this list is ever pressed.
SHORTCUTS = {
    "copy": "cmd c", "paste": "cmd v", "cut": "cmd x", "select all": "cmd a", "undo": "cmd z",
    "redo": "cmd shift z", "save": "cmd s", "find": "cmd f", "refresh / reload": "cmd r",
    "close tab": "cmd w", "new window": "cmd n", "new tab": "cmd t", "reopen closed tab": "cmd shift t",
    "go back": "cmd [", "go forward": "cmd ]", "enter / return": "return", "escape / cancel": "escape",
    "tab": "tab", "space / play pause": "space", "delete / backspace": "delete",
    "full screen": "cmd ctrl f", "minimize window": "cmd m", "zoom in": "cmd =", "zoom out": "cmd -",
    "scroll down": "pagedown", "scroll up": "pageup", "top of page": "cmd up", "bottom of page": "cmd down",
    "next tab": "cmd shift ]", "previous tab": "cmd shift [", "quit app": "cmd q",
}


def keys(combo):
    """Press a key combo such as "cmd shift t" in the front app."""
    *mods, key = combo.split()
    flags = 0
    for m in mods:
        flags |= MODS[m]
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, KEYCODES[key], down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.02)


def type_text(text):
    """Type any text, whatever the keyboard layout: one Unicode key event per chunk."""
    for i in range(0, len(text), 20):
        chunk = text[i:i + 20]
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def clipboard_text():
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or ""


def set_clipboard_text(text):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def set_clipboard_files(paths):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])


def activate(app):
    subprocess.run(["open", "-a", app], timeout=15)
    for _ in range(25):
        if ax.front_name() == app:
            return True
        time.sleep(0.2)
    return False


# ---- files -----------------------------------------------------------------

def finder_selection():
    r = subprocess.run(["osascript", "-e", 'tell application "Finder" to set s to selection as alias list',
                        "-e", 'set out to ""', "-e", 'repeat with f in s', "-e",
                        'set out to out & POSIX path of f & linefeed', "-e", 'end repeat', "-e", 'out'],
                       capture_output=True, text=True, timeout=15)
    return [p.rstrip("/") for p in r.stdout.splitlines() if p.strip()]


def this_file(front_app, last_file):
    """What "this file" means: the Finder selection when Finder is in front, else the
    file Morpheus made or found last, else whatever Finder has selected."""
    if front_app == "Finder":
        sel = finder_selection()
        if sel:
            return sel
    if last_file and os.path.exists(last_file):
        return [last_file]
    return finder_selection()


MEDIA = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg")
FOLDER_ROOTS = ["~/Desktop", "~/Downloads", "~/Documents", "~/Movies", "~/Music", "~/Pictures", "~"]


def folders(depth=2):
    """Folders a person would name out loud: the usual places and two levels inside them.
    [(name, path)], skipping hidden folders, app bundles and code clutter."""
    out, seen = [], set()
    skip = {"node_modules", "Library", "Applications", ".venv", "venv", "__pycache__", "site-packages"}

    def walk(path, level):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            if (e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in skip
                    and not e.name.endswith((".app", ".photoslibrary", ".bundle")) and e.path not in seen):
                seen.add(e.path)
                out.append((e.name, e.path))
                if level < depth:
                    walk(e.path, level + 1)
    for root in FOLDER_ROOTS:
        walk(os.path.expanduser(root), 1 if root != "~" else depth)
    return out


def media_in(folder):
    try:
        return sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(MEDIA))
    except OSError:
        return []


def compress(paths, done):
    """Zip files and folders; shrink a video instead (zipping a video saves nothing).
    Runs in the background and calls done(report) when finished."""
    def work():
        try:
            if len(paths) == 1 and paths[0].lower().endswith(VIDEO_EXT):
                src = paths[0]
                out = os.path.splitext(src)[0] + " (small).mp4"
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:v", "hevc_videotoolbox",
                                    "-q:v", "55", "-tag:v", "hvc1", "-c:a", "aac", "-b:a", "128k", out],
                                   capture_output=True, text=True, timeout=3600)
                if r.returncode:
                    return done(f"Couldn't shrink the video: {r.stderr.strip()[-120:]}", None)
                before, after = os.path.getsize(src), os.path.getsize(out)
                return done(f"Shrank {os.path.basename(src)}: {before / 1e6:.0f} MB to {after / 1e6:.0f} MB", out)
            parent = os.path.dirname(paths[0])
            if len(paths) == 1:
                out = paths[0] + ".zip"
                cmd = ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", paths[0], out]
            else:
                out = os.path.join(parent, f"Archive {stamp()}.zip")
                cmd = ["zip", "-qry", out] + [os.path.basename(p) for p in paths]
            r = subprocess.run(cmd, cwd=parent, capture_output=True, text=True, timeout=3600)
            if r.returncode:
                return done(f"Couldn't compress: {r.stderr.strip()[-120:]}", None)
            done(f"Compressed to {os.path.basename(out)} ({os.path.getsize(out) / 1e6:.1f} MB)", out)
        except Exception as e:
            done(f"Couldn't compress: {e}", None)
    threading.Thread(target=work, daemon=True).start()


# ---- screen ----------------------------------------------------------------

def screenshot_dir():
    r = subprocess.run(["defaults", "read", "com.apple.screencapture", "location"], capture_output=True, text=True)
    path = os.path.expanduser(r.stdout.strip()) if r.returncode == 0 else ""
    return path if os.path.isdir(path) else os.path.expanduser("~/Desktop")


def front_window_rect():
    app = ax.AX.AXUIElementCreateApplication(ax.front_pid())
    win = ax._get(app, "AXFocusedWindow")
    if win is None:
        return None
    pos, size = ax._get(win, "AXPosition"), ax._get(win, "AXSize")
    _, p = ax.AX.AXValueGetValue(pos, ax.AX.kAXValueCGPointType, None)
    _, s = ax.AX.AXValueGetValue(size, ax.AX.kAXValueCGSizeType, None)
    return int(p.x), int(p.y), int(s.width), int(s.height)


def screenshot(window_only):
    path = os.path.join(screenshot_dir(), f"Screenshot {stamp()}.png")
    cmd = ["screencapture", "-x"]
    rect = front_window_rect() if window_only else None
    if rect:
        cmd += ["-R", ",".join(map(str, rect))]
    subprocess.run(cmd + [path], timeout=20)
    if not os.path.exists(path):
        return "Screenshot failed: Morpheus needs Screen Recording permission", None
    return f"Screenshot saved: {os.path.basename(path)}", path


_recording = {"proc": None, "path": None}


def start_recording():
    if _recording["proc"] and _recording["proc"].poll() is None:
        return "Already recording the screen", None
    os.makedirs(RECORDINGS, exist_ok=True)
    path = os.path.join(RECORDINGS, f"Recording {stamp()}.mov")
    _recording.update(proc=subprocess.Popen(["screencapture", "-v", "-x", "-C", path], stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), path=path)
    return "Recording the screen. Say \"Morpheus, stop recording\" to finish", None


def stop_recording():
    proc, path = _recording["proc"], _recording["path"]
    if not proc or proc.poll() is not None:
        return "No screen recording is running", None
    proc.send_signal(signal.SIGINT)   # screencapture saves the movie on Ctrl+C
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    _recording["proc"] = None
    if not os.path.exists(path):
        return "Recording stopped, but nothing was saved (Screen Recording permission?)", None
    return f"Saved recording: {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.0f} MB)", path


def ocr_window():
    """On-screen text of the front window with click points: [(text, (x, y))].

    For what the accessibility tree does not show, such as links inside Zen's web pages.
    """
    import Vision
    rect = front_window_rect()
    if not rect:
        return []
    x0, y0, w, h = rect
    path = "/tmp/morpheus-ocr.png"
    subprocess.run(["screencapture", "-x", "-R", f"{x0},{y0},{w},{h}", path], timeout=20)
    if not os.path.exists(path):
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    handler.performRequests_error_([req], None)
    os.remove(path)
    out = []
    for r in req.results() or []:
        b = r.boundingBox()   # 0-1, origin bottom left
        text = r.topCandidates_(1)[0].string()
        out.append((text, (x0 + (b.origin.x + b.size.width / 2) * w, y0 + (1 - b.origin.y - b.size.height / 2) * h)))
    return out


def on_screen(app, browser):
    """Everything clickable in the front window: [(label, how)], how = AX element or (x, y)."""
    items = [(label, el) for label, role, el in ax.elements(ax.pid_of(app) or None)
             if "zoom the window" not in label]
    if browser or len(items) < 5:   # web pages and custom-drawn apps: read the text off the screen too
        items += ocr_window()
    seen, out = set(), []
    for label, how in items:
        label = label.replace("‎", "").strip()
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append((label, how))
    return out


def rank(items, words, n=25):
    """The items whose labels best match the spoken words, best first."""
    words = [w for w in words if len(w) > 1]
    def score(label):
        low = label.lower()
        parts = re.findall(r"[a-z0-9']+", low)
        s = difflib.SequenceMatcher(None, " ".join(words), low).ratio()
        s += sum(any(p.startswith(w) or difflib.SequenceMatcher(None, w, p).ratio() > 0.8 for p in parts) for w in words)
        return s
    return sorted(items, key=lambda it: score(it[0]), reverse=True)[:n]


def click(how):
    if isinstance(how, tuple):
        ax.click_at(*how)
        return True
    return ax.press(how)


# ---- web -------------------------------------------------------------------

def spoken_url(words):
    """ "example dot com" -> "example.com"; None when the words name no address."""
    text = re.sub(r"\s+dot\s+", ".", words.lower().strip())
    text = re.sub(r"\b(website|site|web ?page|landing page|home ?page|the|page)\b", " ", text)
    m = re.search(r"([a-z0-9-]+(?:\s+[a-z0-9-]+){0,3})\.([a-z]{2,6})(\.[a-z]{2})?\b", text)
    if not m:
        return None
    # "go to forge growth" -> "forge growth": the name is the words before the dot, minus the instruction.
    name = []
    for w in reversed(m.group(1).split()):
        if w in URL_STOP:
            break
        name.insert(0, w)
    return "".join(name) + "." + m.group(2) + (m.group(3) or "") if name else None


URL_STOP = {"go", "to", "open", "visit", "take", "me", "please", "and", "on", "in", "at", "copy", "text", "of", "from", "check", "out"}


def website_url(words):
    """An address when the words say one; else DuckDuckGo's jump-to-first-result."""
    host = spoken_url(words)
    if host:
        return "https://" + host
    name = re.sub(r"\b(website|site|web ?page|landing page|home ?page|the)\b", " ", words, flags=re.I).strip()
    return "https://duckduckgo.com/?q=" + urllib.parse.quote_plus("\\" + name + " official site")


def browser_url(app):
    """The address of the page open in the front browser."""
    if app in ("Google Chrome", "Brave Browser"):
        r = subprocess.run(["osascript", "-e", f'tell application "{app}" to URL of active tab of front window'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    if app == "Safari":
        r = subprocess.run(["osascript", "-e", 'tell application "Safari" to URL of front document'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    # Zen and Firefox have no AppleScript: copy the address bar, then put the clipboard back.
    saved = clipboard_text()
    activate(app)
    keys("cmd l")
    time.sleep(0.15)
    keys("cmd c")
    time.sleep(0.25)
    keys("escape")
    url = clipboard_text()
    set_clipboard_text(saved)
    return url if url.startswith("http") else ""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ("script", "style", "noscript", "svg")

    def handle_endtag(self, tag):
        self.skip -= tag in ("script", "style", "noscript", "svg") and self.skip > 0

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(" ".join(data.split()))


def page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Morpheus"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    p = _Text()
    p.feed(html)
    return "\n".join(p.parts)


def copy_front_text(app, browser):
    """Copy all the text in the front window; for a web page that copies nothing, fetch the page."""
    before = NSPasteboard.generalPasteboard().changeCount()
    keys("cmd a")
    time.sleep(0.15)
    keys("cmd c")
    time.sleep(0.4)
    text = clipboard_text() if NSPasteboard.generalPasteboard().changeCount() != before else ""
    if browser and len(text) < 40:
        url = browser_url(app)
        if url:
            text = page_text(url)
            set_clipboard_text(text)
    if not text:
        return "Nothing was copied"
    if not browser:
        keys("right")   # drop the selection, so the next key press doesn't replace the text
    preview = " ".join(text.split())[:60]
    return f"Copied {len(text):,} characters: \"{preview}...\""


# ---- downloads -------------------------------------------------------------

def yt(*args, timeout=900):
    return subprocess.run([sys.executable, "-m", "yt_dlp", *args], capture_output=True, text=True, timeout=timeout)


def video_search(query, shorts):
    r = yt("--flat-playlist", "--print", "%(id)s\t%(duration)s\t%(title)s", f"ytsearch10:{query}{' shorts' if shorts else ''}",
           timeout=60)
    found = []
    for line in r.stdout.splitlines():
        vid, dur, title = (line.split("\t", 2) + ["", ""])[:3]
        dur = float(dur) if dur not in ("", "NA") else None
        found.append({"url": f"https://www.youtube.com/watch?v={vid}", "title": title, "duration": dur})
    if shorts:
        short = [v for v in found if v["duration"] and v["duration"] <= SHORT_MAX_S]
        found = short or found
    return found


def download(url):
    """Download a video as MP4 (plays in VLC, sends on WhatsApp). Returns (report, path)."""
    os.makedirs(DOWNLOADS, exist_ok=True)
    r = yt("-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4",
           "--no-playlist", "-o", os.path.join(DOWNLOADS, "%(title).80s [%(id)s].%(ext)s"),
           "--print", "after_move:filepath", "--no-simulate", url)
    path = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode or not os.path.exists(path):
        return f"Download failed: {r.stderr.strip().splitlines()[-1][:120] if r.stderr.strip() else 'unknown error'}", None
    return f"Downloaded {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.0f} MB)", path


# ---- WhatsApp --------------------------------------------------------------

WHATSAPP_ME = CONFIG.get("whatsapp_self_chat", "")   # your own chat, shown as "(You)": what "send it to me" means
MYSELF = {n.lower() for n in CONFIG.get("my_names", [])} if WHATSAPP_ME else set()
WA_UI = {"you", "edit", "more", "new chat", "clear text", "search", "photos", "gifs", "links", "videos", "documents",
         "audio", "polls", "events", "share media", "emoji picker", "voice message", "compose message"}


def _wa_rows():
    """Chat rows in WhatsApp's list. Rows have no accessibility identifier; the
    sidebar, header (same name as the open chat) and message bubbles all have one."""
    out = []
    for label, role, el in ax.elements(ax.pid_of("WhatsApp")):
        label = label.replace("\u200e", "").strip()
        if (role in ("AXButton", "AXCell", "AXStaticText") and label and not ax._get(el, "AXIdentifier")
                and label.lower() not in WA_UI and not label.startswith("Archived") and "end-to-end" not in label
                and "zoom the window" not in label):
            out.append((label, el))
    return out


def whatsapp_chats(contact):
    """Search WhatsApp for a contact; returns the chats it lists: [(name, element)]."""
    if contact.lower() in MYSELF:
        contact = WHATSAPP_ME
    if not activate("WhatsApp"):
        return []
    pid = ax.pid_of("WhatsApp")
    clear = ax.find(pid, "TokenizedSearchBar_DeleteButton")   # drop any old search and filter chip
    if clear is not None:
        ax.press(clear)
        time.sleep(0.3)
    keys("cmd f")
    time.sleep(0.4)
    keys("cmd a")
    type_text(contact)
    # Results take 1-3 s to replace the normal chat list: wait for the contact to show.
    want = contact.lower()
    rows = []
    for _ in range(12):
        time.sleep(0.4)
        rows = _wa_rows()
        if any(label.lower() == want for label, _ in rows):
            break
    exact = [r for r in rows if r[0].lower() == want]
    return exact + [r for r in rows if r not in exact]


def whatsapp_open(name, el):
    """Open a chat from the list; True once its header shows the name."""
    click(el)
    pid = ax.pid_of("WhatsApp")
    for _ in range(15):
        time.sleep(0.2)
        head = ax.find(pid, "NavigationBar_HeaderViewButton")
        if head is not None and ax._label(head).replace("\u200e", "").strip().lower() == name.lower():
            clear = ax.find(pid, "TokenizedSearchBar_DeleteButton")
            if clear is not None:
                ax.press(clear)
            return True
    return False


def whatsapp_attach(paths=None, message=None):
    """In the open chat: paste the files (WhatsApp shows them ready to send) or type the message.
    The message box is focused first, so nothing lands in the search box."""
    pid = ax.pid_of("WhatsApp")
    box = ax.find(pid, "ChatBar_ComposerTextView")
    if box is None:
        return False
    at = ax._center(box)          # WhatsApp ignores an accessibility focus request: click into the box
    if at is None:
        return False
    ax.click_at(*at)
    time.sleep(0.3)
    if paths:
        set_clipboard_files(paths)
        keys("cmd v")
        time.sleep(1.5)
    if message:
        type_text(message)
    return True


def whatsapp_press(identifier):
    el = ax.find(ax.pid_of("WhatsApp"), identifier)
    return el is not None and ax.press(el)


# ---- saved commands --------------------------------------------------------

def saved_commands():
    try:
        with open(COMMANDS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_saved(name):
    cmd = saved_commands()[name]
    r = subprocess.run(["/bin/zsh", "-lc", cmd], capture_output=True, text=True, timeout=300)
    out = (r.stdout.strip() or r.stderr.strip()).splitlines()
    tail = " | ".join(out[-3:])[:180]
    return f"{name}: {tail}" if r.returncode == 0 else f"{name} failed: {tail}"


# ---- system controls -------------------------------------------------------

# The keys on the top row of a Mac keyboard, sent the way the keyboard sends them.
SPECIAL_KEYS = {"volume up": 0, "volume down": 1, "brightness up": 2, "brightness down": 3, "mute": 7,
                "play pause": 16, "next track": 17, "previous track": 18,
                "keyboard light up": 21, "keyboard light down": 22}


def special_key(name, times=1):
    from AppKit import NSEvent
    for _ in range(times):
        for down in (True, False):
            flags = 0xa00 if down else 0xb00
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), flags, 0, 0, None, 8, (SPECIAL_KEYS[name] << 16) | flags, -1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())
            time.sleep(0.03)


def _osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr.strip()


# Spoken setting -> (what it does, needs a yes first). Clef picks the setting name.
SYSTEM = {
    "brightness up": ("Brightness up", False), "brightness down": ("Brightness down", False),
    "brightness max": ("Brightness to full", False), "brightness low": ("Brightness low", False),
    "brightness half": ("Brightness to half", False),
    "keyboard light up": ("Keyboard light up", False), "keyboard light down": ("Keyboard light down", False),
    "play or pause music": ("Play/pause", False), "next track": ("Next track", False), "previous track": ("Previous track", False),
    "dark mode on": ("Dark mode on", False), "dark mode off": ("Dark mode off", False), "toggle dark mode": ("Dark mode switched", False),
    "wifi on": ("Wi-Fi on", False), "wifi off": ("Wi-Fi off", True),
    "lock screen": ("Locked the screen", False), "sleep": ("Going to sleep", False),
    "turn the display off": ("Display off", False), "show desktop": ("Showing the desktop", False),
    "empty trash": ("Emptied the Trash", True), "restart": ("Restarting", True),
    "shut down": ("Shutting down", True), "log out": ("Logging out", True),
}


def system(setting):
    """Do one SYSTEM setting; returns a report line."""
    label = SYSTEM[setting][0]
    if setting in ("brightness up", "brightness down", "keyboard light up", "keyboard light down", "next track", "previous track"):
        special_key(setting, 2 if "brightness" in setting else 1)
    elif setting in ("brightness max", "brightness low", "brightness half"):
        special_key("brightness down", 16)          # 16 steps from full to off: start from the bottom
        special_key("brightness up", {"brightness max": 16, "brightness low": 3, "brightness half": 8}[setting])
    elif setting == "play or pause music":
        special_key("play pause")
    elif setting.startswith("dark mode") or setting == "toggle dark mode":
        value = {"dark mode on": "true", "dark mode off": "false"}.get(setting, "not dark mode")
        ok, err = _osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {value}')
        if not ok:
            return f"Couldn't change dark mode: {err[:80]}"
    elif setting.startswith("wifi"):
        r = subprocess.run(["networksetup", "-setairportpower", "en0", setting.split()[1]], capture_output=True, text=True)
        if r.returncode:
            return f"Couldn't switch Wi-Fi: {r.stderr.strip()[:80]}"
    elif setting == "lock screen":
        keys("ctrl cmd q")
    elif setting == "sleep":
        subprocess.Popen(["pmset", "sleepnow"])
    elif setting == "turn the display off":
        subprocess.Popen(["pmset", "displaysleepnow"])
    elif setting == "show desktop":
        _osa('tell application "System Events" to key code 103')   # F11
    elif setting == "empty trash":
        ok, err = _osa('tell application "Finder" to empty trash')
        if not ok:
            return f"Couldn't empty the Trash: {err[:80]}"
    elif setting in ("restart", "shut down", "log out"):
        # Through System Events, so apps get the normal chance to save or object.
        action = {"restart": "restart", "shut down": "shut down", "log out": "log out"}[setting]
        ok, err = _osa(f'tell application "System Events" to {action}')
        if not ok:
            return f"Couldn't {setting}: {err[:80]}"
    return label


# ---- finding things anywhere on the Mac --------------------------------------

def spotlight(words, folders_only=False, limit=40):
    """Files or folders whose names contain any of the words, newest first, from
    Spotlight's index: the whole Mac in a fraction of a second. [(name, path)]"""
    words = [w for w in words if len(w) > 2][:5]
    if not words:
        return []
    name = " || ".join(f'kMDItemFSName == "*{w}*"cd' for w in words)
    query = f"({name})" + (' && kMDItemContentType == "public.folder"' if folders_only else "")
    try:
        out = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), query],
                             capture_output=True, text=True, timeout=10).stdout.splitlines()
    except subprocess.TimeoutExpired:
        return []
    keep = [p for p in out if "/Library/" not in p and "/." not in p and "/node_modules/" not in p
            and ".app/" not in p and "/.venv/" not in p]
    return [(os.path.basename(p), p) for p in keep[:limit * 5]]


# ---- web search, without opening a browser -----------------------------------

def jina_key():
    r = subprocess.run(["security", "find-generic-password", "-s", "morpheus", "-a", "jina_api_key", "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def web_results(query, n=5):
    """Top web results as [{"title", "url", "text"}]: Jina search with your key, DuckDuckGo without."""
    key = jina_key()
    if key:
        req = urllib.request.Request("https://s.jina.ai/?q=" + urllib.parse.quote(query),
                                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                              "X-Respond-With": "no-content"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return [{"title": d.get("title", ""), "url": d.get("url", ""), "text": d.get("description", "")}
                for d in (data.get("data") or [])[:n]]
    import html as html_lib
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode(),
                                 headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"})
    body = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body)
    texts = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    clean = lambda s: html_lib.unescape(re.sub("<.*?>", "", s)).strip()
    out = []
    for (url, title), text in list(zip(links, texts))[:n]:
        m = re.search(r"uddg=([^&]+)", url)
        out.append({"title": clean(title), "url": urllib.parse.unquote(m.group(1)) if m else url, "text": clean(text)})
    return out
