"""Clef desktop agent (Morpheus).

Local code observes the Mac and lists candidate actions; Cloudflare Clef (a
decision model, not a chat model) picks one; local code runs it. Clef cannot
write text, so search words are chosen from spans of the user's own request.

A command with several tasks ("open VLC and search X on Zen") is split first,
at "and" / "then" / "also" only when an action word follows, so "salt and
pepper" stays together. Each task then runs on its own, passing on the app,
file, folder or website it used ("download it and play it on VLC").

Env: CF_ACCOUNT_ID, CF_API_TOKEN (Workers AI permission), CLEF_MODEL (default clef).
Usage: python3 agent.py "open vlc and search what is my name on zen"
"""
import base64
import difflib
import glob
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import actions

ACCOUNT = os.environ["CF_ACCOUNT_ID"]
TOKEN = os.environ["CF_API_TOKEN"]
MODEL = os.environ.get("CLEF_MODEL", "clef")
MAX_STEPS = 4                  # per task; a task is usually one or two actions
MAX_APP_OPTIONS = 40
MEDIA_EXT = (".mp4", ".mkv", ".mov", ".webm", ".mp3", ".m4a", ".wav", ".flac")
KNOWN_APPS = ["Zen", "Google Chrome", "Brave Browser", "Firefox", "Safari", "VLC", "IINA", "QuickTime Player", "Spotify"]
BROWSERS = {"Zen", "Google Chrome", "Brave Browser", "Firefox", "Safari"}
MEDIA_PLAYERS = ["VLC", "IINA", "QuickTime Player", "Music"]   # first installed one is the default
GENERIC_WORDS = {"open", "play", "watch", "listen", "start", "put", "on", "the", "a", "for", "me", "my", "to", "in", "please", "laptop", "it", "this"}
APP_WORDS = {"zen", "chrome", "brave", "firefox", "safari", "vlc", "iina", "quicktime", "spotify", "browser", "ghostty", "ghosty"}
EDGE_WORDS = {"on", "for", "me", "my", "the", "a", "an", "in", "to", "please", "laptop", "mac", "computer", "and", "it", "this"}
SPLIT_WORDS = {"and", "then", "also"}
# A new task starts with an action or a request ("and open ...", "and I want you to ...");
# "tom and jerry" or "salt and pepper" never does, so those "and"s are not offered to Clef.
TASK_STARTS = {"open", "search", "play", "set", "turn", "quit", "close", "check", "list", "show", "find", "go",
               "start", "launch", "make", "send", "read", "tell", "put", "mute", "pause", "stop", "add", "create",
               "i", "can", "could", "please", "then", "also", "and", "lower", "raise", "increase", "decrease",
               "download", "compress", "zip", "shrink", "take", "screenshot", "record", "copy", "paste", "type",
               "write", "click", "tap", "press", "run", "visit", "save", "select", "undo", "redo", "refresh",
               "reload", "scroll", "minimize", "share", "message"}
FILLER = re.compile(r"^(?:(?:hey|ok|okay|so|um|uh|please|can you|could you|would you|i want you to|i need you to|"
                    r"help me to|help me|go ahead and|and then|and|also|then|you)\s+)+", re.I)
