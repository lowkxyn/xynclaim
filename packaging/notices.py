"""Write THIRD-PARTY-NOTICES.txt for the build: the licence text of every bundled package.

Run with the build venv's Python (packaging\build.ps1 does): python packaging\notices.py <output file>
"""
from importlib import metadata
from pathlib import Path
import sys

# Distributions PyInstaller bundles into dist\XynClaim (see _internal).
BUNDLED = [
    "bottle", "cffi", "clr_loader", "comtypes", "mss", "numpy", "opencv-python", "packaging",
    "proxy-tools", "psutil", "pycaw", "pycparser", "PyDirectInput", "pythonnet", "pywebview",
    "setuptools", "typing_extensions", "winrt-runtime", "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections", "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging", "winrt-Windows.Media.Ocr", "winrt-Windows.Storage.Streams",
]
LICENCE_WORDS = ("LICEN", "COPYING", "NOTICE")
RULE = "=" * 78


def licence_texts(distribution: metadata.Distribution) -> list[str]:
    texts = []
    for file in distribution.files or []:
        if any(word in file.name.upper() for word in LICENCE_WORDS) and file.suffix.lower() in {"", ".txt", ".md", ".rst"}:
            texts.append(f"--- {file.name} ---\n{Path(file.locate()).read_text(encoding='utf-8', errors='replace').strip()}")
    if not texts:
        # Some wheels (winrt, proxy-tools) only declare the licence in their metadata.
        declared = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "see project"
        home = distribution.metadata.get("Home-page") or next(
            (url.split(",", 1)[-1].strip() for url in distribution.metadata.get_all("Project-URL") or []), "")
        texts.append(f"Licence: {declared}\n{home}".strip())
    return texts


def main(output: Path) -> None:
    sections = [
        "XynClaim bundles the following third-party software. Each keeps its own licence.\n"
        "The Chakra Petch, Inter and DM Mono fonts are covered by ui/fonts/OFL-1.1.txt.\n"
        "Microsoft.Web.WebView2 (shipped by pywebview) is licensed by Microsoft under the BSD 3-Clause\n"
        "licence: https://www.nuget.org/packages/Microsoft.Web.WebView2/",
    ]
    python_licence = Path(sys.base_prefix) / "LICENSE.txt"
    sections.append(f"{RULE}\nPython {sys.version.split()[0]}\n{RULE}\n"
                    + (python_licence.read_text(encoding="utf-8", errors="replace").strip()
                       if python_licence.is_file() else "PSF License Agreement: https://docs.python.org/3/license.html"))
    for name in BUNDLED:
        distribution = metadata.distribution(name)
        header = f"{RULE}\n{distribution.metadata['Name']} {distribution.version}\n{RULE}"
        sections.append(header + "\n" + "\n\n".join(licence_texts(distribution)))
    output.write_text("\n\n".join(sections) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
