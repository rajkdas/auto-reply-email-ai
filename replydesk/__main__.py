"""Entry point so ``python -m replydesk`` works."""
import sys

from .main import _cli

if __name__ == "__main__":
    sys.exit(_cli())
