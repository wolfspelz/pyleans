# Task 16: Counter App (Silo with FastAPI)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-14-silo.md](task-14-silo.md)
- [task-15-counter-grain.md](task-15-counter-grain.md)
- [task-10-file-storage.md](task-10-file-storage.md)
- [task-11-yaml-membership.md](task-11-yaml-membership.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1 milestone, Section 7 (Silo usage)

## Description

Create a complete sample silo application that hosts the CounterGrain and
exposes it via a FastAPI HTTP API. This is the Phase 1 milestone.

### Files to create
- `examples/counter-app/main.py`
- `examples/counter-app/requirements.txt` (or pyproject.toml)

### main.py

```python
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
from pyleans.server import Silo
from pyleans.server.providers import FileStorageProvider, YamlMembershipProvider
from grains import CounterGrain

# Create silo
silo = Silo(
    grains=[CounterGrain],
    storage_providers={"default": FileStorageProvider("./data/storage")},
    membership_provider=YamlMembershipProvider("./data/membership.yaml"),
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start silo (non-blocking)
    silo_task = asyncio.create_task(silo.start())
    yield
    await silo.stop()
    silo_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/counter/{counter_id}")
async def get_counter(counter_id: str):
    counter = silo.grain_factory.get_grain(CounterGrain, counter_id)
    value = await counter.get_value()
    return {"counter_id": counter_id, "value": value}

@app.post("/counter/{counter_id}/increment")
async def increment_counter(counter_id: str):
    counter = silo.grain_factory.get_grain(CounterGrain, counter_id)
    value = await counter.increment()
    return {"counter_id": counter_id, "value": value}

@app.put("/counter/{counter_id}")
async def set_counter(counter_id: str, value: int):
    counter = silo.grain_factory.get_grain(CounterGrain, counter_id)
    await counter.set_value(value)
    return {"counter_id": counter_id, "value": value}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### What this demonstrates

- Silo co-hosted with FastAPI in the same process
- Grain calls from HTTP handlers
- State persisted to files (survives restart)
- Membership visible in YAML file
- Multiple counter instances (different counter_id values)

### Acceptance criteria

- [ ] `python main.py` starts silo + FastAPI
- [ ] `GET /counter/foo` returns `{"counter_id": "foo", "value": 0}` initially
- [ ] `POST /counter/foo/increment` returns incremented value
- [ ] `PUT /counter/foo?value=42` sets the value
- [ ] State persists across restarts (kill + restart, same value)
- [ ] `data/membership.yaml` shows silo entry
- [ ] `data/storage/CounterGrain/foo.json` contains state
- [ ] Multiple counters (`foo`, `bar`) are independent

## Summary of implementation
_To be filled when task is complete._