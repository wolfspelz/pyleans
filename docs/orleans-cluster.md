# Orleans Cluster Architecture, Silos, and Membership

This document provides a thorough reference on how Microsoft Orleans manages
clusters of silos, including the membership protocol, grain directory, silo
lifecycle, and client connectivity.  It is derived from the official Microsoft
Learn documentation (learn.microsoft.com/dotnet/orleans), the Orleans GitHub
repository (github.com/dotnet/orleans), and the original Orleans research paper
("Orleans: Distributed Virtual Actors for Programmability and Scalability",
MSR-TR-2014-41, 2014).

---

## 1. What Is a Silo?

A **silo** is the fundamental server-side host in Orleans.  One OS process runs
one silo.  A silo:

- Hosts zero or more **grain activations** (in-memory instances of virtual
  actors).
- Provides runtime services to those grains: timers, reminders (persistent
  timers), persistence, transactions, streams, the grain directory partition,
  and the membership protocol agent.
- Exposes two network endpoints:
  - **Silo-to-silo endpoint** (default port 11111): used for inter-silo
    messaging (grain calls, membership protocol probes, directory operations,
    snapshot broadcasts).
  - **Gateway endpoint** (default port 30000): used by external Orleans clients
    to send grain calls into the cluster.
- Participates in the cluster membership protocol as both a monitor and a
  monitored node.
- Owns a partition of the distributed grain directory (see Section 6).

Internally a silo is built on the standard .NET Generic Host and uses
dependency injection throughout.  The silo builder (`ISiloBuilder` /
`UseOrleans`) configures:

1. A **clustering provider** (the `IMembershipTable` implementation).
2. **Cluster options** (`ClusterOptions`): `ServiceId` (stable across
   deployments, identifies the logical service) and `ClusterId` (identifies
   which set of silos form one cluster).
3. **Endpoint options** (`EndpointOptions`): silo port, gateway port,
   advertised IP address, and optional separate listening endpoints for NAT /
   port-forwarding scenarios.
4. Grain storage providers, stream providers, reminder providers, serializers,
   placement strategies, and application-specific services.

A group of silos sharing the same `ClusterId` and `ServiceId` forms a
**cluster**.  The cluster can scale from 1 to hundreds (proven in production up
to ~200 silos, designed to support thousands).

---

## 2. Cluster Formation: How Silos Discover Each Other

Silos do not use multicast or gossip for initial discovery.  Instead they rely
on a shared, durable external store accessed through the **`IMembershipTable`**
abstraction.  This table serves as a **rendezvous point**:

1. When a new silo starts, it writes its own entry into the table keyed by
   `(deploymentId, ip:port:epoch)`.  The epoch is the time-in-ticks when the
   silo started, making the triple globally unique within a deployment.
2. It then reads the full table to learn about all other silos that are
   currently alive.
3. Before being admitted to the cluster it must validate **two-way
   connectivity** to every other active silo.  If any existing silo does not
   respond, the new silo is **not allowed to join** (see exception for
   `IAmAlive`-based disaster recovery below).
4. Once admitted, the silo becomes visible to other silos during their periodic
   table reads and via snapshot broadcasts.

External Orleans **clients** also read the membership table (specifically the
gateway entries) to discover which silos accept client connections.

---

## 3. The Membership Protocol

### 3.1 Goals

- All silos agree on the set of currently alive silos.
- Failed silos are detected and removed.
- New silos can join.
- The protocol is **accurate** (never mistakenly declares a live silo dead due
  to table unavailability) and **complete** (eventually declares actually-dead
  silos dead).

### 3.2 Protocol Overview

The protocol combines **direct peer-to-peer failure detection** with a
**shared durable membership table** for agreement.

