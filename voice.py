"""Live voice front end for the Clef agent.

Silero VAD cuts the mic into spoken sentences; Parakeet (the model family Handy
uses) transcribes each one locally. A sentence that starts with "Morpheus"
(or a close spelling) wakes the agent: "Hey Morpheus, open Naval on VLC" is wake word and
command in one breath. Saying only the name plays a tink and the next sentence
is the command. The agent stays silent and shows a notification, except when
the command asks it to read or tell you something.

Usage:
  .venv/bin/python voice.py            listen and act, in this terminal
  .venv/bin/python voice.py --menubar  same, with a menu bar icon (how Morpheus.app runs it at login)
  .venv/bin/python voice.py --wake     only test the wake word (prints what it hears)
"""
import difflib
import os
import re
import signal
import subprocess
import sys
import threading
import time

import numpy as np
import sherpa_onnx
import sounddevice as sd

import actions
import memory
from settings import CONFIG, personal

HERE = os.path.dirname(os.path.abspath(__file__))
M = os.path.join(HERE, "models")
PARAKEET = os.path.join(M, "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
SR = 16000
BLOCK = 512                    # the frame size Silero VAD expects at 16 kHz
COMMAND_WAIT_S = 6             # after a bare "Morpheus", wait this long for the command
NAMES = ["morpheus", "morphius", "morpheous"]
# --names-test listens for every candidate at once and says which one it heard.
CANDIDATES = {"jarvis": ["jarvis"], "morpheus": ["morpheus", "morphius", "morpheous"],
              "nova": ["nova"], "atlas": ["atlas"], "orion": ["orion", "oreon"]}
JOINERS = {"is", "it", "as", "us", "so", "was", "has", "his", "its", "this", "says", "said"}
MIN_NAME_LEN = 5               # "Mira" alone must not wake it
SPEAK_WHEN = re.compile(r"^\W*(read|tell me|say|explain|what is|what's|who is)\b", re.I)


def keychain_token():
    """The Cloudflare token lives in the macOS Keychain, not in this folder."""
    if os.environ.get("CF_API_TOKEN"):
        return
    out = subprocess.run(["security", "find-generic-password", "-s", "clef-agent", "-a", "cf_api_token", "-w"],
                         capture_output=True, text=True)
    if out.returncode:
        sys.exit("No token: run  security add-generic-password -s clef-agent -a cf_api_token -w")
    os.environ["CF_API_TOKEN"] = out.stdout.strip()
    os.environ.setdefault("CF_ACCOUNT_ID", CONFIG["cloudflare_account_id"])


def sound(name):
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"])


def notify(text):
    safe = text.replace('"', "'")[:200]
    subprocess.Popen(["osascript", "-e", f'display notification "{safe}" with title "Morpheus"'])


def parakeet():
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=f"{PARAKEET}/encoder.int8.onnx", decoder=f"{PARAKEET}/decoder.int8.onnx",
        joiner=f"{PARAKEET}/joiner.int8.onnx", tokens=f"{PARAKEET}/tokens.txt",
        model_type="nemo_transducer", num_threads=2)


def voice_detector():
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = f"{M}/silero_vad.onnx"
    cfg.silero_vad.min_silence_duration = 0.4
    cfg.silero_vad.min_speech_duration = 0.2
    cfg.silero_vad.max_speech_duration = 15
    cfg.sample_rate = SR
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)


def transcribe(asr, audio):
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if 0 < peak < 0.5:  # a quiet voice: bring it up to normal level, at most 8x
        audio = audio * min(8.0, 0.5 / peak)
    st = asr.create_stream()
    st.accept_waveform(SR, audio)
    asr.decode_stream(st)
    return st.result.text.strip()


VOCAB_FILE = personal("vocab.txt")


def load_vocab():
    """vocab.txt: "Term: misheard, forms" per line -> [(term, [aliases])]."""
    out = []
    try:
        for line in open(VOCAB_FILE):
            line = line.split("#")[0].strip()
            if line:
                term, _, aliases = line.partition(":")
                out.append((term.strip(), [a.strip().lower() for a in aliases.split(",") if a.strip()]))
    except OSError:
        pass
    return out


