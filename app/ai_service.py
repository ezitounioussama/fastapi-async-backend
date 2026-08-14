"""The async AI service layer.

There is no API key here. Every call is a **fake async AI call**: it awaits
`asyncio.sleep` to imitate the network latency of a real model, then returns
deterministic placeholder content. That is enough to demonstrate the async flow,
and it makes timeouts and failures reproducible in a test instead of depending on
a real service being slow at the right moment.

Why `asyncio.sleep` and not `time.sleep`
----------------------------------------
`time.sleep` blocks the thread. Inside an `async def` running on the event loop,
that would freeze *every* other request for the duration — the exact problem
async is supposed to solve. `await asyncio.sleep(...)` suspends only this
coroutine and hands control back to the loop, so other requests keep being
served while this one waits.

Swapping in a real model means replacing the `await asyncio.sleep(...)` line with
`await client.chat.completions.create(...)` from an async client. Nothing else in
this file — and nothing in the routers — has to change.
"""

import asyncio
import re
from typing import List

from app import config


class UpstreamAIError(Exception):
    """The AI service failed in a way that might succeed if retried.

    Modelled on a 503 from a real provider: a transient upstream problem rather
    than something wrong with the request.
    """


async def _simulate_latency(seconds: float) -> None:
    """Wait, without blocking the event loop."""
    await asyncio.sleep(seconds)


async def _fake_ai_call(
    delay_seconds: float,
    fail: bool = False,
) -> None:
    """Shared body of every fake call: wait, then maybe fail.

    The failure is raised *after* the wait, which matches how a real timeout
    interacts with a real error — the request has already cost you the latency by
    the time the service tells you it went wrong.
    """
    await _simulate_latency(delay_seconds)

    if fail:
        raise UpstreamAIError("The AI service is temporarily unavailable (simulated).")


# ---------------------------------------------------------------------------
# The three independent generators
#
# These are the tasks that /study-pack runs concurrently. They are independent:
# none of them needs another's output, which is exactly the condition that makes
# asyncio.gather worthwhile.
# ---------------------------------------------------------------------------


async def generate_summary(
    text: str,
    max_bullets: int = 3,
    *,
    delay_seconds: float = config.DEFAULT_AI_DELAY,
    fail: bool = False,
) -> List[str]:
    """Summarise text into bullets. Extractive: sentences are selected, not written."""
    await _fake_ai_call(delay_seconds, fail)

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]

    if len(sentences) <= max_bullets:
        return sentences

    # Score by length, with a bonus for the opening sentence, then restore the
    # original reading order among the winners.
    scored = [(len(s) + (100 if i == 0 else 0), i, s) for i, s in enumerate(sentences)]
    best = sorted(scored, reverse=True)[:max_bullets]
    best.sort(key=lambda item: item[1])

    return [sentence for _, _, sentence in best]


async def generate_quiz(
    text: str,
    num_questions: int = 3,
    *,
    delay_seconds: float = config.DEFAULT_AI_DELAY,
    fail: bool = False,
) -> List[dict]:
    """Produce quiz questions from the text."""
    await _fake_ai_call(delay_seconds, fail)

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]

    questions = []
    for index in range(num_questions):
        source = sentences[index % len(sentences)] if sentences else "the material"
        # Trim to a readable stem rather than quoting a whole paragraph.
        stem = source[:80].rstrip(".") if len(source) > 80 else source.rstrip(".")

        correct = "The statement from the material"
        distractors = ["An unrelated claim", "The opposite of the material", "None of these"]
        answer_index = index % 4
        options = distractors[:answer_index] + [correct] + distractors[answer_index:]

        questions.append(
            {
                "number": index + 1,
                "question": f"Which option matches the material about: {stem}?",
                "options": options,
                "answer_index": answer_index,
            }
        )

    return questions


async def generate_flashcards(
    text: str,
    num_cards: int = 3,
    *,
    delay_seconds: float = config.DEFAULT_AI_DELAY,
    fail: bool = False,
) -> List[dict]:
    """Produce front/back flashcards from the text."""
    await _fake_ai_call(delay_seconds, fail)

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]

    cards = []
    for index in range(num_cards):
        source = sentences[index % len(sentences)] if sentences else "the material"
        words = source.split()
        topic = " ".join(words[:5]).rstrip(".,") if words else "the material"

        cards.append(
            {
                "number": index + 1,
                "front": f"What should you remember about {topic}?",
                "back": source,
            }
        )

    return cards


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def stream_answer(question: str, *, chunk_delay: float = 0.15):
    """Yield an answer a few words at a time.

    An async generator: each `yield` hands a chunk to the caller, and the `await`
    between chunks lets the event loop serve other requests while this response
    is still being produced. That is how a token-by-token model response is
    surfaced without waiting for the whole thing first.
    """
    answer = (
        f"Here is a placeholder answer to: {question} "
        "Async lets the server hand back the first words immediately, "
        "instead of holding the whole reply until it is finished."
    )

    for word in answer.split():
        await asyncio.sleep(chunk_delay)
        yield word + " "
