"""Personal settings, kept out of git.

config.json (yours, ignored by git) is read first; config.example.json is the
template it is copied from. vocab.txt and commands.json work the same way, with
.example versions in git.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def personal(name):
    """Path of a personal file, or of its committed example when it doesn't exist yet."""
    path = os.path.join(HERE, name)
    if os.path.exists(path):
        return path
    stem, ext = os.path.splitext(name)
    return os.path.join(HERE, f"{stem}.example{ext}")


with open(personal("config.json")) as f:
    CONFIG = json.load(f)