def fix_words(text, vocab):
    """Put the user's own words back: listed mishearings exactly, and close misspellings
    of longer terms ("Hormozy" -> "Hormozi") by letter similarity."""
    for term, aliases in vocab:
        for a in aliases:
            text = re.sub(rf"\b{re.escape(a)}\b", term, text, flags=re.I)
    found = list(re.finditer(r"[A-Za-z']+", text))
    for term, _ in vocab:
        want = re.sub(r"[^a-z]", "", term.lower())
        if len(want) < 6:
            continue
        for n in (1, 2, 3):
            for i in range(len(found) - n + 1):
                span = found[i:i + n]
                got = "".join(m.group().lower() for m in span).replace("'", "")
                # Same length give or take, so "on YouTube" is not "fixed" to "YouTube".
                if (got != want and abs(len(got) - len(want)) <= max(1, len(want) // 6)
                        and difflib.SequenceMatcher(None, got, want).ratio() >= 0.85):
                    a, b = span[0].start(), span[-1].end()
                    return fix_words(text[:a] + term + text[b:], [(term, [])] + [v for v in vocab if v[0] != term])
    return text


SPEAKER_MODEL = f"{M}/nemo_en_titanet_small.onnx"
PROFILE = os.path.join(M, "voice-profile.npy")
HANDY = os.path.expanduser("~/Library/Application Support/com.pais.handy/recordings")
SAME_VOICE = 0.5         # live mic: the user scored 0.67-0.75; another person in the room 0.36; videos and TTS below 0.25
LEARN_ABOVE = 0.5        # only clearly-the-user sentences are added to the voice profile
MAX_SAMPLES = 60


class Voice:
    """Knows the user's voice, so conversation mode ignores a video, a call or
    someone else in the room. The profile starts from the Handy recordings and
    keeps learning from commands that clearly came from the user."""
    def __init__(self):
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=SPEAKER_MODEL, num_threads=2)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.samples = list(np.load(PROFILE)) if os.path.exists(PROFILE) else self.from_handy()
        self.ref = self._mean()

    def embed(self, audio):
        st = self.ex.create_stream()
        st.accept_waveform(SR, audio)
        st.input_finished()
        v = np.array(self.ex.compute(st), dtype=np.float32)
        return v / np.linalg.norm(v)

    def from_handy(self):
        out = []
        for name in sorted(os.listdir(HANDY)) if os.path.isdir(HANDY) else []:
            if name.endswith(".wav"):
                raw = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.join(HANDY, name), "-ac", "1", "-ar", str(SR),
                                      "-f", "f32le", "-"], capture_output=True).stdout
                audio = np.frombuffer(raw, dtype=np.float32)
                if len(audio) > SR * 2:
                    out.append(self.embed(audio))
        print(f"Voice profile made from {len(out)} Handy recordings")
        if out:
            np.save(PROFILE, np.array(out))
        return out

    def _mean(self):
        if not self.samples:
            return None
        m = np.mean(self.samples, axis=0)
        return m / np.linalg.norm(m)

    def score(self, audio):
        """(similarity to the user's voice, embedding); 1.0 when there is no profile yet."""
        if len(audio) < SR * 0.6:   # too short to tell
            return 1.0, None
        e = self.embed(audio)
        if self.ref is None:        # no profile yet: accept, and learn from it
            return 1.0, e
        return float(e @ self.ref), e

    def learn(self, emb, score):
        if emb is None or score < LEARN_ABOVE:
            return
        self.samples = (self.samples + [emb])[-MAX_SAMPLES:]
        self.ref = self._mean()
        np.save(PROFILE, np.array(self.samples))


# Conversation mode: after "Morpheus, stay with me", every sentence in the user's
# voice is a command, no name needed, until they say they're done or go quiet.
SESSION_START = re.compile(r"\b(stay with me|keep listening|keep talking|let'?s talk|conversation mode|stay (on|awake|here)"
                           r"|i want to keep talking( to you)?|don'?t stop listening|listen to me)\b[,.!]?\s*", re.I)
# Searched anywhere in a short sentence: "Okay, that's all Morpheus, you can end it" must end it.
SESSION_END = re.compile(r"\b(that'?s (all|it)( for now)?|stop listening|go to sleep|good ?bye|bye bye|thanks?( you)?,? morpheus|"
                         r"end (it|this|(the )?(session|conversation))|you can (rest|go|sleep|stop|end it)|we'?re done|i'?m done)\b", re.I)
