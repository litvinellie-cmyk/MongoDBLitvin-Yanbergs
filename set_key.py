"""Save an API key into .env without showing it on screen.

  python3 set_key.py ELEVENLABS_API_KEY
"""

import getpass
import re
import sys
from pathlib import Path

name = sys.argv[1] if len(sys.argv) > 1 else "ELEVENLABS_API_KEY"
env = Path(__file__).with_name(".env")
value = getpass.getpass(f"Paste {name} (typing is hidden), then press Enter: ").strip()
if not value:
    raise SystemExit("Nothing entered, .env unchanged.")
text = env.read_text() if env.exists() else ""
text = re.sub(rf"(?m)^{name}=.*\n?", "", text)
if text and not text.endswith("\n"):
    text += "\n"
env.write_text(text + f"{name}={value}\n")
print(f"saved {name}")
