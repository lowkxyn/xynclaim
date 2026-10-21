# PyInstaller spec for the XynClaim desktop app (onedir, no console).
# Build with packaging\build.ps1, which runs this from the project root.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

ui_files = [
    (str(path), str(path.parent.relative_to(ROOT)))
    for path in (ROOT / "ui").rglob("*")
    # Underscore pages (preview, theme drafts) and the node contract test are dev-only.
    if path.is_file() and not path.name.startswith("_") and path.name != "ui-contract.test.mjs"
]

a = Analysis(
    [str(ROOT / "xynclaim" / "__main__.py")],
    pathex=[str(ROOT)],
    datas=ui_files + [(str(ROOT / "assets" / "reference"), "assets/reference"),
                       (str(ROOT / "packaging" / "xynclaim.ico"), "packaging")],
    # winrt ships each Windows namespace as its own package, imported lazily by xynclaim.ocr.
    hiddenimports=(collect_submodules("xynclaim") + collect_submodules("winrt")
                   + ["webview.platforms.edgechromium", "clr"]),
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="XynClaim",
    icon=str(ROOT / "packaging" / "xynclaim.ico"),
    console=False,
    upx=False,
)
# OpenCV's FFmpeg plugin is only for video files, which XynClaim never opens; leaving it out
# also keeps LGPL code out of the bundle.
binaries = [entry for entry in a.binaries if "opencv_videoio_ffmpeg" not in entry[0].lower()]
coll = COLLECT(exe, binaries, a.datas, name="XynClaim", upx=False)
