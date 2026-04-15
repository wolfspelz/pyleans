# Beyond Stateless

**Speaker:** Heiner Wolf
**Event:** PyConDE & PyData Darmstadt 2026
**Date:** April 14, 2026

## Thesis

The dominant "stateless web server + database" architecture is fundamentally wrong for scalable applications. The "stateless is good" mantra leads to unnecessary complexity, and AI coding agents perpetuate this pattern because they learn from mainstream code. There is a better way: **virtual actors**.

## The Problem

Building a scalable, high-availability system (e.g. an online game with millions of concurrent players) forces developers to juggle microservices, Kubernetes, NoSQL, pub/sub, message queues, load balancers, caching layers, serialization, threading, and synchronization. Even AI agents can't eliminate this accidental complexity -- they still have to orchestrate all of it.

Small programs are easy because they are just classes and methods. Scalable apps bolt on a tower of infrastructure. The gap between the two should not exist.

## The Vision

What if you could write a scalable distributed application the same way you write a small local program? Just classes with state, calling methods on each other -- and the framework handles distribution, persistence, high availability, and scale-out transparently.

The only difference between local and distributed code:

```python
# Local
room.join(self.id)

# Distributed (possibly remote)
await room.join(self.id)
```

## Why Not Microservices?

REST microservices turn every class into its own Dockerfile, OS process, web server, HTTP parser, and Kubernetes deployment. REST is great for public APIs and inter-organization composition, but it's the wrong abstraction for scaling out internal business logic.

## The Solution: Virtual Actor Clusters

Instead of stateless web servers hitting a database, put **stateful business objects into a cluster**:

- **Cluster**: Connected nodes that host object instances, avoiding the latency and overhead of external services.
- **Membership Provider**: Tracks active nodes (via ZooKeeper, Redis, MongoDB, cloud storage, etc.).
- **Storage Provider**: Persists object state (SQL, NoSQL, JSON, file, S3, etc.).
- **Stream Provider**: Handles pub/sub (Kafka, Redis, Service Bus, SQS, etc.).

The web server becomes a thin gateway. Business logic lives as stateful objects distributed across cluster nodes.

## Benefits

| Concern | Virtual Actor Solution |
|---|---|
| Business object caching | Native -- objects live in memory |
| RPC | Nearly invisible (`await`) |
| Parallelism | Massive, but no manual threading/sync |
| Frontend caching | Built-in |
| Streaming | Built-in |
| Scale-out | Add nodes |
| High availability | Automatic rebalancing on node failure |

The programming model stays the same whether the app is small or web-scale.

## History: How We Got Here

1. Single web server + DB -- bottleneck at the server.
2. Load balancer + multiple stateless web servers -- bottleneck moves to the DB.
3. DB replication (read replicas) -- still high write contention.
4. Caching layers (memcached/Redis) -- now logic is separated from its data by multiple hops.
5. **Insight**: stateless logic + external cache is really just a poorly reassembled object instance. Reunite logic and state as in-memory objects in the cluster.

## Existing Implementations

- **Orleans** (Microsoft, .NET) -- the original virtual actor framework
- **Ray** (Python, but closing/changing direction)
- **Akka** (JVM, but license changed)
- All implement the **virtual actor** model

## Call to Action

- Put business logic into a virtual actor cluster instead of into the web server.
- The setup is no harder than a typical Vue/Vite dev workflow with a CI pipeline.
- For AI agents: add to your rules that all web services should use a virtual actor backend.
- **Next step**: Build an Orleans-equivalent virtual actor framework in Python ("pyleans") with pluggable membership, storage, and streaming providers.
