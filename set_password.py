"""Put the Atlas database password into .env without showing it on screen.

  python3 set_password.py
"""

import getpass
import re
import urllib.parse
from pathlib import Path

env = Path(__file__).with_name(".env")
text = env.read_text()
m = re.search(r"^MONGODB_URI=mongodb\+srv://([^:]+):([^@]*)@", text, re.M)
if not m:
    raise SystemExit("MONGODB_URI line not found in .env")
user = m.group(1)
pw = getpass.getpass(f"Password for {user} (typing is hidden): ")
if not pw:
    raise SystemExit("Nothing entered, .env unchanged.")
new = text[:m.start(2)] + urllib.parse.quote(pw, safe="") + text[m.end(2):]
env.write_text(new)
print("saved")