END_MAX_WORDS = 14
SESSION_IDLE_S = 180
FOLLOW_UP_S = 8          # after any command, a reply in the user's voice needs no name either


def split_wake(text):
    """(woke, command): the name may be one to three words, as the first or second word
    ("Morpheus, ..." or "Hey Morpheus, ..."); later in a sentence it is just talk.

    A name split into several words ("who may raise") must match more closely
    than a single word, so "who may I say is calling" does not wake it.
    """
    found = list(re.finditer(r"[a-z0-9']+", text.lower()))  # keep numbers: "workspace 2"
    words = [m.group() for m in found]
    best = (0.0, 0, 0)
    for i in range(min(2, len(words))):
        for j in (1, 2, 3):
            cand = "".join(words[i:i + j])
            # Every form of the name has its "r": a near-miss without it is not the name.
            if len(cand) < MIN_NAME_LEN or i + j > len(words) or "r" not in cand:
                continue
            if cand[0] != NAMES[0][0]:  # "Orpheus" is not "Morpheus"
                continue
            if j > 1 and words[i + j - 1] in JOINERS:  # "Morphy is" is talk about a name, not the name
                continue
            score = max(difflib.SequenceMatcher(None, cand, n).ratio() for n in NAMES)
            need = 0.8 if j == 1 else 0.85
            if score >= need and (score, j) > (best[0], best[2]):
                best = (score, i, j)
    if not best[0]:
        return False, ""
    # The command is the original text after the name, punctuation kept: a full
    # stop between two sentences is how agent.split_tasks sees two tasks.
    end = found[best[1] + best[2] - 1].end()
    return True, text[end:].strip(" ,.!?;:")


class Mouth:
    """Kokoro, loaded only the first time something has to be spoken. Speaks in the
    background so listening carries on; the voice check ignores Morpheus's own voice."""
    def __init__(self, status=None):
        self.tts = None
        self.lock = threading.Lock()
        self.status = status if status is not None else {}

    def say(self, text):
        threading.Thread(target=self._say, args=(text,), daemon=True).start()

    def _say(self, text):
        import soundfile as sf
        from kokoro_onnx import Kokoro
        voice = CONFIG.get("speaking_voice", "af_bella")
        with self.lock:   # one sentence at a time
            if self.tts is None:
                self.tts = Kokoro(f"{M}/kokoro-v1.0.fp16.onnx", f"{M}/voices-v1.0.bin")
            # Kokoro voice names start with a for American, b for British English.
            samples, rate = self.tts.create(text[:600], voice=voice, speed=1.1, lang="en-gb" if voice[0] == "b" else "en-us")
            path = "/tmp/morpheus-say.wav"
            sf.write(path, samples, rate)
            # The listener closes the mic while this plays, so Morpheus never hears
            # itself and an answer right after the question starts a clean sentence.
            self.status["speaking"] = True
            try:
                subprocess.run(["afplay", path])
            finally:
                self.status["speaking"] = False


PAD = int(SR * 0.3)            # the VAD clips the first syllable; add audio back around each sentence
KEEP = SR * 30


def sentences(mic, vad, skip, status):
    """Yield each spoken sentence from the mic as float32 audio, padded on both sides.

    Set skip["now"] to drop what the mic caught while the agent was busy. The VAD
    is never reset: that restarts its sample count and the padded slices below
    would come out empty, so nothing could wake the agent again.
    Returns when status["paused"] is set, so the caller can close the mic.
    """
    buf, offset = np.zeros(0, dtype=np.float32), 0   # offset = absolute index of buf[0]
    pending, loudest, started = [], 0.0, time.time()
    while not status["paused"] and not status.get("speaking"):
        if skip.pop("now", False):
            backlog = mic.read_available
            if backlog:
                mic.read(backlog)  # not fed to the VAD or buf, so their counts stay in step
            pending.clear()
        block, _ = mic.read(BLOCK)
        block = block[:, 0].astype(np.float32)
        if loudest == 0.0:
            loudest = float(np.abs(block).max())
            # macOS hands a blocked mic pure silence instead of an error.
            if loudest == 0.0 and time.time() - started > 5 and status["state"] != "no mic":
                status["state"] = "no mic"
                print("Microphone gives only silence: allow Morpheus in System Settings > Privacy & Security > Microphone")
                notify("Microphone is blocked. Allow Morpheus in Privacy & Security > Microphone.")
            elif loudest:
                status["state"] = "listening"
        buf = np.concatenate([buf, block])
        if len(buf) > KEEP:
            offset += len(buf) - KEEP
            buf = buf[-KEEP:]
        vad.accept_waveform(block)
        while not vad.empty():
            pending.append((vad.front.start, len(vad.front.samples)))
            vad.pop()
        end_seen = offset + len(buf)
        while pending and pending[0][0] + pending[0][1] + PAD <= end_seen:
            start, n = pending.pop(0)
            lo, hi = max(0, start - PAD - offset), max(0, start + n + PAD - offset)
            yield buf[lo:hi]