| Step | Description |
|------|-------------|
| 1 | Each silo adds its own row to the `IMembershipTable` on startup. |
| 2 | Silos probe each other directly via "are you alive" heartbeats sent over the same TCP connections used for grain calls. |
| 3 | Each silo monitors a configurable number of other silos, chosen by consistent hashing on silo identities (a virtual ring). |
| 4 | If silo S fails to receive Y probe replies from monitored silo P, S writes a timestamped **suspicion** into P's row in the table. |
| 5 | If P accumulates Z suspicions from different silos within a configurable time window T, the last suspecting silo declares P **dead** by updating P's Status column. |
| 6 | Every silo periodically reads the full table to learn about joins and deaths. |
| 7 | After any table write, the writing silo broadcasts a **snapshot** of the current table state to all silos to speed up propagation (the periodic read is a fallback). |

### 3.3 Default Configuration

The defaults differ by Orleans version:

| Parameter | Orleans 7/8 | Orleans 9/10 |
|-----------|-------------|--------------|
| `NumProbedSilos` (silos each silo monitors) | 3 | 10 |
| `NumVotesForDeathDeclaration` | 2 | 2 |
| `DeathVoteExpirationTimeout` | 180 s | 180 s |
| `ProbeTimeout` (interval between probes) | 10 s | 10 s |
| `NumMissedProbesLimit` | 3 | 3 |

With the Orleans 9/10 defaults, typical failure detection time is approximately
**15 seconds**.

### 3.4 Suspicion and Voting in Detail

1. Silo S suspects silo P after missing `NumMissedProbesLimit` consecutive
   probe responses.
2. S reads P's row from the table (including an ETag / version for optimistic
   concurrency).
3. If P already has `NumVotesForDeathDeclaration - 1` suspicions within the
   window `DeathVoteExpirationTimeout`:
   - S is the deciding voter.  S adds itself to the suspicion list **and**
     writes `Status = Dead` in P's row.
4. Otherwise S merely adds itself to the suspicion list.
5. The write uses the previously-read ETag/version.  If it fails (concurrent
   modification), S retries: read again, check if P is already dead, and
   re-attempt the write.
6. This read-modify-write sequence is a logical transaction enforced by the
   optimistic concurrency control of the underlying store.

Expired suspicions (older than `DeathVoteExpirationTimeout`) are ignored.

### 3.5 Indirect Probing (Lifeguard-Inspired)

Orleans incorporates ideas from Hashicorp's Lifeguard research
(arxiv.org/abs/1707.00788).  When a monitoring silo has only two probe
attempts remaining before voting a target dead, it uses **indirect probing**:

1. S randomly selects another silo I as an intermediary.
2. S asks I to probe target P.
3. If I also fails to reach P and I declares itself healthy (via
   self-monitoring), then the negative acknowledgment from I counts as **both
   votes** (with the default `NumVotesForDeathDeclaration = 2`), allowing
   faster death declaration when multiple perspectives confirm the failure.

### 3.6 Self-Monitoring (Lifeguard-Inspired)

Each silo scores its own health using the `LocalSiloHealthMonitor` component.
Health is assessed on a 0-8 scale (0 = fully healthy) based on:

- Active status in the membership table.
- No suspicions from other silos.
- Recent successful probe responses.
- Recent probe requests received (shows others can reach us).
- Thread pool responsiveness (work items executing within 1 second).
- Timer accuracy (timers firing within 3 seconds of schedule).

An unhealthy silo (score 1-8) gets **increased probe timeouts**, which:

- Gives more time for probes to succeed under stress.
- Makes it more likely the unhealthy silo is voted dead before it can
  incorrectly vote out healthy silos.

### 3.7 Enforcing Perfect Failure Detection

Once a silo is declared dead in the table, **all silos treat it as dead** even
if it is still running.  They stop communicating with it.  When the dead silo
reads its own status from the table, it **terminates its own process**.  An
external supervisor (Kubernetes, Windows Service, Azure infrastructure) must
restart it as a new process with a new epoch.

### 3.8 Ordered Membership Views

Every write to the membership table atomically increments a **membership
version number** (stored in a special version row).  This ensures:

