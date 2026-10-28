"""What Nimbus says aloud: symbols, maths, links and names become words; captions keep the original."""
import pytest

from nimbus.mac.io import best_voice
from nimbus.speakable import speakable


@pytest.mark.parametrize("text, spoken", [
    ("Click **+ New** at the top left (the sidebar).", "Click New at the top left, the sidebar."),
    ("Factor: (x - 2)(x - 3) = 0", "Factor: x minus 2, times x minus 3, equals 0"),
    ("x² - 5x + 6 = 0", "x squared minus 5x plus 6 equals 0"),
    ("2x = 8 -> x = 4", "2x equals 8, so x equals 4"),
    ("Press Ctrl+Opt+Space or ⌘⇧T.", "Press control option Space or command shift T."),
    ("Open https://www.chatgpt.com/auth/login?x=1 now", "Open chatgpt dot com now"),
    ("It is chat.openai.com, e.g. the login page", "It is chat dot openai dot com, for example the login page"),
    ("Edit /Users/me/project/nimbus/app.py and/or settings.json", "Edit app dot py and or settings dot json"),
    ("Put the API key in .env", "Put the A P I key in dot env"),
    ("Turn Wi-Fi on/off 🚀", "Turn wifi on or off"),
    ("1/2 of 16, ratio 16:9, at 10:30", "one half of 16, ratio 16 to 9, at 10:30"),
    ("add to -5", "add to minus 5"),
    ("git push - done", "git push, done"),
    ("the x-axis of a well-known chart", "the x-axis of a well-known chart"),
])
def test_speakable(text, spoken):
    assert speakable(text) == spoken


def test_best_voice_prefers_the_neural_voices():
    assert best_voice(["Samantha", "Zoe (Enhanced)", "Ava (Premium)"]) == "Ava (Premium)"
    assert best_voice(["Fred", "Samantha"]) == "Samantha"
    assert best_voice(["Fred"]) == ""