YES = re.compile(r"^\W*(yes|yeah|yep|yup|yah|ya|sure|send( it)?|do it|go ahead|open it|confirm(ed)?|okay|ok|correct|right)\b", re.I)
CONFIRM_WAIT_S = 20            # counted from the question, which takes a few seconds to say
YES_VOICE = 0.3                # a short "yes" scores lower than a sentence; other voices stay under 0.25


def act(agent, mouth, command, status, quiet_unclear=False):
    """Run the command. Returns ("ask", None) when Clef wants the next sentence as a
    clarification, ("confirm", action) when an action waits for a yes, else (None, None)."""
    known = memory.alias(command)
    if known:   # a phrasing you confirmed before ("Did you mean ...?" "yes")
        print(f"  {command!r} is what you confirmed as {known!r}")
        command = known
    print(f"  command: {command!r}")
    status["state"] = "working"
    try:
        out = agent.do(command)
    except Exception as e:  # keep listening whatever one command does
        print("  error:", e)
        sound("Basso")
        notify(f"Couldn't do that: {e}")
        status["last"] = f"{command[:40]}: error"
        return None, None
    done = " ".join(out["report"])
    status["last"] = f"{command[:40]}: {done[:60] or 'asked'}"
    if agent.last.get("results") is not status.get("results"):
        status["results"] = agent.last["results"]   # shown under "Last web search" in the menu bar
    if out["confirm"]:
        print(f"  => waiting for yes: {out['confirm']['text']!r}")
        sound("Submarine")
        notify(f"{out['confirm']['text']} Say yes to go ahead, anything else cancels.")
        mouth.say(out["confirm"]["text"] + " Say yes.")
        return "confirm", out["confirm"]
    if out["ask"] and quiet_unclear and not done:
        print(f"  => not a command, ignored: {out['ask']!r}")
        return None, None
    if out["ask"]:
        return unclear(agent, mouth, out["ask"], status)
    memory.worked(command)
    if out.get("say"):
        mouth.say(out["say"])
    summary = done or "Done."
    print("  =>", summary)
    sound("Glass")
    notify(summary)
    if SPEAK_WHEN.match(command) and not out.get("say"):
        mouth.say(summary)
    return None, None


QUESTION = re.compile(r"^\W*(what|who|whom|whose|where|when|why|how|which|is|are|was|were|can|could|does|do|did|"
                      r"define|meaning|explain|tell me)\b", re.I)
STOPWORDS = {"the", "a", "an", "to", "of", "on", "in", "for", "me", "my", "i", "you", "it", "is", "and", "so", "like",
             "this", "that", "with", "can", "please", "just", "um", "uh", "okay", "yeah"}


