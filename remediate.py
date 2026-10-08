#!/usr/bin/env python
"""python remediate.py input.pdf  ->  input_accessible.pdf + report + review file"""
import sys

from remediator.cli import main

if __name__ == "__main__":
    sys.exit(main())
