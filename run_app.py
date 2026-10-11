"""Double-click (or `python run_app.py`) to open Plane Tracker in your web browser."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    try:
        import flask, rasterio, scipy, shapely  # noqa: F401,E401
    except ImportError:
        print("Installing requirements (first run only)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r",
                               os.path.join(HERE, "requirements.txt")])
    os.chdir(HERE)
    sys.exit(subprocess.call([sys.executable, "-m", "planetracker", "serve", "--open", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
