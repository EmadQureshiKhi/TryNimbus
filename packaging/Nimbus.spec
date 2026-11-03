# PyInstaller spec for the downloadable Nimbus.app. Build with scripts/build_release.sh.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is set by PyInstaller
VERSION = "0.1.0"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "nimbus" / "assets"), "nimbus/assets")],
    # pynput and keyring pick their macOS backends at run time, out of PyInstaller's sight.
    hiddenimports=collect_submodules("nimbus") + collect_submodules("pynput") + collect_submodules("keyring.backends"),
    excludes=["tkinter", "pytest", "IPython"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Nimbus", console=False,  # noqa: F821
          target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="Nimbus")  # noqa: F821
app = BUNDLE(  # noqa: F821
    coll,
    name="Nimbus.app",
    icon=str(ROOT / "dist" / "Nimbus.icns"),
    bundle_identifier="ai.nimbus.mac",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Nimbus",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Nimbus listens while you hold the talk key so you can ask about your screen.",
        "NSScreenCaptureUsageDescription":
            "Nimbus looks at your screen when you ask a question, so it can point at what it is talking about.",
        "NSAppleEventsUsageDescription": "Nimbus can open apps for you when you ask it to.",
    },
)
