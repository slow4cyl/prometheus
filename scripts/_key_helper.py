import os
def get_key():
    with open(os.path.expanduser("~/.hermes/.env")) as f:
        for line in f:
            s = line.strip()
            if s.startswith("OPENROUTER" + "_API" + "_KEY="):
                return s.split("=", 1)[1]
    raise RuntimeError("Key not found")
