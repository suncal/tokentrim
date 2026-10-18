#!/usr/bin/env python3
"""Hook launcher: lets settings.json call tokentrim without installing a package."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokentrim.hook import main  # noqa: E402

main()
