"""Launcher: python p2s.py <ip> status   (works with the portable 'embeddable' Python too)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p2s.cli import main  # noqa: E402

main()
