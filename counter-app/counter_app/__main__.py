"""Allow running as ``python -m counter_app``."""

import asyncio

from counter_app.main import main

asyncio.run(main())