VOLUME = {"mute": "0", "low": "25", "medium": "50", "high": "75", "max": "100", "up": "+15", "down": "-15"}
NUMBER_WORDS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def workspaces():
    """AeroSpace workspace names, e.g. 1-9 and letters; none if AeroSpace is not running."""
    try:
        out = subprocess.run(["aerospace", "list-workspaces", "--all"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out


def focused_app():
    try:
        return subprocess.run(["aerospace", "list-windows", "--focused", "--format", "%{app-name}"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""

TOOLS = {
    "list_open_apps": "Report which apps are open right now",
    "open_app": "Open an app, or bring it to the front",
    "quit_app": "Quit an app",
    "new_tab": "Open a new tab or window in an app (presses Cmd+T)",
    "web_search": "Search the web for words from the request, in a browser app",
    "find_media_file": "Look for a video or audio file on this Mac matching words from the request",
    "open_file_in_app": "Open a file that was already found, in a chosen app",
    "set_volume": "Change the sound volume",
    "open_in_workspace": "Open an app and put its window on a numbered or lettered workspace",
    "go_to_workspace": "Switch the screen to a workspace",
    "take_screenshot": "Take a screenshot of the whole screen or the front window",
    "start_recording": "Start recording the screen",
    "stop_recording": "Stop the screen recording and save it",
    "compress_file": "Compress the selected file: zip it, or shrink it if it is a video",
    "download_video": "Search YouTube for a video matching words from the request and download it",
    "download_current_video": "Download the video on the page open in the browser right now",
    "open_website": "Open a website by its name or address in a browser",
    "copy_page_text": "Copy all the text in the front window or web page to the clipboard",
    "press_shortcut": "Press a keyboard shortcut in the front app: copy, paste, undo, save, refresh, enter, scroll...",
    "type_text": "Type words from the request into the front app",
    "click": "Click a button, link, chat or other item on screen by its name",
    "send_whatsapp": "Send a file or a message to a contact on WhatsApp",
    "run_saved_command": "Run one of the user's saved terminal commands",
    "open_folder": "Open a folder on this Mac in Finder, wherever it is",
    "open_file": "Find a file or document anywhere on this Mac by its name and open it",
    "open_search_result": "Open one of the web results found earlier (the first one, the second one...)",
    "system_control": "Change a Mac setting: brightness, keyboard light, music play/pause, dark mode, Wi-Fi, lock, sleep, restart, shut down, empty trash",
    "terminal_command": "Run a program or command the user names (tmux, htop, git status...) in the terminal app",
    "finish": "The task is fully done; stop",
    "ask_user": "The task is unclear or unsafe; ask the user",
}
# The task's first word narrows which actions Clef may pick: it chose "open app"
# for "search the latest Naval podcast on Zen", so obvious intent is not left to it.
# First match wins, so "search X and download it" is a download, not a web search.
INTENTS = [
    (re.compile(r".*\bdownload\b", re.I), {"download_video", "download_current_video", "ask_user"}),
    (re.compile(r".*\bscreen ?shot\b", re.I), {"take_screenshot", "ask_user"}),
    (re.compile(r"^\W*(stop|end|finish|save)\b.*\brecord", re.I), {"stop_recording", "ask_user"}),
    (re.compile(r"^\W*((start|begin) (a |the )?(screen )?record|record (my |the )?screen|screen ?record)", re.I),
     {"start_recording", "ask_user"}),
    (re.compile(r"^\W*(compress|zip|shrink)\b", re.I), {"compress_file", "ask_user"}),
    (re.compile(r"^\W*send\b.*\bwhats ?app\b", re.I), {"send_whatsapp", "ask_user"}),
    (re.compile(r".*\bwhats ?app\b.*\b(send|message|text)\b", re.I), {"send_whatsapp", "ask_user"}),
    (re.compile(r"^\W*(go to|open|visit|take me to)\b.*(\bwebsite\b|\bsite\b|\bdot (com|ng|org|io|net|ai|co)\b|\.[a-z]{2,6}\b)", re.I),
     {"open_website", "ask_user"}),
    (re.compile(r"^\W*copy\b", re.I), {"copy_page_text", "press_shortcut", "ask_user"}),
    (re.compile(r"^\W*(click|tap)\b", re.I), {"click", "ask_user"}),
    (re.compile(r"^\W*press\b", re.I), {"press_shortcut", "click", "ask_user"}),
    (re.compile(r"^\W*(paste|undo|redo|save|refresh|reload|select all|scroll|go back|go forward|minimize)\b", re.I),
     {"press_shortcut", "ask_user"}),
    (re.compile(r"^\W*(type|write)\b", re.I), {"type_text", "ask_user"}),
    (re.compile(r"^\W*open (the )?(first|second|third|fourth|fifth|top|last|1st|2nd|3rd|number \w+|\d)\b( one| result| link)?\W*$", re.I),
     {"open_search_result", "ask_user"}),
    (re.compile(r".*\b(bright(ness|er|en)?|dim|dimmer|darker|dark mode|light mode|wi-?fi|restart|reboot|shut ?down|sleep|lock( the| my)? screen|"
                r"lock (it|my mac|the mac)|log ?out|trash|next (song|track)|previous (song|track)|(pause|resume|play) (the )?(music|song)|"
                r"keyboard (light|backlight))\b", re.I), {"system_control"}),   # restart and the like ask for a yes themselves
    (re.compile(r".*\b(folder|directory)\b", re.I), {"open_folder", "ask_user"}),
    (re.compile(r"^\W*(open|find|show)\b.*\b(file|document|doc|pdf|report|spreadsheet|sheet|presentation|deck|slides|image|photo|picture|"
                r"note|notes|zip)\b", re.I), {"open_file", "ask_user"}),
    (re.compile(r".*\b(terminal|ghostty|tmux)\b", re.I), {"terminal_command", "open_app", "new_tab", "run_saved_command", "ask_user"}),
    (re.compile(r"^\W*run\b", re.I), {"run_saved_command", "terminal_command", "ask_user"}),
    (re.compile(r"^\W*(search|google|look up)\b", re.I), {"web_search", "ask_user"}),
    (re.compile(r"^\W*(quit|close|exit|kill)\b", re.I), {"quit_app", "ask_user"}),
    (re.compile(r"^\W*(play|watch|listen)\b", re.I), {"find_media_file", "open_file_in_app", "open_app", "web_search", "ask_user"}),
    (re.compile(r"^\W*(go to|switch to)\b.*workspace", re.I), {"go_to_workspace", "ask_user"}),
    (re.compile(r".*\b(sound|volume|louder|quieter|softer|mute|unmute)\b", re.I), {"set_volume", "system_control", "ask_user"}),
]


def allowed_tools(task):
    for pattern, tools in INTENTS:
        if pattern.match(task):
            return tools
    # Sending reaches other people: only when the task says WhatsApp ("call Nancy to come
    # out here", said to someone in the room, once became a WhatsApp send).
    return set(TOOLS) - {"send_whatsapp"}


# These finish the task they run in; only finding a file needs a next step.
ONE_STEP_TOOLS = set(TOOLS) - {"find_media_file", "finish", "ask_user"}

_conn = None
timings = []


def clef(state, questions, image=None):
    """One Clef decision over a kept-alive HTTPS connection."""
    global _conn
    body = {"model": MODEL, "state": state, "questions": questions}
    if image:
        body["images"] = [image]
    for attempt in range(2):
        if _conn is None:
            _conn = http.client.HTTPSConnection("api.cloudflare.com", timeout=60)
        t = time.time()
        try:
            _conn.request("POST", f"/client/v4/accounts/{ACCOUNT}/ai/run/@cf/cloudflare/{MODEL}",
                          body=json.dumps(body), headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
            resp = _conn.getresponse()
            data = json.loads(resp.read())
        except (http.client.HTTPException, OSError):
            _conn = None
            if attempt:
                raise
            continue
        timings.append((time.time() - t) * 1000)
        if not data.get("success", False):
            raise RuntimeError(json.dumps(data.get("errors"))[:300])
        return data["result"]
    raise RuntimeError("no response")


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()


def osa(script):
    """Run AppleScript; returns (ok, error text)."""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr.strip()


def running_apps():
    js = ('ObjC.import("AppKit"); var a=$.NSWorkspace.sharedWorkspace.runningApplications; var o=[];'
          'for (var i=0;i<a.count;i++){var x=a.objectAtIndex(i); if (x.activationPolicy==0) o.push(ObjC.unwrap(x.localizedName));}'
          'JSON.stringify(o)')
    return [a.replace("‎", "") for a in json.loads(sh("osascript", "-l", "JavaScript", "-e", js) or "[]")]


def frontmost_app():
    js = 'ObjC.import("AppKit"); ObjC.unwrap($.NSWorkspace.sharedWorkspace.frontmostApplication.localizedName)'
    return sh("osascript", "-l", "JavaScript", "-e", js).replace("‎", "")


def all_apps():
    paths = glob.glob("/Applications/*.app") + glob.glob("/System/Applications/*.app") + glob.glob(os.path.expanduser("~/Applications/*.app"))
    return sorted({os.path.basename(p)[:-4] for p in paths})


def app_options(text):
    """Apps worth offering: open ones, the usual ones, and any the request names (even misheard)."""
    installed = all_apps()
    words = re.findall(r"[a-z0-9]+", text.lower())
    named = []
    for app in installed:
        parts = re.findall(r"[a-z0-9]+", app.lower())
        if any(difflib.SequenceMatcher(None, w, p).ratio() >= 0.8 for w in words for p in parts if len(p) > 2):
            named.append(app)
    out = list(dict.fromkeys(named + [a for a in running_apps() if a in installed] + [a for a in KNOWN_APPS if a in installed]))
    # Morpheus is an app too: "open the Morpheus voices folder" once opened it, "that's all" once quit it.
    return [a for a in out if a != "Morpheus"][:MAX_APP_OPTIONS]


def screenshot_b64():
    from PIL import Image
    path = tempfile.mktemp(suffix=".png")
    subprocess.run(["screencapture", "-x", "-t", "png", path], timeout=15)
    img = Image.open(path).convert("RGB")
    os.remove(path)
    img.thumbnail((1280, 800))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def spans(text, max_len=8):
    """Search-phrase candidates: spans of the request, minus app names and edge filler."""
    words = re.findall(r"[A-Za-z0-9']+", text)
    out = {}
    for n in range(1, min(max_len, len(words)) + 1):
        for i in range(len(words) - n + 1):
            part = words[i:i + n]
            low = [w.lower() for w in part]
            if APP_WORDS & set(low) or low[0] in EDGE_WORDS or low[-1] in EDGE_WORDS:
                continue
            s = " ".join(part)
            if s not in out.values():
                out[f"q{len(out)}"] = s
    if len(out) < 2:
        out.update({"q_all": text, "q_none": "(nothing to search)"})
    return out


_media = {"files": None, "at": 0.0}
MEDIA_REFRESH_S = 600


def _scan_media():
    out = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), "kMDItemContentTypeTree == 'public.audiovisual-content'"],
                         capture_output=True, text=True, timeout=120).stdout
    _media["files"] = [p for p in out.splitlines() if p.lower().endswith(MEDIA_EXT) and "/Library/" not in p]
    _media["at"] = time.time()


def media_files():
    """Every audio and video file in the home folder that Spotlight knows about.

    Listing them takes about 20 s, so the list is built in the background at
    start and kept for MEDIA_REFRESH_S; a search waits only on the first scan.
    """
    if _media["files"] is None:
        _scan_media()
    elif time.time() - _media["at"] > MEDIA_REFRESH_S:
        _media["at"] = time.time()
        threading.Thread(target=_scan_media, daemon=True).start()
    return _media["files"]


threading.Thread(target=_scan_media, daemon=True).start()


def find_media(query):
    """Media files whose names share most of the query's words, loosely:
    "truth" matches "Truths" and a misheard "neval" matches "Naval"."""
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 1 and w.lower() not in {"the", "podcast", "video", "episode", "song", "file"}]
    if not words:
        return []
    def close(w, n):
        return n.startswith(w) or w.startswith(n) or (len(w) > 3 and difflib.SequenceMatcher(None, w, n).ratio() >= 0.75)
    def score(p):
        name = set(re.findall(r"[a-z0-9]+", os.path.basename(p).lower()))
        return sum(any(close(w, n) for n in name if len(n) > 1) for w in words)
    need = max(1, (len(words) + 1) // 2)
    ranked = sorted((p for p in media_files() if score(p) >= need), key=score, reverse=True)
    return list(dict.fromkeys(ranked))[:8]


def pick(answer, allowed):
    """Clef's choice if it is allowed here, else its most likely option that is."""
    if answer["choice"] in allowed:
        return answer["choice"]
    ok = sorted(((p, a) for a, p in answer["probabilities"].items() if a in allowed), reverse=True)
    return ok[0][1] if ok else None


def clean(text):
    return FILLER.sub("", text.strip(" ,.!?;")).strip(" ,.!?;")


LEAD = re.compile(r"^\W*(?:search(?: for)?|google|look up|look for|find(?: me)?|download|get(?: me)?|go to|open|visit|play)\s+", re.I)
TAIL = re.compile(r"\s+(?:and (?:download|save|get) (?:it|that|them)"
                  r"|(?:on|in|using) (?:the )?(?:youtube|zen|chrome|brave|safari|firefox|google|the web|the internet|my mac|my laptop)(?: (?:in )?(?:the )?browser)?"
                  r"|in (?:the )?browser|for me|with \w+|please)\s*$", re.I)


def rest(goal):
    """The request without its leading verb and trailing filler: often exactly the search words."""
    text = LEAD.sub("", goal)
    while TAIL.search(text):
        text = TAIL.sub("", text)
    return text.strip(" ,.!?")


def choose(goal, instructions, options, state=None):
    """One Clef choice among local options; returns the chosen key (never the "none" filler)."""
    if len(options) < 2:
        options = {**options, "none": "none of these"}
    ans = clef(state or {"task": goal}, {"x": {"type": "choice", "instructions": instructions, "criteria": options}})["answers"]["x"]
    print(f"    {instructions[:50]}... -> {options.get(ans['choice'], ans['choice'])!r} ({ans['probabilities'][ans['choice']]:.0%}) [{timings[-1]:.0f} ms]")
    choose.sure = ans["probabilities"][ans["choice"]]   # how sure Clef was, for callers that ask when unsure
    return ans["choice"]


def minutes(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}" if seconds else "?"


PRONOUN = re.compile(r"\b(it|that|this|them|the (video|file|recording|screenshot|download|document))\b", re.I)
WANTS_FILE = re.compile(r"\b(file|document|doc|pdf|video|photo|picture|image|screenshot|recording|download|zip)\b"
                        r"|^\W*send (it|this|that|them)\b", re.I)

# The file Morpheus made or found last ("take a screenshot", then "send it to ME on WhatsApp").
last = {"file": None, "results": []}

NAME_NOISE = re.compile(r"\b(open|show|find|go to|look for|the|a|an|folder|directory|file|document|for me|please|on|in|at|my|"
                        r"desktop|downloads|documents|mac|laptop|computer|called|named|from|of)\b", re.I)


def name_words(goal):
    """The words that name the thing: "open the voices folder on my desktop" -> ["voices"]."""
    return re.findall(r"[a-z0-9']+", NAME_NOISE.sub(" ", goal.lower()))


def find_by_name(goal, words, places, folder):
    """A file or folder named by the words, anywhere in your home folder. The quick scan of
    usual places and Spotlight's index together; Clef picks among the best matches."""
    found = actions.spotlight(words, folders_only=folder)
    if folder:
        found = actions.folders() + found
    if places:
        found = [f for f in found if f"/{places[0]}/" in f[1] + "/"] or found
    found = list({p: (n, p) for n, p in found}.values())
    ranked = actions.rank(found, words, n=8)
    if not ranked:
        return None
    # With no place named, the one you worked with lately is the likely one: say how recent each is.
    choice = choose(goal, f"Which {'folder' if folder else 'file'} does the task mean? When the task names no place, prefer one changed recently over an old one with the same words.",
                    {**{f"d{k}": f"{p.replace(os.path.expanduser('~'), '~')} (changed {age(p)})" for k, (_, p) in enumerate(ranked)},
                     "none": "none of these"})
    return ranked[int(choice[1:])][1] if choice.startswith("d") else None


UNSURE = 0.45


def age(path):
    try:
        days = (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return "unknown"
    return "today" if days < 1 else f"{int(days)} days ago" if days < 60 else f"{int(days / 30)} months ago"


ORDINALS = {"first": 0, "1st": 0, "top": 0, "one": 0, "1": 0, "second": 1, "2nd": 1, "two": 1, "2": 1,
            "third": 2, "3rd": 2, "three": 2, "3": 2, "fourth": 3, "four": 3, "4": 3, "fifth": 4, "five": 4, "5": 4}


def result_number(goal):
    for w in re.findall(r"[a-z0-9]+", goal.lower()):
        if w in ORDINALS:
            return ORDINALS[w]
        if w == "last":
            return len(last.get("results") or [1]) - 1
    return 0


STRONG_VERBS = {"open", "search", "play", "close", "quit", "download", "send", "launch"}
HESITATION = re.compile(r"\b(?:um+|uh+|uhm|erm|hmm+|ah+)\b[,.]?\s*", re.I)
# Searching "on YouTube" (or after opening YouTube) searches YouTube itself, not Google.
SITE_SEARCH = {
    "youtube": "https://www.youtube.com/results?search_query=",
    "google": "https://www.google.com/search?q=",
    "github": "https://github.com/search?q=",
    "reddit": "https://www.reddit.com/search/?q=",
    "amazon": "https://www.amazon.com/s?k=",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
    "twitter": "https://x.com/search?q=",
    "x": "https://x.com/search?q=",
    "spotify": "https://open.spotify.com/search/",
}


def search_site(goal, prev_site):
    named = re.search(r"\b(?:on|in|at|from|using)\s+(youtube|google|github|reddit|amazon|wikipedia|twitter|x)\b", goal, re.I)
    if named:
        return named.group(1).lower()
    return prev_site if prev_site in SITE_SEARCH else "google"


def split_tasks(command):
    """Split at "and" / "then" / "also" when the next word starts a new task.

    Clef was asked this at first, but it scored plainly separate tasks ("open VLC
    in workspace 2 and open Chrome ...") at 0.2-0.3; the action-word rule alone
    got every test right and costs no call.
    """
    parts = []
    command = HESITATION.sub("", command)
    # Separate sentences are separate tasks ("Open VLC. Open the Naval podcast."),
    # unless one starts with no action: "Search on Zen. What is your name?" is
    # one search, the speaker just paused before saying what to search for.
    for n, sentence in enumerate(re.split(r"(?<=[.!?;])\s+", command.strip())):
        words = sentence.split()
        if n and parts and words and words[0].lower().strip(",.!?;") not in TASK_STARTS:
            parts[-1] += " " + clean(sentence)
            continue
        low = [w.lower().strip(",.!?;") for w in words]
        cuts = [i for i, w in enumerate(low) if w in SPLIT_WORDS and 0 < i < len(words) - 1 and low[i + 1] in TASK_STARTS]
        # "open Zen open terminal": people chain commands without "and" too. A strong
        # verb after an object starts a new task; cut just before it (keeping the verb).
        bare = [i for i, w in enumerate(low) if w in STRONG_VERBS and i > 1 and low[i - 1] not in TASK_STARTS | EDGE_WORDS
                and (i - 1) not in cuts]
        for i in reversed(bare):   # from the end, so earlier positions stay right
            words.insert(i, "and")
            low.insert(i, "and")
        if bare:
            cuts = [i for i, w in enumerate(low) if w in SPLIT_WORDS and 0 < i < len(words) - 1 and low[i + 1] in TASK_STARTS]
        start = 0
        for i in cuts + [len(words)]:
            part = clean(" ".join(words[start:i]))
            # "search X and download it": the download needs the search, so they are one task.
            if parts and re.match(r"^(download|save|get)\s+(it|that|this|them|the video|one)\b", part, re.I):
                parts[-1] += " and " + part
            elif part:
                parts.append(part)
            start = i + 1
    return parts or [clean(command)]


def run(goal, ctx=None):
    """Do one task. Returns {"report": [...], "ask": None or the task Clef wants clarified,
    "app": app used, "file": file made or found, "confirm": None or an action waiting for a yes}.

    ctx is what the previous task in the same command left: its app ("open Zen and
    search X" searches in Zen) and its file ("download it and play it on VLC").
    """
    ctx = ctx or {}
    prev_app, prev_file = ctx.get("app"), ctx.get("file")
    history, found_files, report, seen = [], [], [], set()
    out = {"report": report, "ask": None, "app": prev_app, "file": prev_file, "confirm": None, "site": ctx.get("site"),
           "folder": ctx.get("folder")}
    if ctx.get("folder") and re.search(r"\b(those|these|them|all|everything|the files|the videos|the songs)\b", goal, re.I):
        found_files = actions.media_in(ctx["folder"])
        history.append(f"media in the folder opened in the previous step: {len(found_files)} files")
    elif prev_file and PRONOUN.search(goal):
        found_files = [prev_file]
        history.append(f"file from the previous step: {os.path.basename(prev_file)}")
    # Tell Clef up front about media on this Mac that matches the task, so "open
    # the Naval podcast" opens the file instead of searching the web.
    # Not for an explicit web search: a local file match there only distracts Clef.
    words = [w for w in re.findall(r"[a-z0-9']+", goal.lower()) if w not in GENERIC_WORDS and w not in APP_WORDS]
    if not found_files and words and re.match(r"^\W*(play|watch|listen|open|put)\b", goal, re.I):
        found_files = find_media(" ".join(words))
        if found_files:
            history.append("media on this Mac matching the task: " + ", ".join(os.path.basename(f) for f in found_files))
    query_options = spans(goal)
    r = rest(goal)
    if r and r not in query_options.values():
        query_options["q_rest"] = r
    apps = app_options(goal)
    if len(apps) < 2:
        apps = list(dict.fromkeys(apps + ["Finder", "Safari"]))
    allowed = allowed_tools(goal)
    print(f"task: {goal}  [model {MODEL}]")
    for step in range(1, MAX_STEPS + 1):
        # A Clef choice needs at least 2 options, so "none" is always one.
        file_options = {"none": "no matching file", **{f"f{i}": os.path.basename(p) for i, p in enumerate(found_files)}}
        if len(file_options) < 2:
            file_options["unknown"] = "not searched for files yet"
        front = frontmost_app()
        state = {"task": goal, "running_apps": running_apps(), "frontmost_app": front,
                 "steps_done": history, "files_found": list(file_options.values())}
        tools = {t: d for t, d in TOOLS.items() if t in allowed | {"finish"}}
        questions = {
            "tool": {"type": "choice", "instructions": "Which single action should run next to complete this task?", "criteria": tools},
            "app": {"type": "choice", "instructions": "If the action needs an app, which one does the task name?", "criteria": {a: a for a in apps}},
            "query": {"type": "choice", "instructions": "If the action searches or opens a website, which exact words from the task name what to search for or the site (just those words, not the instruction)?", "criteria": query_options},
            "file": {"type": "choice", "instructions": "If the action opens a found file, which one matches the task?", "criteria": file_options},
        }
        if allowed & {"set_volume"}:
            questions["volume"] = {"type": "choice", "instructions": "If the action changes the volume, to what level?", "criteria": {k: k for k in VOLUME}}
        if allowed & {"press_shortcut", "copy_page_text"}:
            questions["shortcut"] = {"type": "choice", "instructions": "If the action presses a keyboard shortcut, which one does the task mean?",
                                     "criteria": {k: k for k in actions.SHORTCUTS}}
        if allowed & {"system_control"}:
            questions["setting"] = {"type": "choice", "instructions": "Which Mac setting does the task want changed?",
                                    "criteria": {k: k for k in actions.SYSTEM}}
        if allowed & {"take_screenshot"}:
            questions["area"] = {"type": "choice", "instructions": "Should the screenshot show the whole screen or only the front window?",
                                 "criteria": {"screen": "the whole screen (the default)", "window": "only the front window or app"}}
        saved = actions.saved_commands()
        if allowed & {"run_saved_command"} and saved:
            questions["command"] = {"type": "choice", "instructions": "Which saved command does the task name?",
                                    "criteria": {**{k: k for k in saved}, "none": "none of the saved commands"}}
        spaces = workspaces()
        if len(spaces) >= 2 and allowed & {"open_in_workspace", "go_to_workspace"}:
            questions["workspace"] = {"type": "choice", "instructions": "If the action uses a workspace, which one does the task name?",
                                      "criteria": {w: f"workspace {w}" + (f" ({NUMBER_WORDS[w]})" if w in NUMBER_WORDS else "") for w in spaces}}
        res = clef(state, questions)["answers"]
        # Nothing done yet, so the task cannot be finished ("open VLC" when VLC
        # is already in front): take Clef's best actual action instead.
        res["tool"]["choice"] = pick(res["tool"], allowed - {"finish"} if step == 1 else allowed | {"finish"})
        tool, app = res["tool"]["choice"], res["app"]["choice"]
        query, fkey = query_options[res["query"]["choice"]], res["file"]["choice"]
        p = res["tool"]["probabilities"].get(tool, 0)
        print(f"  step {step}: {tool} ({p:.0%})  app={app}  query={query!r}  [{timings[-1]:.0f} ms]")
        if tool in (None, "finish"):
            break
        if tool == "ask_user":
            out["ask"] = goal
            return out
        # The same action twice made no progress the first time: stop, don't loop.
        key = (tool, app if tool != "find_media_file" else query)
        if key in seen:
            report.append(f"Stopped: {tool} repeated with no new result.")
            break
        seen.add(key)
        named = [a for a in apps if a.lower() in goal.lower()]   # "click send in WhatsApp"
        if tool == "list_open_apps":
            report.append("Open apps: " + ", ".join(running_apps()))
        elif tool == "open_app":
            sh("open", "-a", app)
            report.append(f"Opened {app}")
        elif tool == "quit_app":
            ok, err = osa(f'tell application "{app}" to quit')
            report.append(f"Quit {app}" if ok else f"Couldn't quit {app}: {err[:80]}")
        elif tool == "new_tab":
            actions.activate(app)
            actions.keys("cmd t")
            report.append(f"New tab in {app}")
        elif tool == "web_search":
            named_browser = any(w in goal.lower() for w in ("zen", "chrome", "brave", "firefox", "safari"))
            app = prev_app if prev_app in BROWSERS and not named_browser else pick(res["app"], BROWSERS)
            if not app:
                history.append("no browser available for a web search")
                continue
            site = search_site(goal, ctx.get("site"))
            words = re.sub(rf"\s*\b(on|in|at)\s+{site}\b", "", query, flags=re.I).strip() or query
            sh("open", "-a", app, SITE_SEARCH[site] + urllib.parse.quote_plus(words))
            out["site"] = site
            report.append(f"Searched {words!r} on {site.title()} in {app}")
        elif tool == "set_volume":
            level = VOLUME[res["volume"]["choice"]]
            if level[0] in "+-":
                now = int(sh("osascript", "-e", "output volume of (get volume settings)") or 50)
                level = str(max(0, min(100, now + int(level))))
            osa(f"set volume output volume {level}")
            report.append(f"Volume {level}%")
        elif tool in ("open_in_workspace", "go_to_workspace"):
            if "workspace" not in res:
                report.append("Workspaces need AeroSpace running")
                break
            space = res["workspace"]["choice"]
            if tool == "go_to_workspace":
                sh("aerospace", "workspace", space)
                report.append(f"Went to workspace {space}")
            else:
                sh("open", "-a", app)
                # Wait for the app's window to take focus, then move that window.
                for _ in range(15):
                    if focused_app() == app:
                        break
                    time.sleep(0.2)
                if focused_app() != app:
                    report.append(f"Opened {app}, but it has no window to move to workspace {space}")
                else:
                    sh("aerospace", "move-node-to-workspace", space)
                    report.append(f"Opened {app} on workspace {space}")
        elif tool == "find_media_file":
            found_files = find_media(query)
            names = ", ".join(os.path.basename(f) for f in found_files) or "nothing"
            history.append(f"looked for media matching {query!r}: found {names}")
            if not found_files:
                report.append(f"No file matching {query!r}")
                break
        elif tool == "open_file_in_app":
            if not fkey.startswith("f") or not found_files:
                history.append("tried to open a file but none was found yet")
                continue
            path = found_files[int(fkey[1:])]
            players = [a for a in MEDIA_PLAYERS if os.path.exists(f"/Applications/{a}.app") or os.path.exists(f"/System/Applications/{a}.app")]
            app = pick(res["app"], set(players)) or (players[0] if players else app)
            if len(found_files) > 1 and re.search(r"\b(those|these|them|all|everything)\b", goal, re.I):
                sh("open", "-a", app, *found_files)   # VLC queues them as a playlist
                report.append(f"Playing {len(found_files)} files in {app}")
            else:
                sh("open", "-a", app, path)
                report.append(f"Opened {os.path.basename(path)} in {app}")
            out["file"] = path
        elif tool == "take_screenshot":
            line, out["file"] = actions.screenshot(res["area"]["choice"] == "window")
            report.append(line)
        elif tool == "start_recording":
            line, _ = actions.start_recording()
            report.append(line)
        elif tool == "stop_recording":
            line, out["file"] = actions.stop_recording()
            report.append(line)
        elif tool == "compress_file":
            paths = actions.this_file(front, prev_file or last["file"])
            if not paths:
                report.append("Select the file in Finder first, then say compress")
                break
            def done(line, path):
                if path:
                    last["file"] = path
                actions.notify(line)
            actions.compress(paths, done)
            report.append(f"Compressing {os.path.basename(paths[0])}{f' and {len(paths) - 1} more' if len(paths) > 1 else ''}; you'll get a notice when it's done")
        elif tool == "download_video":
            shorts = bool(re.search(r"\bshorts?\b", goal, re.I))
            words = re.sub(r"\bshorts?\b", "", query, flags=re.I).strip()
            found = actions.video_search(words, shorts)
            if not found:
                report.append(f"No videos found for {words!r}")
                break
            options = {f"v{i}": f"{v['title']} ({minutes(v['duration'])})" for i, v in enumerate(found)}
            choice = choose(goal, "Which video best matches what the task asks for?", options,
                            {"task": goal, "searched_for": words, "wants_a_short_video": shorts})
            if not choice.startswith("v"):
                report.append(f"None of the videos for {words!r} looked right")
                break
            line, out["file"] = actions.download(found[int(choice[1:])]["url"])
            report.append(line)
        elif tool == "download_current_video":
            browser = front if front in BROWSERS else (prev_app if prev_app in BROWSERS else None)
            url = actions.browser_url(browser) if browser else ""
            if not url:
                report.append("No browser page to download from")
                break
            line, out["file"] = actions.download(url)
            report.append(line)
        elif tool == "open_website":
            browsers = [a for a in named if a in BROWSERS]
            app = browsers[0] if browsers else (prev_app if prev_app in BROWSERS else None)
            host = actions.spoken_url(goal)
            url = "https://" + host if host else actions.website_url(query)
            sh(*(["open", "-a", app, url] if app else ["open", url]))
            time.sleep(2.5)   # let the page load before a following "copy the text"
            app = app or frontmost_app()
            site = (host or query).lower().split(".")[0].replace(" ", "")
            out["site"] = site
            report.append(f"Opened {host or query} in {app}")
        elif tool == "copy_page_text":
            if named and named[0] != front:
                actions.activate(named[0])
            app = frontmost_app()
            report.append(actions.copy_front_text(app, app in BROWSERS))
        elif tool == "press_shortcut":
            if named and named[0] != front:
                actions.activate(named[0])
            name = res["shortcut"]["choice"]
            actions.keys(actions.SHORTCUTS[name])
            report.append(f"Pressed {name}")
        elif tool == "type_text":
            text = re.sub(r"^\W*(type|write)( out| down| in)?\s+", "", goal, flags=re.I)
            if named and named[0] != front:
                actions.activate(named[0])
            actions.type_text(text)
            report.append(f"Typed {text[:60]!r}")
        elif tool == "click":
            if named and named[0] != front:
                actions.activate(named[0])
                time.sleep(0.5)
            app = frontmost_app()
            target = re.sub(r"^\W*(click|tap|press)( on)?\s+(the\s+)?", "", goal, flags=re.I)
            target = re.sub(r"\s+(button|link|in \w+|on (the )?screen)$", "", target, flags=re.I)
            items = actions.rank(actions.on_screen(app, app in BROWSERS), re.findall(r"[a-z0-9']+", target.lower()))
            if not items:
                report.append(f"Nothing clickable found in {app}")
                break
            choice = choose(goal, "Which item on screen does the task want clicked?",
                            {**{f"i{k}": label for k, (label, _) in enumerate(items)}, "none": "nothing on screen matches"},
                            {"task": goal, "app": app})
            if not choice.startswith("i"):
                report.append(f"Couldn't find {target!r} on screen")
                break
            label, how = items[int(choice[1:])]
            report.append(f"Clicked {label[:50]!r}" if actions.click(how) else f"Couldn't click {label[:50]!r}")
        elif tool == "send_whatsapp":
            return send_whatsapp(goal, front, prev_file, out)
        elif tool in ("open_folder", "open_file"):
            folder = tool == "open_folder"
            words = name_words(goal)
            places = [p for p in ("Desktop", "Downloads", "Documents") if p.lower() in goal.lower()]
            if folder and not words and places:
                path = os.path.expanduser(f"~/{places[0]}")
            else:
                path = find_by_name(goal, words, places, folder)
                if not path:
                    report.append(f"Couldn't find a {'folder' if folder else 'file'} called {' '.join(words)!r}")
                    break
                if choose.sure < UNSURE:   # several similar names: check before opening the wrong one
                    where = os.path.basename(os.path.dirname(path))

                    def open_it(path=path):
                        sh("open", path)
                        return f"Opened {os.path.basename(path)}"
                    out["confirm"] = {"text": f"Open {os.path.basename(path)} in {where}?", "yes": open_it,
                                      "no": lambda: "Okay. Say where it is, like: open the voices folder on my desktop."}
                    out["folder" if folder else "file"] = path
                    return out
            sh("open", path)
            out["folder" if folder else "file"] = path
            report.append(f"Opened {os.path.basename(path)}" + (" folder" if folder else ""))
        elif tool == "open_search_result":
            results = last.get("results") or []
            n = result_number(goal)
            if not results or n >= len(results):
                report.append("No search results to open yet")
                break
            browser = prev_app if prev_app in BROWSERS else None
            sh(*(["open", "-a", browser, results[n]["url"]] if browser else ["open", results[n]["url"]]))
            report.append(f"Opened {results[n]['title'][:60]}")
        elif tool == "system_control":
            setting = res["setting"]["choice"]
            if actions.SYSTEM[setting][1]:
                out["confirm"] = {"text": f"{setting.capitalize()}?",
                                  "yes": lambda: actions.system(setting), "no": lambda: "Cancelled"}
                return out
            report.append(actions.system(setting))
        elif tool == "terminal_command":
            return terminal_command(goal, out)
        elif tool == "run_saved_command":
            name = res.get("command", {}).get("choice")
            if name not in saved:
                report.append("No saved command matches; add it to commands.json")
                break
            report.append(actions.run_saved(name))
        if tool in ONE_STEP_TOOLS:
            break
        time.sleep(0.5)
    # Only an app the task really used carries over, not Clef's unused app pick.
    used = tool in {"open_app", "new_tab", "web_search", "open_in_workspace", "open_file_in_app", "open_website",
                    "copy_page_text", "click"}
    out["app"] = app if used else prev_app
    if out["file"] and out["file"] != prev_file:
        last["file"] = out["file"]
    return out


TERMINAL = actions.CONFIG.get("terminal", "Ghostty")
# Programs that only show things: these run straight away, anything else waits for a yes.
SAFE_PROGRAMS = {"tmux", "htop", "btop", "top", "lazygit", "nvim", "vim", "yazi", "claude", "ls", "git status", "git log",
                 "df -h", "uptime", "brew list"}


def terminal_command(goal, out):
    """Type the command the user named into a new terminal tab; run it at once if it is
    harmless, otherwise wait for a spoken yes before pressing Enter."""
    words = re.sub(r"^\W*(open|run|start|launch|type)\s+", "", goal, flags=re.I)
    words = re.sub(r"\s+(on|in|using)\s+(the\s+)?(ghostty|terminal)(\s+terminal)?.*$", "", words, flags=re.I)
    cmd = words.strip(" .,!?").lower() if words.strip().lower() in SAFE_PROGRAMS else words.strip(" .,!?")
    if not cmd or cmd.lower() in ("terminal", "ghostty", "a new tab", "new tab"):
        actions.activate(TERMINAL)
        out["report"].append(f"Opened {TERMINAL}")
        out["app"] = TERMINAL
        return out
    actions.activate(TERMINAL)
    actions.keys("cmd t")
    time.sleep(0.4)
    actions.type_text(cmd)
    out["app"] = TERMINAL
    if cmd.lower() in SAFE_PROGRAMS:
        actions.keys("return")
        out["report"].append(f"Ran {cmd} in {TERMINAL}")
        return out

    def yes():
        actions.activate(TERMINAL)
        actions.keys("return")
        return f"Ran {cmd}"

    def no():
        actions.activate(TERMINAL)
        actions.keys("ctrl u")   # clears the typed line in the shell
        return "Cancelled, the command was not run"
    out["confirm"] = {"text": f"Run \"{cmd}\" in {TERMINAL}?", "yes": yes, "no": no}
    return out


def send_whatsapp(goal, front, prev_file, out):
    """Open the contact's chat, put the file or message in, and wait for the user's yes."""
    text = re.sub(r"\s*\b(on|in|via|through|using)\s+whats ?app\b", "", goal, flags=re.I)
    m = re.search(r"\b(?:saying|that says|and say|telling (?:him|her|them))\s+(.+)$", text, re.I)
    message = m.group(1) if m else None
    if m:
        text = text[:m.start()]
    # "send hey, how far to Tunde": the words between "send" and "to" are the message,
    # unless they name a file ("send this document to ...").
    m2 = re.match(r"^\W*(?:send|message|text)\s+(.+?)\s+to\s+\S", text, re.I)
    if (not message and m2 and not WANTS_FILE.search(m2.group(1)) and not PRONOUN.fullmatch(m2.group(1).strip())
            and not re.fullmatch(r"(a |the )?(message|text|note|msg)", m2.group(1).strip(), re.I)):
        message = m2.group(1).strip(" ,")
        text = text[:m2.start(1)] + text[m2.end(1):]
    m = re.search(r"\bto\s+(?:my\s+)?(.+?)(?:'s)?(?:\s+(?:contact|chat|number))?\s*$", text, re.I)
    if not m:
        out["report"].append("Who should I send it to? Say \"send this to NAME on WhatsApp\"")
        return out
    contact = m.group(1).strip(" ,.")
    paths = []
    if WANTS_FILE.search(goal):
        paths = actions.this_file(front, prev_file or last["file"])
        if not paths:
            out["report"].append("Which file? Select it in Finder first")
            return out
    chats = actions.whatsapp_chats(contact)
    wanted = actions.WHATSAPP_ME if contact.lower() in actions.MYSELF else contact
    exact = [c for c in chats if c[0].lower() == wanted.lower()]
    if exact:
        name, el = exact[0]
    else:
        ranked = actions.rank(chats, re.findall(r"[a-z0-9']+", contact.lower()), n=8)
        if not ranked:
            out["report"].append(f"No WhatsApp chat found for {contact!r}")
            return out
        choice = choose(goal, f"Which WhatsApp chat is the contact {contact!r}?",
                        {**{f"c{k}": label for k, (label, _) in enumerate(ranked)}, "none": "none of these"})
        if not choice.startswith("c"):
            out["report"].append(f"No WhatsApp chat found for {contact!r}")
            return out
        name, el = ranked[int(choice[1:])]
    if not actions.whatsapp_open(name, el):
        out["report"].append(f"Couldn't open the WhatsApp chat with {name}")
        return out
    if not actions.whatsapp_attach(paths, message):
        out["report"].append(f"Opened {name}, but couldn't find the message box")
        return out
    what = ", ".join(os.path.basename(p) for p in paths) if paths else (f"\"{message[:40]}\"" if message else "")
    if not what:
        out["report"].append(f"Opened the WhatsApp chat with {name}")
        return out
    def send():
        actions.activate("WhatsApp")
        if not actions.whatsapp_press("WAMultiSendBottomBarView_SendButton"):
            actions.keys("return")       # a typed message has no send button to press
        return f"Sent {what} to {name}"

    def cancel():
        actions.activate("WhatsApp")
        if paths:
            if not actions.whatsapp_press("MultiSend_BackButton"):   # Escape does not close the preview
                actions.keys("escape")
        else:
            actions.keys("cmd a")        # clears the typed message
            actions.keys("delete")
        return "Cancelled, nothing sent"
    out["confirm"] = {"text": f"Send {what} to {name} on WhatsApp?", "yes": send, "no": cancel}
    out["app"] = "WhatsApp"
    return out


def do(command):
    """Split the command into tasks and run each in order.

    Stops at a task that needs a clarification or a yes before it acts (sending),
    and returns that with the tasks still to do.
    """
    tasks = split_tasks(command)
    if len(tasks) > 1:
        print(f"split into {len(tasks)} tasks: {tasks}")
    report, ask, confirm, ctx = [], None, None, {}
    for task in tasks:
        r = run(task, ctx)
        ctx = {"app": r["app"], "file": r["file"], "site": r["site"], "folder": r.get("folder")}
        report += r["report"]
        if r["ask"] or r["confirm"]:
            ask, confirm = r["ask"], r["confirm"]
            break
        time.sleep(0.8)  # let the app just opened settle before the next task
    return {"report": report, "ask": ask, "confirm": confirm, "tasks": tasks}


def vision_check():
    """Can Clef tell from a screenshot which app is in front? Ground truth from macOS."""
    time.sleep(2)  # let the app that was just opened come to the front first
    truth, apps = frontmost_app(), running_apps()
    shot = screenshot_b64()
    res = clef({"task": "Look at the screenshot of this Mac."},
               {"front": {"type": "choice", "instructions": "Which app's window is in front and in focus on screen?", "criteria": {a: a for a in apps}}},
               image=shot)["answers"]["front"]
    ok = res["choice"] == truth
    print(f"  vision: Clef says {res['choice']} ({res['probabilities'][res['choice']]:.0%}), macOS says {truth} {'OK' if ok else 'WRONG'}  [{timings[-1]:.0f} ms]")
    return ok


if __name__ == "__main__":
    for command in sys.argv[1:]:
        out = do(command)
        for line in out["report"]:
            print("  =>", line)
        if out["ask"]:
            print("  => needs clarification:", out["ask"])
    if timings:
        s = sorted(timings)
        print(f"decisions: {len(s)}  median {s[len(s) // 2]:.0f} ms  min {s[0]:.0f}  max {s[-1]:.0f}")