- A **global total order** of all membership changes.
- Higher-level protocols (grain directory, etc.) can reason about monotonically
  increasing views.

All writes are serialized through this version row using atomic updates
(database transactions or ETag-based compare-and-swap).  This design has been
proven in production up to ~200 silos; beyond ~1000, version-row contention
might become a bottleneck.

### 3.9 Behavior When the Table Is Unavailable

- Operational silos **continue working**.  No silo is mistakenly killed.
- Dead silos cannot be declared dead (suspicions cannot be written).
- New silos cannot join.
- Accuracy is preserved; completeness (detecting deaths) may be delayed.

### 3.10 IAmAlive for Diagnostics and Disaster Recovery

Each silo periodically (default every 30 seconds) writes a timestamp to an
`IAmAlive` column in its row.

- **Diagnostics**: Admins can query the table to see when each silo was last
  alive.
- **Disaster recovery**: If a silo's `IAmAlive` timestamp is stale for several
  periods (configured via `NumMissedTableIAmAliveLimit`, default 3), new silos
  skip it during startup connectivity checks.  This lets a fresh cluster start
  even if the table contains rows from crashed silos that never cleaned up.

---

## 4. The Membership Table (`IMembershipTable`)

### 4.1 What It Stores

Each row represents one silo and contains:

| Field | Description |
|-------|-------------|
| Partition key / deployment ID | The cluster/deployment identifier |
| Row key / silo identity | `ip:port:epoch` (globally unique per silo instance) |
| Status | `Joining`, `Active`, `ShuttingDown`, `Dead` |
| Suspicion list | Timestamped entries: "at time T, silo S suspected this silo" |
| IAmAlive timestamp | Last heartbeat timestamp written by the silo itself |
| Start time | When the silo started |
| Host name | Machine/container hostname |
| Proxy port (gateway port) | For client gateway discovery |
| ETag / Version | For optimistic concurrency control |

A special **version row** (same partition key) holds the monotonically
increasing membership version number.

### 4.2 Consistency Requirements

The `IMembershipTable` implementation **must** provide:

1. **Optimistic concurrency control**: reads return an ETag/version; writes
   are conditional on that ETag/version matching.
2. **Atomic multi-row updates**: updating a silo row and the version row must
   be atomic (a single transaction or batch operation).
3. **Durability**: the table must survive process and machine failures.  Using
   volatile storage (e.g., Redis without persistence) risks cluster
   unavailability.

### 4.3 Official Membership Providers

| Provider | Package | Key / Concurrency mechanism |
|----------|---------|---------------------------|
| Azure Table Storage | `Microsoft.Orleans.Clustering.AzureStorage` | Partition key = deployment ID, row key = silo identity.  Uses Azure Table ETags.  Batch transactions for multi-row atomicity within a partition. |
| ADO.NET (SQL Server, PostgreSQL, MySQL/MariaDB, Oracle) | `Microsoft.Orleans.Clustering.AdoNet` | Composite key = (deploymentID, ip, port, epoch).  Uses database-generated ETags (ROWVERSION on SQL Server 2005+, NEWID() on 2000) and database transactions. |
| Apache ZooKeeper | `Microsoft.Orleans.Clustering.ZooKeeper` | Root node = deployment ID, child node = `ip:port@epoch`.  Uses ZK node versions for OCC.  `multi()` method for atomic operations. |
| HashiCorp Consul | `Microsoft.Orleans.Clustering.Consul` | Uses Consul Key/Value store. |
| AWS DynamoDB | `Microsoft.Orleans.Clustering.DynamoDB` | Partition key = deployment ID, range key = `ip-port-generation`.  Conditional writes on an ETag attribute. |
| Apache Cassandra | `Microsoft.Orleans.Clustering.Cassandra` | Composite partition key = (ServiceId, ClusterId), row key = `ip:port:epoch`.  Static column version with Lightweight Transactions for OCC.  Optional TTL-based cleanup of defunct silos. |
| Azure Cosmos DB | `Microsoft.Orleans.Clustering.Cosmos` | Configurable database and container names.  Supports auto-creation of resources. |
| Redis | `Microsoft.Orleans.Clustering.Redis` | Key format: `{ServiceId}/members/{ClusterId}`.  Requires persistence enabled. |
| In-memory (development only) | Built-in | Uses a special system grain on a designated primary silo.  **Not for production.** |