def unclear(agent, mouth, command, status):
    """Clef couldn't map what was said to an action. Try, in order: a command you use
    that sounds like it; a file or folder with that name; a web search, read out loud;
    and if it was too garbled for any of those, say what was heard."""
    # 1. A command you have used before, or a common one, that sounds like it.
    guess = memory.did_you_mean(command)
    if guess:
        print(f"  => unclear, did you mean {guess!r}?")

        def yes():
            out = agent.do(guess)
            memory.learn(command, guess)
            return " ".join(out["report"]) or "Done."
        sound("Submarine")
        notify(f"Did you mean: \"{guess}\"? Say yes.")
        mouth.say(f"Did you mean, {guess}?")
        return "confirm", {"text": guess, "yes": yes, "no": lambda: "Okay, never mind."}
    # 2. A folder or file on this Mac with that name.
    words = [w for w in agent.name_words(command) if w not in STOPWORDS]
    if 1 <= len(words) <= 4:
        hits = [(n, p) for n, p in actions.spotlight(words)
                if all(w in n.lower() for w in words)]
        if hits:
            name, path = min(hits, key=lambda h: len(h[1]))   # the shallowest match is usually the one
            kind = "folder" if os.path.isdir(path) else "file"
            print(f"  => unclear, open the {kind} {path!r}?")
            sound("Submarine")
            notify(f"Open the {kind} \"{name}\"? Say yes.")
            mouth.say(f"Should I open the {kind} {name}?")

            def open_it():
                subprocess.run(["open", path])
                return f"Opened {name}"
            return "confirm", {"text": name, "yes": open_it, "no": lambda: "Okay, never mind."}
    # 3. Search the web without opening a browser and read out the top answer.
    meaningful = [w for w in re.findall(r"[a-z0-9']+", command.lower()) if w not in STOPWORDS]
    if QUESTION.match(command) or len(meaningful) >= 3:
        try:
            results = actions.web_results(command)
        except Exception as e:
            results = []
            print("  web search failed:", e)
        if results:
            agent.last["results"] = results
            status["results"] = results
            memory.log_search(command, results)
            top = results[0]
            first = agent.written_answer(command, results) or re.split(r"(?<=[.!?])\s", top["text"].strip())[0][:220]
            print(f"  => searched the web: {top['title']!r}")
            sound("Glass")
            notify(f"{top['title']}: {first}  (say \"open the first one\")")
            mouth.say(f"{first or top['title']}")
            status["last"] = f"searched: {command[:50]}"
            return None, None
    # 4. Too garbled: say what was heard, so you know whether to speak up or rephrase.
    print(f"  => unclear: {command!r}  (say it again another way)")
    sound("Purr")
    notify(f"I heard \"{command}\". Say it another way, no need to say Morpheus.")
    mouth.say(f"I heard, {command}. Say it again?")
    return "ask", None


def toggle_pause(status):
    """The kill switch: stop listening and stop whatever is running (downloads,
    video shrinking, speech, recording); a second press resumes."""
    status["paused"] = not status["paused"]
    if status["paused"]:
        subprocess.run(["pkill", "-TERM", "-P", str(os.getpid())])
        status["state"] = "paused"
        print("Paused.")
        notify("Paused. Nothing is listening.")
    else:
        print("Resumed.")
        notify("Listening again.")


