"""Stand-in for a real notification command: appends the message to the file named in LANE_NOTIFY_FILE."""
import os
from pathlib import Path

with open(Path(os.environ["LANE_NOTIFY_FILE"]), "a", encoding="utf-8") as f:
    f.write(os.environ["LANE_MESSAGE"] + "\n")