### 4.4 Design Rationale: Why Not Just Use ZooKeeper/etcd?

Orleans chose to implement its own membership protocol atop a pluggable table
for three reasons:

1. **Cloud deployment simplicity**: ZooKeeper is not a hosted service; Azure
   Table Storage (and similar) is fully managed.
2. **Direct failure detection**: ZK ephemeral nodes detect disconnection from
   ZK, not between Orleans silos.  Orleans probes correlate with actual
   silo-to-silo network health.
3. **Portability**: The `IMembershipTable` abstraction lets you swap backends
   without changing the protocol.

---

## 5. Consistent Hashing Ring for Probe Target Selection

Each silo's identity (`ip:port:epoch`) is hashed to position it on a virtual
ring (standard consistent hashing as used in Chord DHT, Amazon Dynamo, etc.).
Each silo then monitors its **X successor silos** on the ring (X =
`NumProbedSilos`).

This ensures:

- Monitoring responsibilities are evenly distributed.
- When a silo joins or leaves, only a small number of monitoring relationships
  change.
- The same ring concept is reused for the grain directory partitioning (see
  below).

---

## 6. The Grain Directory

### 6.1 Purpose

The grain directory is a distributed key-value store:

- **Key**: grain identity (type + key).
- **Value**: the `SiloAddress` of the silo hosting the active grain (its
  "activation").

It enforces the **single-activation guarantee**: for a given grain identity,
at most one activation exists in the cluster at any time (except for stateless
worker grains, which are exempt from the directory entirely).

### 6.2 Architecture

The directory is partitioned across all active silos using a **consistent hash
ring with virtual nodes**.  Each silo owns a configurable number of hash
ranges (default 30 per silo), similar to Amazon Dynamo and Apache Cassandra.
A grain ID is hashed to find the owning silo for that portion of the ring.

**Local caching**: Each silo maintains a local cache of directory lookups.
Cache entries are invalidated when the membership view changes or when a
lookup is found to be stale.  This means most grain calls resolve locally
without a remote directory read.

### 6.3 Grain Location Flow

When silo A wants to call grain G:

1. A checks its local directory cache.
2. If cache miss (or stale entry), A hashes G's identity to find the directory
   owner silo D.
3. A sends a lookup request to D.
4. D checks its partition:
   - If G is registered, D returns the hosting silo address.
   - If G is not registered, D selects a silo for activation using the grain's
     placement strategy, registers G -> selected silo, and returns the address.
5. A caches the result and sends the grain call to the hosting silo.
6. The hosting silo activates G if not already active, then delivers the call.

### 6.4 Strong Consistency (Orleans 9.0+)

Starting with Orleans 9.0, the default grain directory is a **strongly
consistent** distributed hash table based on the **Virtually Synchronous
methodology** (Microsoft Research) with similarities to **Vertical Paxos**.

Directory partitions operate in two modes:

1. **Normal operation**: requests processed locally without coordination.
2. **View change**: when cluster membership changes, partitions coordinate to
   transfer ownership of hash ranges.

All directory operations carry **view numbers**:

- Requests include the caller's view number.
- Responses include the partition's view number.
- Mismatches trigger synchronization; requests retry on view changes.

#### View Change Procedure

When membership changes (silo join or leave):

1. The previous owner **seals** the affected range and creates a snapshot.
2. The new owner requests and applies the snapshot.
3. The new owner begins servicing requests.
4. The previous owner deletes the snapshot.

Range locks ("wedges" in the Virtual Synchrony terminology) prevent invalid
access during the transition.

