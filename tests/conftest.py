"""Test fixtures."""

import httpx
import pytest_asyncio

from app.main import app

BASE_URL = "http://testserver"

SAMPLE_TEXT = (
    "Python is a high-level programming language. It is known for readable syntax. "
    "Many beginners start with Python because the code looks close to plain English. "
    "It is widely used for web development, data analysis and automation."
)


@pytest_asyncio.fixture
async def client():
    """httpx client wired to the app in-process through ASGITransport.

    This keeps the whole request path real — routing, validation, the event loop —
    without starting a server or touching the network.
    """
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL, timeout=30.0
    ) as async_client:
        yield async_client
