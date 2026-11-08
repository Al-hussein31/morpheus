"""What Morpheus remembers between runs, in ~/Library/Application Support/Morpheus:

history.json   commands that worked, so a misheard one can be matched to them
               ("Did you mean: open YouTube?") and phrasings you confirmed with
               "yes" run straight away next time.
searches.md    every web search it did for you, with the links.
"""
import difflib
import json
import os
import re
import time

DIR = os.path.expanduser("~/Library/Application Support/Morpheus")
HISTORY = os.path.join(DIR, "history.json")
SEARCHES = os.path.join(DIR, "searches.md")
MAX_COMMANDS = 300
# Seed suggestions before there is any history: the commands people use most.
EXAMPLES = ["what apps are open", "open YouTube", "open Zen", "open VLC", "take a screenshot", "start recording my screen",
            "stop recording", "turn the volume down", "turn the volume up", "brightness down", "brightness up",
            "lock the screen", "open the Downloads folder", "open the Desktop folder", "scroll down", "go back",
            "copy the text on this page", "new tab", "play or pause the music", "next track"]


def norm(text):
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _load():
    try:
        with open(HISTORY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"commands": {}, "aliases": {}}


def _save(data):
    os.makedirs(DIR, exist_ok=True)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, HISTORY)


def worked(command):
    """Count a command that ran without trouble."""
    data, key = _load(), norm(command)
    if not key:
        return
    data["commands"][key] = data["commands"].get(key, 0) + 1
    if len(data["commands"]) > MAX_COMMANDS:   # forget the least used
        data["commands"] = dict(sorted(data["commands"].items(), key=lambda kv: -kv[1])[:MAX_COMMANDS])
    _save(data)


def learn(heard, meant):
    """After "Did you mean X?" ... "yes": next time, what was heard runs X directly."""
    data = _load()
    data["aliases"][norm(heard)] = meant
    _save(data)


def alias(heard):
    """The command a confirmed mishearing stands for, or None."""
    aliases = _load()["aliases"]
    key = norm(heard)
    if key in aliases:
        return aliases[key]
    close = difflib.get_close_matches(key, list(aliases), n=1, cutoff=0.9)
    return aliases[close[0]] if close else None


def did_you_mean(heard, cutoff=0.72):
    """The known command closest to what was heard, or None. Commands you use often win ties."""
    data, key = _load(), norm(heard)
    known = dict.fromkeys(EXAMPLES, 0)
    known.update(data["commands"])
    best, best_score = None, cutoff
    for cmd, uses in known.items():
        if cmd == key:
            continue
        score = difflib.SequenceMatcher(None, key, norm(cmd)).ratio() + min(uses, 20) * 0.002
        if score > best_score:
            best, best_score = cmd, score
    return best


def log_search(query, results):
    os.makedirs(DIR, exist_ok=True)
    with open(SEARCHES, "a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}  {query}\n\n")
        for i, r in enumerate(results, 1):
            f.write(f"{i}. [{r['title']}]({r['url']})\n   {r['text'][:200]}\n")
