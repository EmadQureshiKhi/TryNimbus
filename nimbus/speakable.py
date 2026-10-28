"""Turn on-screen text into text a voice reads naturally.

Captions keep the original wording; only what is spoken goes through `speakable()`. The rules follow
common text-to-speech normalisation practice: spell out symbols and operators, read URLs, paths and
file names the way a person would, expand abbreviations, say acronyms as letters only when people do,
drop markdown and emoji, and use punctuation (not symbols) for pauses.
"""
from __future__ import annotations

import re

# Acronyms the system voice gets wrong as written. Anything not listed is left to the voice, which
# already reads most capitals as letters ("PDF") and pronounceable ones as words ("NASA").
LEXICON = {
    "URL": "U R L", "URLs": "U R Ls", "UI": "U I", "API": "A P I", "APIs": "A P Is", "CLI": "C L I",
    "GUI": "gooey", "PR": "P R", "PRs": "P Rs", "AI": "A I", "LLM": "L L M", "GPT": "G P T",
    "ChatGPT": "Chat G P T", "iOS": "i O S", "macOS": "mac O S", "SQL": "sequel", "JSON": "jay son",
    "YAML": "yammel", "SAM": "Sam", "CSV": "C S V", "HTML": "H T M L", "CSS": "C S S", "JS": "J S",
    "npm": "N P M", "OK": "okay", "Ctrl": "control", "Cmd": "command", "Opt": "option", "Esc": "escape",
    "FAQ": "F A Q", "PDF": "P D F", "USB": "U S B", "GPU": "G P U", "CPU": "C P U", "RAM": "ram",
    "Wi-Fi": "wifi", "vs": "versus", "e.g.": "for example", "i.e.": "that is", "etc.": "et cetera",
    "approx.": "about", "w/": "with", "w/o": "without",
}
FRACTIONS = {"1/2": "one half", "1/3": "one third", "2/3": "two thirds", "1/4": "one quarter",
             "3/4": "three quarters", "1/5": "one fifth", "1/10": "one tenth"}
TLDS = "com|org|net|io|ai|dev|co|uk|us|edu|gov|me|tv|so|xyz"
KEYS = {"⌘": "command", "⌥": "option", "⌃": "control", "⇧": "shift", "↩": "return", "⏎": "return",
        "⌫": "delete", "⎋": "escape", "⇥": "tab"}
KEY_NAMES = r"(?:cmd|command|ctrl|control|opt|option|alt|shift|fn|space|return|enter|tab|esc|escape|delete|[a-z0-9])"
SYMBOLS = [
    ("≠", " does not equal "), ("≤", " is at most "), ("≥", " is at least "), ("≈", " is about "),
    ("±", " plus or minus "), ("×", " times "), ("÷", " divided by "), ("√", " the square root of "),
    ("→", ", so "), ("⇒", ", so "), ("->", ", so "), ("=>", ", so "), ("…", "..."), ("&", " and "),
    ("π", " pi "), ("°", " degrees "), ("∞", " infinity "),
]
SUPERSCRIPTS = {"²": " squared", "³": " cubed", "⁴": " to the fourth", "⁵": " to the fifth", "ⁿ": " to the n"}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
# An operand in math: a number (optionally with a variable, 2x), a single-letter variable, or a bracket.
_OPERAND_L = r"(?:\b\d+(?:\.\d+)?[a-zA-Z]?|\b[a-zA-Z]\b|[)\]²³])"
_OPERAND_R = r"(?:\d|\b[a-zA-Z]\b|[(\[√])"


