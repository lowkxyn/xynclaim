"""Prepare embedded resources and retain the licences of the locked Cargo graph."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def notices(output: Path) -> None:
    result = subprocess.run(
        ["cargo", "metadata", "--locked", "--filter-platform", "x86_64-pc-windows-msvc", "--format-version=1", "--manifest-path", str(HERE / "Cargo.toml")],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    metadata = json.loads(result.stdout)
    included = {node["id"] for node in metadata["resolve"]["nodes"]}
    packages = [p for p in metadata["packages"] if p["id"] in included]
    chunks = ["Custom installer third-party notices. The application has separate THIRD-PARTY-NOTICES.txt."]
    for package in sorted(packages, key=lambda p: (p["name"], p["version"])):
        if package["source"] is None:
            continue
        folder = Path(package["manifest_path"]).parent
        texts = []
        candidates = list(folder.iterdir())
        if (folder / "licenses").is_dir():
            candidates.extend((folder / "licenses").rglob("*"))
        if package.get("license_file"):
            candidates.append(folder / package["license_file"])
        for file in sorted(set(candidates)):
            if file.is_file() and (any(w in file.name.upper() for w in ("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                                   or file == folder / (package.get("license_file") or "__none__")):
                texts.append(f"--- {file.relative_to(folder).as_posix()} ---\n" + file.read_text(encoding="utf-8", errors="replace"))
        if not texts:
            if package["name"] in {"webview2-com", "webview2-com-macros", "webview2-com-sys"}:
                texts.append((HERE / "licenses/webview2-rs-MIT.txt").read_text(encoding="utf-8"))
            else:
                raise RuntimeError(f"Missing licence text for {package['name']} {package['version']}")
        chunks.append(f"{'=' * 78}\n{package['name']} {package['version']}\n"
                      f"SPDX: {package.get('license') or 'see licence text'}\n{package.get('repository') or ''}\n\n" + "\n".join(texts))
    chunks.append((ROOT / "ui/fonts/THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8"))
    chunks.append((ROOT / "ui/fonts/OFL-1.1.txt").read_text(encoding="utf-8"))
    output.write_text("\n\n".join(chunks), encoding="utf-8")


def prepare(bundle: Path, source: Path, version: str) -> None:
    app = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    app["version"] = version
    app["licence"] = (ROOT / "LICENSE").read_text(encoding="utf-8")
    css = (HERE / "style.css").read_text(encoding="utf-8").replace(
        "__LOGO__", "data:image/png;base64," + base64.b64encode((HERE / app["logo"]).read_bytes()).decode())
    # The icon includes its tile; preserve the actual XC artwork and mask only the sheen.
    css += '\n.mark{background-image:var(--logo);background-size:100%;background-repeat:no-repeat}\n'
    html = (HERE / "index.html").read_text(encoding="utf-8")
    for key, value in {"__APP__": app["name"], "__BRAND__": app["brand"], "__VERSION__": version,
                       "__READY_TEXT__": app["readyText"], "__STYLE__": css,
                       "__CONFIG__": "const APP = " + json.dumps(app).replace("<", "\\u003c") + ";",
                       "__SCRIPT__": (HERE / "ui.js").read_text(encoding="utf-8")}.items():
        html = html.replace(key, value)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "ui.html").write_text(html, encoding="utf-8")
    (bundle / "version.txt").write_text(version, encoding="utf-8")
    payload = [{"path": str(p.relative_to(source)).replace("\\", "/"), "size": p.stat().st_size}
               for p in sorted(source.rglob("*")) if p.is_file()]
    payload.append({"path": "CUSTOM-INSTALLER-NOTICES.txt", "size": (bundle / "CUSTOM-INSTALLER-NOTICES.txt").stat().st_size})
    (bundle / "payload.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    args.bundle.mkdir(parents=True, exist_ok=True)
    notices(args.bundle / "CUSTOM-INSTALLER-NOTICES.txt")
    prepare(args.bundle, args.source, args.version)