#### Recovery After Crashes

If a silo crashes without a clean handoff:

1. The new partition owner queries **all active silos** for their grain
   registrations.
2. It rebuilds directory state for the affected ranges.
3. This ensures no duplicate activations.

### 6.5 Earlier Directory (pre-9.0)

Before Orleans 9.0, the directory was an **eventually consistent** distributed
hash table.  It used the same consistent-hash-ring partitioning but without
the view-change coordination and strong consistency guarantees.

### 6.6 Pluggable Directory

The grain directory system is pluggable via the `IGrainDirectory` interface.
Custom implementations can use different storage backends or consistency
models.  Directory configuration can be set on a **per-grain-type** basis.

---

## 7. Grain Placement

When a grain needs to be activated and has no existing entry in the directory,
the runtime must choose a silo.  This is **grain placement**.

### 7.1 Built-in Placement Strategies

| Strategy | Attribute | Description |
|----------|-----------|-------------|
| Resource-optimized (default in 9.2+) | `[ResourceOptimizedPlacement]` | Scores silos by CPU usage, memory usage, available memory, and activation count (configurable weights).  Uses an adaptive Kalman-filter-based algorithm to smooth signals. |
| Random | `[RandomPlacement]` | Picks a random compatible silo.  Default before 9.2. |
| Prefer local | `[PreferLocalPlacement]` | Uses the local silo if compatible; otherwise random. |
| Hash-based | `[HashBasedPlacement]` | `hash(grainId) % numSilos` -- deterministic but not stable across membership changes. |
| Activation-count-based | `[ActivationCountBasedPlacement]` | Power-of-two-choices: randomly samples 2 silos, picks the one with fewer activations. |
| Stateless worker | `[StatelessWorker]` | Like prefer-local but allows multiple activations of the same grain per silo.  Not registered in the directory. |
| Silo-role-based | `[SiloRoleBasedPlacement]` | Deterministic placement on silos with a specific role. |
| Custom | Implement `IPlacementDirector` | Full control over placement logic. |

### 7.2 Placement Filtering (Orleans 9.0+)

Before the placement strategy selects a silo, **placement filters** can narrow
the set of eligible silos based on silo metadata (availability zone, tier,
hardware capabilities, etc.).

### 7.3 Activation Repartitioning (Experimental)

Monitors grain-to-grain communication patterns and migrates activations to
co-locate frequently communicating grains, improving call locality.

### 7.4 Activation Rebalancing (Experimental, Orleans 10.0+)

Cluster-wide coordination to redistribute activations based on memory usage
and activation count, ensuring even load across silos.

---

## 8. Silo Lifecycle: Startup and Shutdown Sequence

The silo uses an **observable lifecycle** pattern with ordered stages.  Stages
are executed in ascending order during startup and descending order during
shutdown.

### 8.1 Lifecycle Stages

| Stage constant | Value | Startup action |
|----------------|-------|----------------|
| `First` | `int.MinValue` | Earliest possible hook |
| `RuntimeInitialize` | 2000 | Initialize threading, basic runtime environment |
| `RuntimeServices` | 4000 | Start networking, initialize silo-to-silo and gateway listeners, start various agents |
| `RuntimeStorageServices` | 6000 | Initialize connections to storage providers |
| `RuntimeGrainServices` | 8000 | Start grain type management, membership service, grain directory |
| `ApplicationServices` | 10000 | Start application-layer services (grain storage providers, stream providers, reminders) |
| `BecomeActive` | 19999 | Silo joins the cluster (writes its row, validates connectivity to all existing silos) |
| `Active` | 20000 | Silo is fully active; ready to accept grain calls and client connections |
| `Last` | `int.MaxValue` | Latest possible hook |

### 8.2 Startup Sequence Detail

1. **Process starts**, builds the .NET Generic Host, calls `UseOrleans`.
2. The silo builder registers all services, providers, and lifecycle
   participants in the DI container.