def speakable(text: str) -> str:
    t = text
    t = _EMOJI.sub("", t)
    t = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", t)  # inline code: keep the words
    t = re.sub(r"(\*\*|__|\*|~~)(.+?)\1", r"\2", t)  # bold, italics, strike
    t = re.sub(r"^\s{0,3}(#{1,6}|>|[-*•]|\d+[.)])\s+", "", t, flags=re.M)  # headings, quotes, bullets
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # markdown links: the label only

    t = re.sub(r"\bhttps?://(?:www\.)?([^\s/]+)\S*", lambda m: _dotted(m.group(1)), t)  # URL -> its site
    t = re.sub(r"\bwww\.([^\s/]+)", lambda m: _dotted(m.group(1)), t)
    t = re.sub(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b", lambda m: f"{_dotted(m.group(1))} at {_dotted(m.group(2))}", t)
    t = re.sub(r"(?:~|\.{1,2})?(?:/[\w.@-]+){2,}/?", lambda m: _path(m.group(0)), t)  # /a/b/c.py -> c dot py

    for sym, word in KEYS.items():
        t = t.replace(sym, f" {word} ")
    # Shortcuts: "Ctrl+Opt+Space" -> "control option space" (before + becomes "plus").
    t = re.sub(rf"\b{KEY_NAMES}(?:\s*\+\s*{KEY_NAMES})+\b",
               lambda m: " ".join(LEXICON.get(k.strip().capitalize(), k.strip()) for k in m.group(0).split("+")),
               t, flags=re.I)

    t = re.sub(r"\^\s*2\b", "²", t)
    t = re.sub(r"\^\s*3\b", "³", t)
    t = re.sub(r"\^\s*\(?([\w.]+)\)?", r" to the power of \1", t)
    t = re.sub(r"\(([^()]+)\)\s*\(([^()]+)\)", r"\1, times \2,", t)  # (x - 2)(x - 3)
    t = re.sub(r"\b(\d+)\s*/\s*(\d+)\b", lambda m: FRACTIONS.get(m.group(0).replace(" ", ""),
                                                                 f"{m.group(1)} over {m.group(2)}"), t)
    t = re.sub(r"\b(\d{1,2}):(\d)\b", r"\1 to \2", t)  # ratios (16:9); times (10:30) are left alone
    for op, word in (("-", "minus"), (r"\+", "plus"), (r"\*", "times")):
        t = re.sub(rf"({_OPERAND_L})\s*{op}\s*(?={_OPERAND_R})", rf"\1 {word} ", t)
    t = re.sub(r"(?<![\w)])-(?=\d)", "minus ", t)  # a negative number
    for sup, word in SUPERSCRIPTS.items():
        t = t.replace(sup, word)
    t = re.sub(r"\s+[-–—]\s+", ", ", t)  # a dash between words is a pause
    t = re.sub(r"\s=\s|(?<=\w)=(?=\w)", " equals ", t)
    t = re.sub(r"\s<\s", " is less than ", t)
    t = re.sub(r"\s>\s", " is greater than ", t)
    for sym, word in SYMBOLS:
        t = t.replace(sym, word)
    t = re.sub(r"(?<!\w)\+\s*(?=[A-Z])", "", t)  # a "+ New" button is read as "New"
    t = re.sub(r"\bC\+\+", "C plus plus", t)
    t = re.sub(r"#(\d+)", r"number \1", t)
    t = re.sub(r"\band/or\b", "and or", t)
    t = re.sub(r"(?<=\w)/(?=\w)", " or ", t)  # on/off, Mac/Windows

    t = re.sub(rf"\b((?:[a-z0-9-]+\.)+(?:{TLDS}))\b", lambda m: _dotted(m.group(1)), t, flags=re.I)  # chat.openai.com
    t = re.sub(r"(?<![\w.])\.([a-z]{2,12})\b", r" dot \1", t)  # dotfiles: .env, .gitignore
    t = re.sub(r"\b([A-Za-z_][\w-]*)\.(py|js|ts|tsx|jsx|json|md|txt|html|css|sh|yml|yaml|toml|csv|mp4|mov|png|jpg|pdf|app|zip)\b",
               lambda m: f"{_words(m.group(1))} dot {m.group(2)}", t)  # file names
    t = re.sub(r"\b([a-z]+_[a-z_]+)\b", lambda m: m.group(1).replace("_", " "), t)  # snake_case
    for word in sorted(LEXICON, key=len, reverse=True):
        t = re.sub(rf"(?<![\w.]){re.escape(word)}(?![\w])", LEXICON[word], t)

    t = re.sub(r"\(\s*", ", ", t)  # asides become pauses
    t = re.sub(r"\s*\)", ",", t)
    t = re.sub(r"[\[\]{}<>|\\*_#~^]", " ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r",\s*([.;:!?])", r"\1", t)
    t = re.sub(r"([.;:!?]),", r"\1", t)
    t = re.sub(r",\s*,+", ",", t)
    t = re.sub(r"\.{2,}(?!\.)", "...", t)
    t = re.sub(r"\.\s*\.(?!\.)", ".", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" ,")


def _dotted(host: str) -> str:
    return " dot ".join(_words(part) for part in host.strip(".").split(".") if part)


def _path(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]  # a file name is then read by the file-name rule


def _words(name: str) -> str:
    return re.sub(r"[-_]+", " ", name)
