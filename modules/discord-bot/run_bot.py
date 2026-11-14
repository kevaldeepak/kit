"""Service entry point: runs the Discord bot (botcore) from this folder."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from botcore import bot  # noqa: E402

if __name__ == "__main__":
    bot.main()
