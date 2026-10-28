"""Invocation of macOS security; interpretation belongs to each caller."""

import subprocess
from typing import List, Optional


def run(args: List[str], *, timeout: float, input: Optional[str] = None):
    return subprocess.run(["security", *args], input=input, capture_output=True,
                          text=True, timeout=timeout)
