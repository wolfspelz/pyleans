# Counter-App Sample

A minimal demonstration silo that hosts `CounterGrain` and
`AnswerGrain`. Single-silo in Phase 1, extended in Phase 2 to run as
a two-silo cluster on localhost.

## Single-silo demo (Phase 1)

```bash
# Terminal 1 — silo
python -m src.counter_app

# Terminal 2 — client
python -m src.counter_client inc my-counter
python -m src.counter_client inc my-counter
python -m src.counter_client get my-counter   # prints 2
```

## Two-silo demo (Phase 2)

Start two silos sharing a membership file and the same cluster id.
The client can hit either gateway; the counter activates on one
silo and subsequent increments route there regardless of the
gateway the client contacted.

```bash
# Terminal 1 — first silo
python -m src.counter_app --port 11111 --gateway 30000 \
    --membership ./data/membership.md --cluster-id dev

# Terminal 2 — second silo (same membership file, different ports)
python -m src.counter_app --port 11112 --gateway 30001 \
    --membership ./data/membership.md --cluster-id dev

# Terminal 3 — client, talks to either gateway
python -m src.counter_client --gateway localhost:30000 inc my-counter
python -m src.counter_client --gateway localhost:30001 inc my-counter
python -m src.counter_client --gateway localhost:30000 get my-counter   # prints 2
```

### Gateway fallback

Pass `--gateway` more than once; the client tries each in order and
falls back on connection error.

```bash
python -m src.counter_client \
    --gateway localhost:30000 --gateway localhost:30001 \
    inc my-counter
```

## CLI reference

### counter_app

| Flag             | Default                   | Purpose                                |
|------------------|---------------------------|----------------------------------------|
| `--host`         | `localhost`               | Bind host for silo + gateway           |
| `--port`         | `11111`                   | Silo-to-silo TCP port                  |
| `--gateway`      | `30000`                   | Gateway TCP port                       |
| `--membership`   | `./data/membership.md`    | Shared membership file                 |
| `--cluster-id`   | `dev`                     | Cluster identifier                     |
| `--storage-dir`  | `./data/storage`          | Storage root                           |
| `--log-level`    | `INFO`                    | Root logger level                      |

### counter_client

| Flag         | Default            | Purpose                                     |
|--------------|--------------------|---------------------------------------------|
| `--gateway`  | `localhost:30000`  | Gateway address. Repeat for fallback.       |

## Scope

This is a demo, not a production example. See
[`docs/adr/adr-counter-sample-scope.md`](../../docs/adr/adr-counter-sample-scope.md)
for what stays out (config files, auth, metrics, Docker Compose).

## Further reading

- [docs/architecture/consistent-hash-ring.md](../../docs/architecture/consistent-hash-ring.md) — why calls
  from either gateway converge on the same silo for a given counter id
- [docs/orleans-architecture/orleans-cluster.md](../../docs/orleans-architecture/orleans-cluster.md) — the
  Orleans cluster model pyleans mirrors