def listen(status, mode):
    """Hear sentences and act on them, until stopped; reopens the mic after a pause."""
    agent = None
    if mode == "run":
        keychain_token()
        import agent
    asr, mouth, vocab = parakeet(), Mouth(status), load_vocab()
    voice = Voice() if mode == "run" else None
    waiting_until, confirm, session_until = 0.0, None, 0.0
    print('Listening. Say "Hey Morpheus, ..." (Ctrl+C to stop)')
    while True:
        if status["paused"] or status.get("speaking"):
            time.sleep(0.1)
            continue
        vad, skip = voice_detector(), {}
        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK) as mic:
            status["state"] = "listening"
            for audio in sentences(mic, vad, skip, status):
                t = time.time()
                text = transcribe(asr, audio)
                if not text:
                    continue
                stamp = time.strftime("%H:%M:%S")
                woke, command = split_wake(text)
                if mode == "names":
                    words = re.findall(r"[a-z']+", text.lower())
                    got = [name for name, forms in CANDIDATES.items()
                           if any(difflib.SequenceMatcher(None, w, f).ratio() >= 0.8 for w in words for f in forms)]
                    print(f"[{stamp}] {('HEARD ' + ', '.join(got)) if got else '      ':16} ({(time.time() - t) * 1000:.0f} ms): {text!r}")
                    continue
                if mode == "wake":
                    print(f"[{stamp}] {'WAKE' if woke else '    '} heard ({(time.time() - t) * 1000:.0f} ms): {text!r}")
                    if woke:
                        sound("Tink")
                    continue
                text = fix_words(text, vocab)
                woke, command = split_wake(text)
                in_session = time.time() < session_until
                if status.get("session") and not in_session:
                    status["session"] = False
                    print(f"[{stamp}] conversation ended: quiet for {SESSION_IDLE_S // 60} minutes")
                    notify("Conversation ended after a quiet spell. Say \"Hey Morpheus\" next time.")
                if in_session or confirm or (not woke and time.time() < waiting_until):
                    # No name was said: act only on the user's own voice.
                    similar, emb = voice.score(audio)
                    if similar < (YES_VOICE if confirm else SAME_VOICE) and not woke:
                        print(f"[{stamp}] other voice ({similar:.2f}), ignored: {text!r}")
                        continue
                if in_session and len(text.split()) <= END_MAX_WORDS and SESSION_END.search(text):
                    session_until, waiting_until = 0.0, 0.0
                    status["session"] = False
                    print(f"[{stamp}] conversation ended: {text!r}")
                    sound("Bottle")
                    notify("Conversation ended. Say \"Hey Morpheus\" next time.")
                    continue
                if confirm:
                    # The sentence after "Send X to Y? Say yes" answers it, with or without the name.
                    # Only its last sentence counts: "Open it? ... Yes, open it." is a yes.
                    action, confirm = confirm, None
                    answer = re.split(r"(?<=[.!?])\s+", (command if woke and command else text).strip())[-1]
                    said_yes = time.time() < action["until"] and YES.match(answer)
                    line = action["yes"]() if said_yes else action["no"]()
                    sound("Glass" if said_yes else "Basso")
                    print(f"[{stamp}] heard {text!r} => {line}")
                    notify(line)
                    status["last"] = line[:80]
                    if said_yes or not (woke and len(command.split()) >= 2):
                        skip["now"] = True
                        continue
                    # Not a yes but a new "Hey Morpheus, ..." command: cancel, then do the new one.
                if not woke and (time.time() < waiting_until or in_session):
                    woke, command = True, text   # the sentence after a bare "Morpheus", or in a conversation
                if not woke:
                    continue
                print(f"[{stamp}] heard: {text!r}")
                if voice and not in_session:
                    similar, emb = voice.score(audio)
                    print(f"  voice match {similar:.2f}")
                    voice.learn(emb, similar)
                if not in_session and len(command.split()) <= END_MAX_WORDS and SESSION_END.search(command):
                    print("  => nothing to end")
                    sound("Bottle")
                    continue
                if SESSION_START.search(command):
                    command = SESSION_START.sub("", command).strip(" ,.!?")
                    session_until = time.time() + SESSION_IDLE_S
                    status["session"] = True
                    print(f"[{stamp}] conversation mode on")
                    sound("Hero")
                    notify("I'm listening. Talk without saying Morpheus; say \"that's all\" when done.")
                    if len(command.split()) < 2:
                        continue
                if in_session:
                    session_until = time.time() + SESSION_IDLE_S
                if len(command.split()) < 2:  # just the name (or one stray word): wait for the command
                    sound("Tink")
                    waiting_until = time.time() + COMMAND_WAIT_S
                    continue
                waiting_until = 0.0
                kind, payload = act(agent, mouth, command, status, quiet_unclear=in_session)
                waiting_until = time.time() + FOLLOW_UP_S
                if kind == "ask":
                    waiting_until = time.time() + COMMAND_WAIT_S  # the next sentence is the rephrased command
                elif kind == "confirm":
                    confirm = {**payload, "until": time.time() + CONFIRM_WAIT_S}
                if not status["paused"]:
                    status["state"] = "listening"
                skip["now"] = True  # drop anything the mic caught while the agent worked
                print('Listening again.')
        if confirm and status["paused"]:  # paused while a send waited for its yes: cancel it
            confirm["no"]()                # (the mic also closes while Morpheus speaks: that keeps it)
            confirm = None


LOG = os.path.expanduser("~/Library/Logs/Morpheus.log")
HOTKEY = "Ctrl+Option+Cmd+M"


