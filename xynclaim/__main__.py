"""Start the desktop app from Python or the bundled executable."""

import sys
from pathlib import Path

# Task Scheduler invokes this absolute file path independently of its caller's cwd.
# Run as a script, Python puts this package folder first on sys.path, where module
# names like stats or history could shadow other packages; use the repo root instead.
if not __package__ and not getattr(sys, "frozen", False):
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from xynclaim.app import main

if __name__ == "__main__":
    main()
