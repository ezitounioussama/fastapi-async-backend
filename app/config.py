"""Settings and the async timing knobs."""

import os

APP_NAME = "AI Study Assistant API (async)"
APP_VERSION = "3.0.0"

# How long a fake AI call pretends to take. Real model calls sit in this range,
# which is exactly why they should be awaited rather than blocking a thread.
DEFAULT_AI_DELAY = float(os.getenv("AI_DELAY_SECONDS", "0.6"))

# How long the server is willing to wait for one AI call before giving up.
# Without a ceiling a hung upstream service would hold the request open until
# the client or a proxy eventually killed it.
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "3.0"))

# The study pack runs three calls at once, so its budget is the slowest single
# call plus a little room — NOT the sum of all three. That only works because
# they run concurrently.
STUDY_PACK_TIMEOUT_SECONDS = float(os.getenv("STUDY_PACK_TIMEOUT_SECONDS", "5.0"))

# Ceiling on the delay a caller may request in a demo, so the simulation knobs
# cannot be used to tie up the server.
MAX_SIMULATED_DELAY = 30.0

# Validation limits
TEXT_MIN = 20
TEXT_MAX = 10000
BULLETS_MIN, BULLETS_MAX = 1, 10
QUESTIONS_MIN, QUESTIONS_MAX = 1, 10
CARDS_MIN, CARDS_MAX = 1, 10
QUESTION_MIN, QUESTION_MAX = 1, 500