def menubar(status):
    """A menu bar icon showing what Morpheus is doing, with pause, log, restart and quit,
    and the global kill-switch hotkey. Runs the AppKit loop on the main thread."""
    import ApplicationServices
    import Quartz
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSEvent, NSEventMaskKeyDown,
                        NSImage, NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength)
    from Foundation import NSObject, NSTimer

    # Ask once for what the actions need; macOS shows its own prompts.
    ApplicationServices.AXIsProcessTrustedWithOptions({ApplicationServices.kAXTrustedCheckOptionPrompt: True})
    if hasattr(Quartz, "CGPreflightScreenCaptureAccess") and not Quartz.CGPreflightScreenCaptureAccess():
        Quartz.CGRequestScreenCaptureAccess()

    icons = {"starting": "ellipsis.circle", "listening": "waveform", "working": "bolt.circle",
             "paused": "pause.circle", "no mic": "mic.slash",
             "conversation": "bubble.left.and.bubble.right"}

    class Controller(NSObject):
        def setup(self):
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            menu = NSMenu.alloc().init()
            self.info = menu.addItemWithTitle_action_keyEquivalent_("Starting...", None, "")
            self.last = menu.addItemWithTitle_action_keyEquivalent_("", None, "")
            menu.addItem_(NSMenuItem.separatorItem())
            self.pause = menu.addItemWithTitle_action_keyEquivalent_(f"Pause  ({HOTKEY})", "toggle:", "")
            for title, sel in (("Open log", "openLog:"), ("Restart", "restart:"), ("Quit Morpheus", "quit:")):
                menu.addItemWithTitle_action_keyEquivalent_(title, sel, "").setTarget_(self)
            self.pause.setTarget_(self)
            self.results = NSMenu.alloc().init()
            found = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Last web search", None, "")
            found.setSubmenu_(self.results)
            menu.insertItem_atIndex_(found, 2)
            self.shown_results = None
            self.item.setMenu_(menu)
            self.shown = None
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.3, self, "tick:", None, True)

        def tick_(self, timer):
            state = "paused" if status["paused"] else status["state"]
            if state == "listening" and status.get("session"):
                state = "conversation"
            if state != self.shown:
                self.shown = state
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(icons.get(state, "waveform"), state)
                img.setTemplate_(True)
                self.item.button().setImage_(img)
                self.info.setTitle_(f"Morpheus: {state}")
                self.pause.setTitle_(f"{'Resume' if status['paused'] else 'Pause'}  ({HOTKEY})")
            self.last.setTitle_(f"Last: {status['last']}" if status["last"] else "Say \"Hey Morpheus, ...\"")
            results = status.get("results")
            if results is not self.shown_results:
                self.shown_results = results
                self.results.removeAllItems()
                for r in results or []:
                    it = self.results.addItemWithTitle_action_keyEquivalent_(r["title"][:70] or r["url"], "openResult:", "")
                    it.setRepresentedObject_(r["url"])
                    it.setTarget_(self)
                more = self.results.addItemWithTitle_action_keyEquivalent_("All searches...", "openSearches:", "")
                more.setTarget_(self)

        def openResult_(self, sender):
            subprocess.Popen(["open", sender.representedObject()])

        def openSearches_(self, sender):
            if os.path.exists(memory.SEARCHES):
                subprocess.Popen(["open", "-e", memory.SEARCHES])

        def toggle_(self, sender):
            toggle_pause(status)

        def openLog_(self, sender):
            subprocess.Popen(["open", "-a", "Console", LOG])

        def restart_(self, sender):
            os._exit(1)   # launchd starts it again; exit 0 would mean "stay quit"

        def quit_(self, sender):
            subprocess.run(["pkill", "-TERM", "-P", str(os.getpid())])
            os._exit(0)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ctl = Controller.alloc().init()
    ctl.setup()
    want = (1 << 18) | (1 << 19) | (1 << 20)   # Control, Option, Command

    def on_key(event):
        if event.keyCode() == 46 and event.modifierFlags() & want == want:   # 46 = M
            toggle_pause(status)
    NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, on_key)
    app.run()


def main():
    sys.stdout.reconfigure(line_buffering=True)
    mode = "wake" if "--wake" in sys.argv else "names" if "--names-test" in sys.argv else "run"
    status = {"state": "starting", "paused": False, "last": ""}
    signal.signal(signal.SIGUSR1, lambda *_: toggle_pause(status))   # `morpheus pause`
    if "--menubar" in sys.argv:
        def guarded():
            try:
                listen(status, mode)
            except Exception as e:   # let launchd restart it rather than sit there deaf
                print("listener crashed:", repr(e))
                os._exit(1)
        threading.Thread(target=guarded, daemon=True).start()
        menubar(status)
    else:
        listen(status, mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
