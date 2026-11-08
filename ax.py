"""Read and press what is on screen through the macOS accessibility tree.

Every app exposes its buttons, links, text fields and list rows to the
accessibility API (the one VoiceOver uses), with their labels. Morpheus lists
the labelled ones in the front window, Clef picks the one the user named, and
this module presses it: no screenshots and no guessing at pixels.

Needs Accessibility permission for the app Morpheus runs in.
"""
import threading
import time

import ApplicationServices as AX
import Quartz
from AppKit import NSRunningApplication, NSWorkspace
from Foundation import NSDate, NSRunLoop

CLICKABLE = {"AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
             "AXTab", "AXTextField", "AXSearchField", "AXTextArea", "AXComboBox", "AXCell", "AXRow", "AXImage",
             "AXStaticText", "AXDisclosureTriangle", "AXSwitch"}
SKIP_CHILDREN_OF = {"AXMenuBar"}
MAX_NODES = 3000
MAX_SECONDS = 2.5


def _get(el, attr):
    err, val = AX.AXUIElementCopyAttributeValue(el, attr, None)
    return val if err == 0 else None


def _label(el):
    for attr in ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp"):
        v = _get(el, attr)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:120]
    return ""


def _pump():
    """NSWorkspace only learns about app changes on the main run loop: without one
    (agent.py from a terminal) its answers stay stuck at start-up, so run it briefly."""
    if threading.current_thread() is threading.main_thread():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))


def front_pid():
    """The app in front now, asked of the accessibility system, which is always current."""
    app = _get(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication")
    if app is not None:
        err, pid = AX.AXUIElementGetPid(app, None)
        if err == 0:
            return pid
    _pump()
    return NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()


def front_name():
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(front_pid())
    return (app.localizedName() or "").replace("‎", "") if app else ""


def pid_of(app_name):
    _pump()
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if (a.localizedName() or "").replace("\u200e", "") == app_name:  # WhatsApp hides a mark in its name
            return a.processIdentifier()
    return None


def elements(pid=None):
    """Labelled, pressable things in the app's focused window: [(label, role, element)].

    Breadth first, so the toolbar and the main list come before deep page content;
    stops after MAX_NODES nodes or MAX_SECONDS, whichever comes first.
    """
    app = AX.AXUIElementCreateApplication(pid or front_pid())
    root = _get(app, "AXFocusedWindow") or _get(app, "AXMainWindow")
    if root is None:
        return []
    out, queue, seen, t = [], [root], 0, time.time()
    while queue and seen < MAX_NODES and time.time() - t < MAX_SECONDS:
        el = queue.pop(0)
        seen += 1
        role = _get(el, "AXRole") or ""
        if role in CLICKABLE:
            label = _label(el)
            if label:
                out.append((label, role, el))
        if role not in SKIP_CHILDREN_OF:
            queue.extend(_get(el, "AXChildren") or [])
    return out


def _center(el):
    pos, size = _get(el, "AXPosition"), _get(el, "AXSize")
    if pos is None or size is None:
        return None
    _, p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
    _, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    return p.x + s.width / 2, p.y + s.height / 2


def click_at(x, y):
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.05)


def press(el):
    """Press the element the way the app expects: its own action first, a real click if it has none."""
    role = _get(el, "AXRole") or ""
    if role in ("AXTextField", "AXSearchField", "AXTextArea", "AXComboBox"):
        if AX.AXUIElementSetAttributeValue(el, "AXFocused", True) == 0:
            return True
    err, actions = AX.AXUIElementCopyActionNames(el, None)
    if err == 0 and actions and "AXPress" in actions and AX.AXUIElementPerformAction(el, "AXPress") == 0:
        return True
    c = _center(el)
    if c is None:
        return False
    click_at(*c)
    return True


def find(pid, identifier):
    """The first element with this accessibility identifier, e.g. WhatsApp's "ChatBar_ComposerTextView"."""
    app = AX.AXUIElementCreateApplication(pid)
    queue, n = [_get(app, "AXFocusedWindow") or _get(app, "AXMainWindow")], 0
    while queue and queue[0] is not None and n < MAX_NODES:
        el = queue.pop(0)
        n += 1
        if _get(el, "AXIdentifier") == identifier:
            return el
        queue.extend(_get(el, "AXChildren") or [])
    return None