3. Host starts, triggering the silo lifecycle.
4. Stages execute in ascending order.  At each stage, all registered
   participants for that stage run concurrently, and the stage completes when
   all participants finish.
5. At `BecomeActive`:
   - The silo writes its row (status = `Joining`) into the membership table.
   - It reads the full table to discover existing silos.
   - It validates two-way connectivity to every active silo (skipping silos
     whose `IAmAlive` timestamp is stale beyond `NumMissedTableIAmAliveLimit`
     periods).
   - If validation succeeds, it updates its status to `Active`.
6. At `Active`, the silo begins accepting grain calls and client connections.

### 8.3 Shutdown Sequence Detail

Shutdown executes stages in **descending** order:

1. At `Active` (stop): the silo stops accepting new grain activations.
2. At `BecomeActive` (stop): the silo marks itself as `ShuttingDown` in the
   membership table, then transitions to `Dead`.
3. Grain activations are deactivated (calling `OnDeactivateAsync` where
   possible).  Grain directory entries are cleaned up.
4. At `RuntimeServices` (stop): networking shuts down, connections close.
5. At `RuntimeInitialize` (stop): threading and runtime environment tear down.

If a silo crashes (process kill, hardware failure), the deactivation callbacks
do not run.  Other silos detect the failure via missed probes and the voting
protocol.

### 8.4 Application Participation

Application code participates in the silo lifecycle by registering
`ILifecycleParticipant<ISiloLifecycle>` in the DI container.  This allows
running custom startup/shutdown logic at any stage.

---

## 9. Client (External) Connections to the Cluster

### 9.1 Client Architecture

An Orleans **client** is a non-silo process that sends grain calls into the
cluster.  It connects through the **gateway endpoints** of silos.

### 9.2 Gateway Discovery

The client uses the same clustering provider (membership table) as the silos
to discover available gateways.  Specifically:

- The client reads the membership table to find all active silos.
- It filters for silos that have a configured gateway (proxy) port.
- It establishes TCP connections to one or more gateways.

Provider-specific client-side configuration methods:

| Provider | Client method |
|----------|---------------|
| Azure Table Storage | `UseAzureStorageClustering(...)` |
| Azure Cosmos DB | `UseCosmosGatewayListProvider(...)` |
| ADO.NET | `UseAdoNetClustering(...)` |
| Redis | `UseRedisClustering(...)` |
| Consul | `UseConsulClustering(...)` |

### 9.3 Client Configuration

```
builder.UseOrleansClient(clientBuilder =>
{
    clientBuilder
        .Configure<ClusterOptions>(options =>
        {
            options.ClusterId = "my-cluster";
            options.ServiceId = "MyService";
        })
        .UseAzureStorageClustering(options => ...);
});
```

The client must use the same `ClusterId` and `ServiceId` as the silos it
connects to.

### 9.4 Co-hosted Client

Orleans also supports **co-hosting** the client within the same process as a
silo.  In this mode, the client automatically connects to the local silo's
gateway without requiring external discovery.

### 9.5 Client-to-Gateway Communication

- Clients maintain connections to multiple gateways for fault tolerance.
- Grain calls from the client are routed through a gateway silo, which forwards
  them to the silo hosting the target grain (or activates the grain if needed).
- The gateway silo is not necessarily the silo hosting the grain -- it acts as
  an entry point into the cluster.
- Clients periodically refresh their gateway list from the membership table.

---

## 10. Properties of the Membership Protocol

The Orleans documentation highlights several important properties:

1. **Handles any number of failures**: Unlike Paxos-based group membership
   (which requires a majority quorum), Orleans membership works even if more
   than half the silos fail, including full cluster restart.

2. **Light table traffic**: Probes go directly between silos, not through the
   table.  The table is only written to for joins, suspicions, and death
   declarations.

3. **Tunable accuracy vs. completeness**: Adjusting `NumVotesForDeathDeclaration`,
   `NumMissedProbesLimit`, and `DeathVoteExpirationTimeout` lets you trade off
   between false positives (declaring a live silo dead) and detection speed
   (how quickly dead silos are removed).

