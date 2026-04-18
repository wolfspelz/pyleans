"""Allow running as ``python -m src.counter_app``."""

import asyncio

from src.counter_app.main import main

asyncio.run(main())
