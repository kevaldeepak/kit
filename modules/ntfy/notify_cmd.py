"""`notify` command: thin wrapper around kit.notify."""
import sys

from kit.notify import main

if __name__ == "__main__":
    sys.exit(main())