4. **Scalable**: Designed for thousands of silos.  The main scalability
   bottleneck is version-row contention in the membership table.

5. **Diagnostic-friendly**: The membership table provides a real-time view of
   cluster state including history of suspicions and deaths.

6. **Reliable storage required**: The table must be durable.  It serves as both
   a rendezvous point and a consensus mechanism (outsourcing distributed
   consensus to the cloud storage platform's conditional-write primitives).

---

## 11. Summary: Key Concepts for a Python Implementation

For building a Python Orleans-compatible or Orleans-inspired system, the
critical components to implement are:

### Membership Table
- A pluggable key-value store with optimistic concurrency control (ETags or
  versioned rows).
- Atomic multi-row updates (silo row + version row).
- Schema: silo identity, status, suspicion list, IAmAlive timestamp, gateway
  port, ETag.

### Membership Protocol
- Consistent-hash ring for assigning monitoring responsibilities.
- Periodic direct probes (TCP or application-level heartbeats).
- Suspicion accumulation with configurable voting threshold and expiration.
- Self-monitoring with health scoring to avoid unhealthy nodes voting out
  healthy ones.
- Indirect probing via randomly-selected intermediaries.
- Snapshot broadcast after table writes for fast propagation.
- Monotonically increasing membership version for total ordering.

### Grain Directory
- Distributed hash table partitioned across silos using consistent hashing
  with virtual nodes (default 30 per silo).
- Local lookup cache on each silo.
- Strong consistency via view-change coordination (seal, snapshot, transfer).
- Recovery protocol for crash scenarios.

### Grain Placement
- Pluggable strategy pattern.
- Default: resource-optimized (CPU + memory weighted scoring).
- Alternatives: random, prefer-local, hash-based, activation-count-based.

### Silo Lifecycle
- Ordered startup stages (RuntimeInitialize -> RuntimeServices ->
  RuntimeStorageServices -> RuntimeGrainServices -> ApplicationServices ->
  BecomeActive -> Active).
- Reverse-order shutdown.
- Connectivity validation before becoming active.

### Client Gateway
- Clients discover gateways by reading the membership table.
- Clients connect to gateway endpoints on silos and send grain calls through
  them.
- Gateway silos forward calls to the appropriate hosting silo.

---

## References

- Microsoft Learn: Orleans Cluster Management
  https://learn.microsoft.com/en-us/dotnet/orleans/implementation/cluster-management
- Microsoft Learn: Orleans Grain Directory
  https://learn.microsoft.com/en-us/dotnet/orleans/implementation/grain-directory
- Microsoft Learn: Orleans Silo Lifecycle
  https://learn.microsoft.com/en-us/dotnet/orleans/host/silo-lifecycle
- Microsoft Learn: Orleans Server Configuration
  https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/server-configuration
- Microsoft Learn: Orleans Client Configuration
  https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/client-configuration
- Microsoft Learn: Orleans Grain Placement
  https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-placement
- Microsoft Learn: Orleans Grain Lifecycle
  https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-lifecycle
- Microsoft Learn: Orleans Overview
  https://learn.microsoft.com/en-us/dotnet/orleans/overview
- Orleans GitHub Repository
  https://github.com/dotnet/orleans
- Original Research Paper: "Orleans: Distributed Virtual Actors for
  Programmability and Scalability", Bykov et al., MSR-TR-2014-41
  https://www.microsoft.com/en-us/research/publication/orleans-distributed-virtual-actors-for-programmability-and-scalability/
- Lifeguard (Hashicorp): "Lifeguard: Local Health Awareness for More Accurate
  Failure Detection"
  https://arxiv.org/abs/1707.00788
- Virtually Synchronous Methodology
  https://www.microsoft.com/en-us/research/publication/virtually-synchronous-methodology-for-dynamic-service-replication/
