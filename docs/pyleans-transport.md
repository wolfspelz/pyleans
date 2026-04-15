# Pyleans Transport Layer Proposal

A comprehensive analysis of pluggable transport options for pyleans, a Python
implementation of Orleans-style virtual actors. This document covers two
distinct use cases -- intra-cluster (silo-to-silo) transport and gateway
(client-to-cluster) transport -- and evaluates protocol candidates against
the required feature set.

---

## Table of Contents

1. [Required Features Analysis](#1-required-features-analysis)
2. [Protocol Candidates](#2-protocol-candidates)
3. [Feature Matrix](#3-feature-matrix)
4. [Custom TCP Mesh Transport Design](#4-custom-tcp-mesh-transport-design)
5. [MQTT as Transport -- Deep Dive](#5-mqtt-as-transport----deep-dive)
6. [Abstract Transport Interface](#6-abstract-transport-interface)
7. [Recommendation](#7-recommendation)

---

## 1. Required Features Analysis

Pyleans requires transport for two fundamentally different communication
patterns. The table below lists each required feature and describes how it
applies to intra-cluster versus gateway transport.

### 1.1 Reliability / Delivery Guarantees

**Intra-cluster**: At-most-once delivery is the baseline (matching Orleans
semantics). The caller holds an `asyncio.Future` with a timeout; if the
response does not arrive, the future is faulted with `TimeoutException`. No
automatic retries at the transport level -- the application layer decides
whether to retry. The transport must guarantee that bytes accepted for
sending are delivered in order and without corruption (i.e., TCP-level
reliability), but message-level acknowledgment is handled by the RPC
correlation layer, not the transport.

**Gateway**: Same at-most-once semantics. However, gateway connections may
traverse unreliable networks (Wi-Fi, cellular, the public internet), so the
transport must handle connection drops gracefully. Reconnection and
re-establishment of pending state is the responsibility of the client library,
but the transport should signal connection loss promptly.

### 1.2 Ordering Guarantees

**Intra-cluster**: Messages between a given silo pair must be delivered in the
order they were sent on that connection. This is naturally provided by TCP.
There is no requirement for global ordering across different silo pairs.

**Gateway**: Messages from a single client to the cluster must maintain order
on a per-connection basis. Multiple clients are independent.

### 1.3 Bidirectional Communication (Request/Response Correlation)

**Intra-cluster**: Every connection is bidirectional. Silo A sends requests to
silo B and receives responses on the same connection, and vice versa. Each
message carries a correlation ID (message ID). The sending side maintains a
dictionary of `{correlation_id: asyncio.Future}` and completes the future when
the response arrives.

**Gateway**: The client sends requests and receives responses. Additionally,
the cluster may push notifications to the client (grain observers, streaming).
The transport must support both request/response and server-initiated push on
the same connection.

### 1.4 Multiplexing

**Intra-cluster**: A single connection between two silos carries all grain
calls, directory operations, membership probes, and system messages
concurrently. Messages are interleaved at the message level (each message is a
complete, length-prefixed frame). This avoids head-of-line blocking at the
application level, though TCP head-of-line blocking at the byte stream level
remains.

**Gateway**: A single client connection carries multiple concurrent grain
calls. The same multiplexing approach applies.

### 1.5 Connection Management

**Intra-cluster**: Connections are established lazily on first need and
maintained for the lifetime of both silos. The transport must support:
- **Discovery**: Silo addresses are obtained from the membership table, not
  from the transport layer. The transport receives a target address and
  connects.
- **Reconnection**: If a connection drops, the transport attempts to
  reconnect with exponential backoff. Pending requests on the lost connection
  are failed immediately (not silently retried).
- **Health checks**: Membership protocol probes flow over the same
  connections. The transport should also support TCP-level keepalive or
  application-level ping/pong to detect half-open connections.

**Gateway**: Clients discover gateways via the membership table. The transport
must support:
- **Connection to multiple gateways** for fault tolerance.
- **Gateway list refresh** -- periodically re-reading the gateway list and
  connecting to new gateways / disconnecting from removed ones.
- **Reconnection** with backoff on connection loss.

### 1.6 Serialization Framing

**Intra-cluster and gateway**: The transport is responsible for framing -- it
must deliver complete messages to the application layer. The standard approach
is length-prefixed binary:

```
[4 bytes: header length][4 bytes: body length][header bytes][body bytes]
```

The transport reads the 8-byte prefix, then reads exactly `header_length +
body_length` bytes, and delivers the complete message. The content of header
and body bytes is opaque to the transport -- serialization format is a
separate concern.

### 1.7 Backpressure

**Intra-cluster**: If silo B is slow to process messages, silo A must not
overwhelm B's buffers. Backpressure mechanisms:
- **TCP flow control**: The OS-level TCP window naturally applies backpressure.
- **Application-level**: A semaphore or bounded queue on the sending side
  limits the number of in-flight (unacknowledged) messages. When the limit is
  reached, new `send_request` calls block (await) until a slot opens.
- **Read-side**: The asyncio reader task drains the socket into a bounded
  queue. If the processing loop falls behind, the reader blocks, which causes
  the TCP window to shrink, propagating backpressure to the sender.

**Gateway**: Same mechanisms apply. Additionally, the gateway silo should
limit the number of concurrent in-flight requests from a single client to
prevent a single client from monopolizing resources.

### 1.8 Security

**Intra-cluster**: TLS is strongly recommended for production deployments.
Mutual TLS (mTLS) with certificate-based authentication ensures that only
authorized silos can join the mesh. The transport interface should accept
an `ssl.SSLContext` for both client and server sides.

**Gateway**: TLS is mandatory for production (data in transit from external
clients). Authentication is a separate concern (tokens, certificates) but the
transport must support TLS and should allow pluggable authentication during
the handshake or as a first-message exchange.

### 1.9 NAT Traversal

**Intra-cluster**: Typically not needed. Silos run in the same network
(data center, VPC, Kubernetes cluster) with direct reachability. If NAT is
involved (multi-region), the silo must advertise its externally-reachable
address via `EndpointOptions.AdvertisedIPAddress`.

**Gateway**: Often needed. Clients may be behind NAT, firewalls, or corporate
proxies. The transport should support:
- Client-initiated connections (client connects to gateway, not vice versa).
- Protocols that traverse HTTP proxies (WebSocket, HTTP/2, MQTT over
  WebSocket).
- Long-lived connections that survive NAT timeout (via keepalive).

### 1.10 Fan-in Scalability

**Intra-cluster**: Not a major concern. The number of connections per silo
equals the number of other silos (full mesh). For a 100-silo cluster, each
silo has 99 connections -- well within OS limits.

**Gateway**: Critical. A single gateway silo may serve thousands or tens of
thousands of client connections. The transport must:
- Use non-blocking I/O (asyncio).
- Minimize per-connection memory overhead.
- Support connection limits and load shedding.
- Avoid patterns that require one OS thread per connection.

### 1.11 Broadcast / Multicast Capability

**Intra-cluster**: Used for membership snapshot broadcasts after table writes.
The current design sends unicast messages to all known silos (application-level
broadcast over the mesh). True multicast is not required but a transport that
natively supports pub/sub can simplify this.

**Gateway**: Not typically needed. Client-directed communication is unicast.
Streaming (grain observers, pub/sub) is handled at the application layer.

---

## 2. Protocol Candidates

### 2.1 Custom TCP Mesh

**What it is**: A hand-built networking layer using raw TCP sockets
(`asyncio.open_connection` / `asyncio.start_server`). Each silo maintains a
persistent, multiplexed TCP connection to every other silo in the cluster
(full mesh topology). Messages are length-prefixed binary frames. This is
essentially what Orleans does internally.

**Strengths**:
- Zero external dependencies.
- Maximum control over framing, backpressure, and connection lifecycle.
- Lowest possible latency (no intermediary, no protocol overhead beyond TCP).
- Proven architecture (Orleans production at scale).
- Naturally fits asyncio with `StreamReader`/`StreamWriter`.
- Supports TLS via `ssl` module directly.

**Weaknesses**:
- Significant implementation effort (connection management, reconnection,
  health checks, framing, error handling all must be built from scratch).
- Full mesh does not scale well beyond ~200 silos (`O(n^2)` connections).
- No built-in NAT traversal -- clients behind NAT need additional handling.
- No broker means no message persistence or replay capability.

**Intra-cluster fit**: Excellent. This is the reference architecture.

**Gateway fit**: Good for same-network clients. Poor for NAT traversal
without wrapping in WebSocket or HTTP.

### 2.2 MQTT v5

**What it is**: A lightweight pub/sub messaging protocol designed for IoT and
constrained devices. MQTT v5 adds features critical for RPC patterns:
response topics, correlation data, message expiry, user properties, and
shared subscriptions. Requires a broker (e.g., Mosquitto, EMQX, HiveMQ).

**Strengths**:
- Designed for unreliable networks with small overhead (2-byte minimum header).
- Three QoS levels: 0 (at-most-once), 1 (at-least-once), 2 (exactly-once).
- Native NAT traversal -- clients connect outbound to the broker.
- MQTT v5 response topics and correlation data enable request/response.
- Ubiquitous in IoT -- wide library support (aiomqtt, paho-mqtt).
- Broker provides natural fan-in (thousands of clients to one broker).
- Supports TLS and username/password or certificate auth.
- Retained messages can serve as a simple discovery mechanism.
- Shared subscriptions enable load balancing across silos.

**Weaknesses**:
- Requires an external broker (additional infrastructure dependency).
- Broker is a single point of failure (unless clustered, adding complexity).
- Higher latency than direct TCP (every message goes through the broker).
- Topic-based routing does not naturally map to point-to-point RPC.
- QoS 1 and 2 add significant overhead (acknowledgment round trips).
- No native multiplexing -- each message is independent (though the TCP
  connection to the broker is shared).
- Ordering is per-topic only, not across topics.
- Message size limits vary by broker (typically 256 MB, but practical limits
  are much lower for IoT brokers).

**Intra-cluster fit**: Possible but suboptimal. The broker adds latency and
becomes a bottleneck for high-throughput silo-to-silo communication.

**Gateway fit**: Good, especially for IoT scenarios. MQTT is the natural
protocol for constrained devices and NAT-traversing clients.

### 2.3 ZeroMQ (zmq)

**What it is**: A brokerless messaging library providing socket-like
abstractions (REQ/REP, PUB/SUB, PUSH/PULL, DEALER/ROUTER) over TCP, IPC,
inproc, or multicast. It handles framing, reconnection, and fair queuing
internally.

**Strengths**:
- No broker required -- direct peer-to-peer communication.
- Very high throughput and low latency.
- Rich socket patterns (ROUTER/DEALER for async request/reply with
  multiplexing).
- Built-in reconnection and message queuing.
- Multi-part messages (frames) support natural header/body separation.
- Cross-platform, mature library (pyzmq with asyncio support).

**Weaknesses**:
- Complex API with many socket types and subtle semantics.
- No built-in TLS (requires CurveZMQ for encryption, which is less standard
  than TLS).
- No built-in authentication beyond CurveZMQ.
- Broker-free means each silo must manage its own connections (similar to
  custom TCP, but with zmq handling some of the low-level details).
- Less suitable for NAT traversal (requires direct connectivity or a proxy).
- Poor support for constrained IoT devices.
- pyzmq asyncio integration has some rough edges.

**Intra-cluster fit**: Good. ROUTER/DEALER sockets provide multiplexed
async request/reply. Avoids much of the boilerplate of custom TCP.

**Gateway fit**: Possible but not ideal. No NAT traversal, no standard
browser support.

### 2.4 WebSocket

**What it is**: A protocol providing full-duplex communication over a single
TCP connection, upgraded from an initial HTTP handshake. Defined in RFC 6455.

**Strengths**:
- Full-duplex bidirectional communication.
- Traverses NAT, firewalls, and HTTP proxies (initial handshake is HTTP).
- Native browser support (JavaScript `WebSocket` API).
- Simple framing (opcode, length, payload).
- TLS via `wss://`.
- Excellent Python support (`websockets` library, fully asyncio-native).
- Low overhead after the initial handshake.

**Weaknesses**:
- No built-in multiplexing -- application must implement its own correlation
  and multiplexing on top of WebSocket frames.
- No built-in request/reply pattern.
- No backpressure beyond TCP flow control (application must manage).
- Single TCP connection means TCP head-of-line blocking.
- HTTP upgrade handshake adds startup latency.
- Some corporate proxies/firewalls interfere with WebSocket connections.

**Intra-cluster fit**: Possible but unnecessary. Adds HTTP upgrade overhead
with no benefit over raw TCP for same-network communication.

**Gateway fit**: Excellent for web/browser clients. The natural choice when
clients run in browsers. Good for mobile clients as well.

### 2.5 Redis Pub/Sub + Streams

**What it is**: Redis provides two messaging primitives: Pub/Sub (fire-and-forget
fan-out) and Streams (persistent, consumer-group-based message log).

**Strengths**:
- Redis is already a common infrastructure component.
- Pub/Sub provides simple fan-out for broadcast messages.
- Streams provide persistence, consumer groups, acknowledgment, and replay.
- Low latency (Redis is in-memory).
- Can double as membership table backend.
- Good Python support (`redis-py` with asyncio, `aioredis`).

**Weaknesses**:
- No built-in request/reply pattern (must be implemented with two
  channels/streams and correlation IDs).
- Pub/Sub is fire-and-forget (no delivery guarantees, messages lost if
  subscriber is disconnected).
- Streams add durability but with different semantics (consumer groups,
  explicit ack).
- Redis is a single point of failure unless using Redis Cluster or Sentinel.
- Every message goes through Redis -- adds latency compared to direct
  connections.
- Not designed for high-throughput RPC patterns.
- No NAT traversal benefit for clients.

**Intra-cluster fit**: Poor for primary transport. Adequate as a supplementary
broadcast channel (membership updates, grain directory invalidation).

**Gateway fit**: Poor. Not designed for client-facing communication.

### 2.6 AMQP (RabbitMQ)

**What it is**: Advanced Message Queuing Protocol, typically used with
RabbitMQ. Provides exchanges, queues, bindings, and consumer patterns with
strong delivery guarantees.

**Strengths**:
- Mature, well-understood messaging semantics.
- Flexible routing (direct, topic, fanout, headers exchanges).
- Durable queues with at-least-once delivery and acknowledgment.
- Dead letter queues for error handling.
- Good Python support (`aio-pika` for asyncio).
- RabbitMQ supports clustering and high availability.
- Supports TLS and SASL authentication.

**Weaknesses**:
- Heavy infrastructure (RabbitMQ is a complex system to operate).
- High latency compared to direct connections (broker intermediary).
- Not designed for low-latency RPC (designed for decoupled messaging).
- Queue-per-silo pattern consumes significant broker resources.
- Request/reply requires temporary reply queues (RabbitMQ has Direct Reply-to
  optimization but it adds complexity).
- Overkill for the pyleans use case.
- No NAT traversal benefit beyond broker connectivity.

**Intra-cluster fit**: Poor. Too heavy and high-latency for silo-to-silo RPC.

**Gateway fit**: Poor for interactive RPC. Adequate if clients are services
that send fire-and-forget commands.

### 2.7 Zenoh

**What it is**: A zero-overhead pub/sub, store/query, and compute protocol
originally developed at ADLINK (now Eclipse Foundation). Zenoh unifies
data in motion (streams), data at rest (storage), and computations. It is
designed for edge-to-cloud scenarios with a peer-to-peer or routed topology.
The protocol can run over TCP, UDP, WebSocket, serial, or shared memory,
and supports automatic peer discovery via multicast scouting.

**Strengths**:
- **Zero-copy, zero-overhead design**: Minimal wire overhead, suitable for
  constrained devices and high-throughput scenarios alike.
- **Flexible topology**: Peer-to-peer (no broker), client-router, or
  full mesh with routers. Silos could connect peer-to-peer for intra-cluster
  and use a router for gateway fan-in.
- **Built-in request/reply** (`get` / `queryable`): native request/response
  pattern without needing to build correlation on top of pub/sub.
- **Key-expression based routing**: Hierarchical key expressions with
  wildcards (e.g., `pyleans/cluster1/silo/*/rpc`) -- similar to MQTT topics
  but more expressive.
- **Transport-agnostic**: Runs over TCP, UDP, TLS, WebSocket, shared memory,
  and serial links. Transport is pluggable at the Zenoh level itself.
- **Automatic discovery**: Multicast scouting finds peers on the local
  network without configuration.
- **Built-in storage**: Zenoh can store values associated with key
  expressions -- potentially usable for membership table or grain directory.
- **Low latency**: In peer-to-peer mode, latency is comparable to raw TCP
  (no broker hop). Router mode adds a single hop.
- **Edge-native**: Designed for IoT/edge with support for constrained
  devices, intermittent connectivity, and bandwidth-limited links.
- **Good Python support**: `zenoh-python` (based on the Rust implementation
  via PyO3) with async API.
- **Shared memory transport**: For same-host communication, Zenoh can use
  shared memory for near-zero-copy message passing between processes.

**Weaknesses**:
- **Younger ecosystem** than MQTT or ZeroMQ. Smaller community, fewer
  production references (though Eclipse Foundation adoption is growing).
- **Router is optional but recommended** for larger deployments -- adds
  operational complexity similar to a broker, though lighter.
- **Python bindings are Rust-backed** (via PyO3) -- excellent performance
  but adds a compiled dependency (no pure-Python fallback).
- **Less standardized** than MQTT (no formal OASIS/IETF standard yet,
  though an IETF draft exists for the Zenoh protocol).
- **QoS model is simpler** than MQTT: reliable (ordered, lossless over TCP)
  or best-effort (UDP). No per-message QoS negotiation.
- **Smaller pool of developers** familiar with Zenoh compared to MQTT.

**Intra-cluster fit**: Excellent. Peer-to-peer mode gives direct silo-to-silo
communication without a broker. Built-in request/reply maps naturally to grain
RPC. Key-expression routing provides flexible addressing. Shared memory
transport is a built-in optimization for same-host silos. Automatic discovery
via scouting could simplify membership for development/small deployments.

**Gateway fit**: Very good. Router mode provides fan-in from many clients.
WebSocket transport enables browser clients. The edge-native design with
support for constrained devices and intermittent connectivity makes it a
strong alternative to MQTT for IoT scenarios -- potentially replacing both
MQTT gateway and TCP mesh with a single protocol.

### 2.8 Unix Domain Sockets

**What it is**: An IPC mechanism for communication between processes on the
same host. Uses filesystem paths instead of IP:port. Provides stream-oriented
(SOCK_STREAM) or datagram-oriented (SOCK_DGRAM) communication.

**Strengths**:
- Zero network overhead (no TCP/IP stack, no loopback interface).
- Lower latency than TCP loopback (kernel bypasses network stack).
- Higher throughput than TCP for same-host communication.
- Works with asyncio (`open_unix_connection`).
- Can use file permissions for access control.

**Weaknesses**:
- Same-host only -- cannot be used for cross-machine communication.
- Not available on Windows (Windows has named pipes, which are different).
  Note: Windows support may be needed for development scenarios, though
  production pyleans deployments will likely run on Linux.
- Not useful for gateway transport (clients are typically remote).

**Intra-cluster fit**: Excellent as an optimization for same-host
communication (e.g., multiple silo processes on one machine, or silo +
co-hosted client). Should be a transparent optimization that the transport
layer selects when source and destination are on the same host.

**Gateway fit**: Only for co-hosted clients (same process or same machine).

---

## 3. Feature Matrix

### 3.1 Intra-Cluster Transport Feature Matrix

| Feature | Custom TCP | MQTT v5 | ZeroMQ | WebSocket | Redis | AMQP | Zenoh | UDS |
|---|---|---|---|---|---|---|---|---|
| Reliability (TCP-level) | Yes | Yes (via broker) | Yes | Yes | Yes (via server) | Yes (via broker) | Yes (TCP mode) | Yes |
| Ordering (per-connection) | Yes | Per-topic | Yes | Yes | Per-stream | Per-queue | Yes (per-key) | Yes |
| Bidirectional | Yes | Via response topics | ROUTER/DEALER | Yes | Two channels | Reply queues | get/queryable | Yes |
| Multiplexing | App-level | Via topics | Built-in | App-level | N/A | N/A | Built-in (key-expr) | App-level |
| Backpressure | TCP + app | QoS flow | HWM | TCP only | None | Consumer prefetch | Congestion control | TCP + app |
| TLS | Yes (ssl) | Yes | CurveZMQ | Yes (wss) | Yes | Yes | Yes (TLS + Quic) | N/A (local) |
| Mutual auth | mTLS | Cert + user/pass | CurveZMQ | mTLS | ACL + TLS | SASL + TLS | TLS + user/pass | File perms |
| No broker needed | Yes | No | Yes | Yes | No | No | Yes (P2P) / Optional router | Yes |
| Latency | Lowest | Higher (broker hop) | Lowest | Low | Medium | Higher | Lowest (P2P) / Low (router) | Lowest |
| Throughput | Highest | Limited by broker | Highest | High | Limited by Redis | Limited by broker | Very high | Highest |
| Implementation effort | High | Medium | Medium | Medium | Medium | Medium | Low-medium | Low |
| Python asyncio support | Native | aiomqtt | pyzmq | websockets | redis-py | aio-pika | zenoh-python | Native |

### 3.2 Gateway Transport Feature Matrix

| Feature | Custom TCP | MQTT v5 | ZeroMQ | WebSocket | Redis | AMQP | Zenoh | UDS |
|---|---|---|---|---|---|---|---|---|
| NAT traversal | Poor | Good | Poor | Good (HTTP) | Poor | Poor | Good (router mode) | N/A |
| Firewall friendly | Poor | Good (port 8883) | Poor | Good (443) | Poor | Poor | Good (WS transport) | N/A |
| Browser support | No | No (needs WS bridge) | No | Native | No | No | Yes (WS transport) | No |
| IoT device support | Poor | Excellent | Poor | Moderate | Poor | Poor | Very good (edge-native) | N/A |
| Fan-in (10K+ clients) | Good (asyncio) | Excellent (broker) | Good | Good (asyncio) | Limited | Limited | Excellent (router) | N/A |
| Mobile support | Poor | Good | Poor | Good | Poor | Poor | Good | N/A |
| Constrained devices | Poor | Excellent | Poor | Moderate | Poor | Poor | Good | N/A |
| Server push | Yes | Yes (pub/sub) | Yes | Yes | Yes (pub/sub) | Yes | Yes (pub/sub) | Yes |

---

## 4. Custom TCP Mesh Transport Design

This section details the design of the default intra-cluster transport: a
custom TCP mesh.

### 4.1 Connection Establishment and Handshake

When silo A needs to communicate with silo B for the first time:

1. **DNS/address resolution**: Silo A obtains silo B's address (`host:port`)
   from the membership table.

2. **TCP connection**: Silo A calls `asyncio.open_connection(host, port,
   ssl=ssl_context)`. If TLS is configured, the SSL handshake occurs during
   connection establishment.

3. **Silo identity handshake**: After the TCP (and optionally TLS) connection
   is established, both sides exchange a handshake message:

   ```
   Handshake message format:
   [4 bytes: magic number 0x504C4E53 ("PLNS")]
   [2 bytes: protocol version (uint16, currently 1)]
   [4 bytes: cluster_id length]
   [N bytes: cluster_id (UTF-8)]
   [4 bytes: silo_id length]
   [N bytes: silo_id (UTF-8, format: "ip:port:epoch")]
   ```

   The initiating side (A) sends first, then B responds. Both sides validate:
   - Magic number matches (ensures both sides speak pyleans protocol).
   - Protocol version is compatible.
   - Cluster ID matches (prevents cross-cluster connections).
   - Silo ID matches the expected peer (prevents MITM in non-TLS mode).

4. **Handshake completion**: If validation passes, the connection enters the
   active state. If validation fails, the connection is closed with an error
   logged.

**Accepting connections**: Each silo runs an `asyncio.start_server` listener.
When a new connection arrives, the silo waits for the initiator's handshake,
validates it, sends its own handshake, and registers the connection.

**Connection deduplication**: If both silos A and B simultaneously try to
connect to each other, a tie-breaking rule prevents duplicate connections:
the silo with the lexicographically smaller silo_id keeps its initiated
connection and closes the accepted one.

### 4.2 Full Mesh Topology Management

```
SiloConnectionManager:
  connections: dict[SiloAddress, SiloConnection]
  pending_connections: dict[SiloAddress, asyncio.Lock]  # prevent concurrent connect attempts
  membership_table: IMembershipTable

  on_membership_change(new_view: MembershipView):
      # Connect to new silos
      for silo in new_view.active_silos:
          if silo not in self.connections and silo != self.local_silo:
              asyncio.create_task(self._connect_to(silo))

      # Disconnect from dead/removed silos
      for silo in list(self.connections):
          if silo not in new_view.active_silos:
              await self._disconnect_from(silo)

  _connect_to(silo: SiloAddress):
      async with self.pending_connections[silo]:
          if silo in self.connections:
              return  # already connected (e.g., incoming connection arrived)
          reader, writer = await asyncio.open_connection(silo.host, silo.port, ssl=...)
          conn = SiloConnection(reader, writer, silo)
          await conn.handshake(self.local_silo)
          self.connections[silo] = conn
          asyncio.create_task(conn.read_loop())
```

Connections are established lazily (on first message) or eagerly (on
membership change), configurable by the operator. Eager connection
establishment is preferred for production to front-load connection setup
costs and detect unreachable silos early.

### 4.3 Message Framing Format

Every message on the wire uses length-prefixed binary framing:

```
Wire format:
[4 bytes: total frame length (uint32 big-endian, excludes these 4 bytes)]
[1 byte:  message type (Request=0x01, Response=0x02, OneWay=0x03,
                         Ping=0x04, Pong=0x05, Error=0x06)]
[8 bytes: correlation ID (uint64 big-endian)]
[4 bytes: header length (uint32 big-endian)]
[H bytes: header payload (serialized message headers)]
[B bytes: body payload (serialized message body, length = total - 13 - H)]
```

The total frame length is read first, then exactly that many bytes are read
to obtain the complete frame. This allows the reader to handle partial reads
gracefully.

**Maximum frame size**: Configurable, default 16 MB. Frames exceeding this
limit cause the connection to be closed with an error. Grain calls with
larger payloads should use streaming.

**Byte order**: Big-endian (network byte order) for all integer fields.

### 4.4 Request/Response Correlation

```python
class PendingRequests:
    def __init__(self, default_timeout: float = 30.0):
        self._pending: dict[int, asyncio.Future] = {}
        self._counter: int = 0
        self._lock = asyncio.Lock()
        self._default_timeout = default_timeout

    async def create_request(self, timeout: float | None = None) -> tuple[int, asyncio.Future]:
        async with self._lock:
            self._counter += 1
            correlation_id = self._counter
        future = asyncio.get_event_loop().create_future()
        self._pending[correlation_id] = future
        effective_timeout = timeout or self._default_timeout
        # Schedule timeout cancellation
        asyncio.get_event_loop().call_later(
            effective_timeout,
            self._timeout_request,
            correlation_id,
        )
        return correlation_id, future

    def complete_request(self, correlation_id: int, result: Any) -> None:
        future = self._pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(result)

    def fail_request(self, correlation_id: int, error: Exception) -> None:
        future = self._pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_exception(error)

    def fail_all(self, error: Exception) -> None:
        """Called when a connection is lost."""
        for cid, future in self._pending.items():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _timeout_request(self, correlation_id: int) -> None:
        future = self._pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_exception(TimeoutError(
                f"Request {correlation_id} timed out"
            ))
```

### 4.5 Multiplexing on a Single Connection

A single `SiloConnection` carries all message types concurrently:

```python
class SiloConnection:
    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter,
                 remote_silo: SiloAddress):
        self._reader = reader
        self._writer = writer
        self._remote_silo = remote_silo
        self._pending = PendingRequests()
        self._write_lock = asyncio.Lock()
        self._write_semaphore = asyncio.Semaphore(1000)  # max in-flight

    async def send_request(self, header: bytes, body: bytes,
                           timeout: float | None = None) -> bytes:
        await self._write_semaphore.acquire()
        try:
            correlation_id, future = await self._pending.create_request(timeout)
            frame = self._build_frame(MessageType.REQUEST, correlation_id,
                                      header, body)
            async with self._write_lock:
                self._writer.write(frame)
                await self._writer.drain()
            return await future
        finally:
            self._write_semaphore.release()

    async def send_one_way(self, header: bytes, body: bytes) -> None:
        frame = self._build_frame(MessageType.ONE_WAY, 0, header, body)
        async with self._write_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def read_loop(self) -> None:
        try:
            while True:
                frame_length_bytes = await self._reader.readexactly(4)
                frame_length = int.from_bytes(frame_length_bytes, 'big')
                frame_data = await self._reader.readexactly(frame_length)
                msg_type = frame_data[0]
                correlation_id = int.from_bytes(frame_data[1:9], 'big')
                header_length = int.from_bytes(frame_data[9:13], 'big')
                header = frame_data[13:13 + header_length]
                body = frame_data[13 + header_length:]

                if msg_type == MessageType.RESPONSE:
                    self._pending.complete_request(correlation_id, (header, body))
                elif msg_type == MessageType.REQUEST:
                    asyncio.create_task(
                        self._dispatch_request(correlation_id, header, body)
                    )
                elif msg_type == MessageType.ONE_WAY:
                    asyncio.create_task(
                        self._dispatch_one_way(header, body)
                    )
                elif msg_type == MessageType.PING:
                    await self._send_pong(correlation_id)
                elif msg_type == MessageType.PONG:
                    self._pending.complete_request(correlation_id, None)
        except asyncio.IncompleteReadError:
            self._on_connection_lost()
        except Exception as e:
            self._on_connection_error(e)

    def _on_connection_lost(self) -> None:
        self._pending.fail_all(ConnectionError(
            f"Connection to {self._remote_silo} lost"
        ))
```

The key design decisions:
- **Write lock**: A single `asyncio.Lock` serializes writes. Since asyncio is
  single-threaded, this prevents frame interleaving at the byte level.
  `writer.write()` buffers in memory; `drain()` flushes to the OS.
- **Read loop**: A dedicated coroutine continuously reads frames. Responses
  are matched to pending futures. Requests are dispatched to the message
  handler as new tasks.
- **Semaphore**: Limits the number of in-flight requests to prevent unbounded
  memory growth if the remote silo is slow.

### 4.6 Health Checks / Keepalive

Two levels of health checking:

**TCP keepalive**: Enabled on the socket to detect half-open connections:

```python
sock = writer.get_extra_info('socket')
sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
```

**Application-level ping/pong**: The membership protocol sends probe messages
over the same connection. These serve as application-level keepalive. If the
connection is idle (no messages in either direction for a configurable
interval, default 30 seconds), the transport sends a PING frame. If no PONG
is received within 10 seconds, the connection is considered dead.

```python
async def _keepalive_loop(self) -> None:
    while self._connected:
        await asyncio.sleep(self._keepalive_interval)
        if self._last_activity + self._keepalive_interval < time.monotonic():
            try:
                correlation_id, future = await self._pending.create_request(
                    timeout=10.0
                )
                ping_frame = self._build_frame(
                    MessageType.PING, correlation_id, b'', b''
                )
                async with self._write_lock:
                    self._writer.write(ping_frame)
                    await self._writer.drain()
                await future  # wait for PONG
            except (TimeoutError, ConnectionError):
                self._on_connection_lost()
                return
```

### 4.7 Reconnection Strategy

When a connection is lost:

1. **Immediate notification**: All pending requests on the connection are
   failed with `ConnectionError`.
2. **Membership check**: The reconnection logic checks the membership table.
   If the remote silo is still active, reconnection proceeds. If the remote
   silo is dead, no reconnection is attempted and the connection entry is
   removed.
3. **Exponential backoff**: Reconnection attempts use exponential backoff
   with jitter:

   ```python
   async def _reconnect_loop(self, silo: SiloAddress) -> None:
       base_delay = 0.1  # 100ms initial delay
       max_delay = 30.0  # 30 seconds max
       attempt = 0
       while silo in self._active_silos:
           delay = min(base_delay * (2 ** attempt), max_delay)
           jitter = random.uniform(0, delay * 0.3)
           await asyncio.sleep(delay + jitter)
           try:
               await self._connect_to(silo)
               return  # success
           except Exception:
               attempt += 1
   ```

4. **Maximum attempts**: After a configurable number of failed attempts
   (default: unlimited while silo is in active membership), the transport
   logs a warning but continues trying.

### 4.8 Backpressure Mechanism

Backpressure operates at three levels:

1. **TCP flow control**: When the remote silo's receive buffer is full, the
   OS stops acknowledging data, causing `writer.drain()` to block. This is
   the fundamental backpressure mechanism.

2. **In-flight semaphore**: The `asyncio.Semaphore` on each connection limits
   concurrent pending requests. When the semaphore is exhausted, callers
   block on `send_request` until a response arrives and releases a permit.
   Default limit: 1000 concurrent requests per connection.

3. **Write queue monitoring**: The transport tracks the size of asyncio's
   write buffer (`writer.transport.get_write_buffer_size()`). If it exceeds
   a configurable high-water mark (default: 1 MB), new requests are delayed
   until the buffer drains below the low-water mark (default: 256 KB).

### 4.9 asyncio Integration

The transport integrates with asyncio through the following coroutine
structure per connection:

```
Per SiloConnection:
  |-- read_loop()        : reads frames, dispatches responses/requests
  |-- keepalive_loop()   : sends periodic PING if idle
  |-- (reconnect_loop()  : activated on connection loss)

SiloConnectionManager:
  |-- membership_watcher(): monitors membership changes, adjusts connections
  |-- accept_loop()      : accepts incoming connections via asyncio.start_server
```

All coroutines run on the same asyncio event loop as the grain runtime. There
are no separate threads for networking. This ensures:
- No thread synchronization overhead.
- Natural integration with grain scheduling (grain calls that await responses
  yield the event loop).
- Backpressure from slow grains propagates naturally through `await`.

For very high throughput, `uvloop` can be used as a drop-in replacement for
the default asyncio event loop, providing 2-4x throughput improvement for
networking-heavy workloads.

---

## 5. MQTT as Transport -- Deep Dive

MQTT v5 is of particular interest to the pyleans team due to IoT and smart
home use cases. This section explores how MQTT can be used for both
intra-cluster and gateway transport.

### 5.1 Topic Structure for Addressing

MQTT uses topic-based routing. For pyleans, the topic hierarchy should map to
the addressing model:

```
Intra-cluster RPC:
  pyleans/{cluster_id}/silo/{silo_id}/rpc
  pyleans/{cluster_id}/silo/{silo_id}/response
  pyleans/{cluster_id}/silo/{silo_id}/system

Broadcast:
  pyleans/{cluster_id}/broadcast/membership
  pyleans/{cluster_id}/broadcast/directory

Gateway (client-to-cluster):
  pyleans/{cluster_id}/gateway/{gateway_silo_id}/request
  pyleans/{cluster_id}/client/{client_id}/response

Direct grain addressing (optional, for observers/push):
  pyleans/{cluster_id}/grain/{grain_type}/{grain_key}/notify
```

Each silo subscribes to its own RPC and response topics on startup. Broadcast
topics use shared subscriptions where appropriate.

**Silo ID encoding**: Since MQTT topic levels cannot contain `/`, the
silo_id `192.168.1.10:11111:1234567890` would be encoded as
`192.168.1.10_11111_1234567890`.

### 5.2 Request/Response Pattern

MQTT v5 introduced properties that enable request/response:

**Request flow**:
1. Silo A publishes to `pyleans/{cluster}/silo/{silo_b}/rpc` with:
   - **Response Topic**: `pyleans/{cluster}/silo/{silo_a}/response`
   - **Correlation Data**: `{correlation_id}` (8-byte binary)
   - **Message Expiry Interval**: Matches the request timeout
   - **Payload**: Serialized request (header + body)

2. Silo B receives the message, processes it, and publishes the response to
   the Response Topic with the same Correlation Data.

3. Silo A receives the response on its response topic, matches the
   correlation data to the pending future, and completes it.

```python
class MqttClusterTransport:
    async def send_request(self, target_silo: SiloAddress,
                           header: bytes, body: bytes,
                           timeout: float = 30.0) -> tuple[bytes, bytes]:
        correlation_id, future = await self._pending.create_request(timeout)
        correlation_bytes = correlation_id.to_bytes(8, 'big')

        await self._client.publish(
            topic=f"pyleans/{self._cluster_id}/silo/{target_silo.encoded}/rpc",
            payload=self._frame_message(header, body),
            qos=1,  # at-least-once for reliability
            properties=Properties(PacketTypes.PUBLISH)
                .ResponseTopic(f"pyleans/{self._cluster_id}/silo/{self._local_silo.encoded}/response")
                .CorrelationData(correlation_bytes)
                .MessageExpiryInterval(int(timeout)),
        )
        return await future

    async def _on_response(self, message: MQTTMessage) -> None:
        correlation_id = int.from_bytes(message.properties.CorrelationData, 'big')
        header, body = self._parse_message(message.payload)
        self._pending.complete_request(correlation_id, (header, body))
```

### 5.3 QoS Level Mapping

MQTT provides three QoS levels with different trade-offs:

| QoS | Name | Guarantee | Overhead | Pyleans use case |
|---|---|---|---|---|
| 0 | At-most-once | Fire and forget | Minimal (no ack) | One-way grain calls, membership broadcasts, metrics |
| 1 | At-least-once | Delivered at least once (may duplicate) | PUBACK round trip | RPC requests/responses (default) |
| 2 | Exactly-once | Delivered exactly once | 4-step handshake | Not recommended (too much overhead for RPC) |

**Recommended mapping**:
- **RPC requests and responses**: QoS 1. Duplicates are harmless because the
  correlation ID mechanism naturally deduplicates -- a duplicate response for
  an already-completed future is simply ignored. A duplicate request may cause
  a grain method to execute twice, but this matches Orleans' at-most-once /
  at-least-once semantics.
- **One-way messages**: QoS 0. Fire-and-forget by definition.
- **Membership broadcasts**: QoS 0 with retained messages. Periodic table
  reads provide the reliability backup.
- **System messages (probes)**: QoS 0. Probe protocol is inherently tolerant
  of lost probes (multiple misses required before suspicion).

### 5.4 Scalability Concerns

The MQTT broker is the central bottleneck in this architecture. Key concerns:

**Throughput**: A high-performance MQTT broker (EMQX, VerneMQ, HiveMQ) can
handle millions of messages per second. For a moderate pyleans cluster (10-50
silos, thousands of grain calls per second), this is sufficient. For
high-throughput clusters (100+ silos, hundreds of thousands of calls per
second), the broker becomes a bottleneck.

**Latency**: Every message takes two hops (sender -> broker -> receiver)
instead of one (sender -> receiver). This approximately doubles the base
latency. For typical datacenter networks:
- Direct TCP: ~0.1-0.5ms RTT
- Via MQTT broker: ~0.5-2ms RTT
- This is acceptable for many workloads but not for latency-critical systems.

**Broker high availability**: A single broker is a single point of failure.
Production deployments require broker clustering (EMQX cluster, HiveMQ
cluster). This adds operational complexity.

**Connection fan-in**: The broker naturally handles fan-in -- all silos and
clients connect to the broker, not to each other. This simplifies network
topology and NAT traversal.

**Topic explosion**: With many silos and clients, the number of active topics
grows linearly. Most brokers handle millions of topics efficiently, but
subscription matching performance degrades with very complex wildcard
patterns.

### 5.5 Latency Implications vs Direct TCP

Measured latency comparison (typical datacenter, single-threaded benchmark):

| Metric | Direct TCP | MQTT QoS 0 | MQTT QoS 1 |
|---|---|---|---|
| Median RTT (small message) | 0.1-0.3 ms | 0.3-1.0 ms | 0.5-2.0 ms |
| P99 RTT (small message) | 0.5-1.0 ms | 2-5 ms | 5-15 ms |
| Throughput (msgs/sec, 1 connection) | 100K-500K | 50K-200K | 20K-100K |

These numbers are approximate and depend heavily on broker implementation,
hardware, and network conditions. The key takeaway: MQTT adds 2-10x latency
and reduces throughput by 2-5x compared to direct TCP.

### 5.6 When MQTT Makes Sense vs When It Does Not

**MQTT is a good choice when**:
- Clients are IoT devices or constrained hardware that already speak MQTT.
- Clients are behind NAT/firewalls and cannot accept incoming connections.
- The deployment already has an MQTT broker for other purposes.
- The cluster is small (< 20 silos) and throughput requirements are moderate.
- Operational simplicity of a star topology (all nodes connect to broker)
  outweighs the latency cost.
- The use case involves smart home / building automation where MQTT is the
  lingua franca.

**MQTT is a poor choice when**:
- Intra-cluster latency is critical (financial systems, real-time gaming).
- The cluster is large (50+ silos) with high inter-silo traffic.
- The deployment does not already have MQTT infrastructure.
- The added broker dependency and operational burden are unacceptable.

**Recommended hybrid approach**: Use direct TCP mesh for intra-cluster
transport (low latency, no broker dependency) and MQTT as a gateway transport
for IoT clients. This gives the best of both worlds:

```
IoT devices --(MQTT)--> Broker --(MQTT)--> Gateway Silo
                                              |
Silo A <--(Direct TCP)--> Silo B <--(Direct TCP)--> Silo C
```

The gateway silo subscribes to MQTT topics for incoming client requests,
translates them into internal grain calls using the direct TCP mesh, and
publishes responses back via MQTT.

---

## 6. Abstract Transport Interface

The transport layer is defined by two abstract interfaces: one for
intra-cluster communication and one for gateway communication. All transport
implementations must satisfy these interfaces.

### 6.1 Core Types

```python
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, AsyncIterator, Callable, Awaitable
import ssl


class MessageDirection(IntEnum):
    REQUEST = 1
    RESPONSE = 2
    ONE_WAY = 3


@dataclass(frozen=True)
class SiloAddress:
    """Identifies a silo in the cluster."""
    host: str
    port: int
    epoch: int  # startup timestamp for uniqueness across restarts

    @property
    def encoded(self) -> str:
        """URL/topic-safe encoding of the silo address."""
        return f"{self.host}_{self.port}_{self.epoch}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}:{self.epoch}"


@dataclass(frozen=True)
class ClientAddress:
    """Identifies an external client."""
    client_id: str


@dataclass
class TransportMessage:
    """A message exchanged over the transport layer."""
    direction: MessageDirection
    correlation_id: int
    header: bytes  # serialized message headers (opaque to transport)
    body: bytes    # serialized message body (opaque to transport)


@dataclass
class TransportOptions:
    """Configuration for transport implementations."""
    max_message_size: int = 16 * 1024 * 1024  # 16 MB
    default_request_timeout: float = 30.0  # seconds
    max_in_flight_requests: int = 1000
    keepalive_interval: float = 30.0  # seconds
    reconnect_base_delay: float = 0.1  # seconds
    reconnect_max_delay: float = 30.0  # seconds
    ssl_context: ssl.SSLContext | None = None
```

### 6.2 IClusterTransport -- Silo-to-Silo

```python
class IClusterTransport(ABC):
    """
    Abstract interface for intra-cluster (silo-to-silo) transport.

    Implementations handle connection management, message framing,
    request/response correlation, and multiplexing. The transport is
    responsible for delivering complete messages -- serialization of
    message content is handled by the layer above.

    Lifecycle:
        1. Create instance with configuration
        2. Call start() to begin listening and accepting connections
        3. Use send_request / send_one_way to communicate with other silos
        4. Call stop() to shut down gracefully
    """

    @abstractmethod
    async def start(
        self,
        local_silo: SiloAddress,
        message_handler: Callable[[SiloAddress, TransportMessage], Awaitable[TransportMessage | None]],
    ) -> None:
        """
        Start the transport: begin listening for incoming connections.

        Args:
            local_silo: The identity of this silo.
            message_handler: Callback invoked for each incoming request
                or one-way message. For requests (direction=REQUEST), the
                handler must return a TransportMessage (the response). For
                one-way messages, the handler returns None.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the transport gracefully.

        Closes all connections, fails all pending requests, and stops
        the listener. Blocks until shutdown is complete.
        """
        ...

    @abstractmethod
    async def connect_to_silo(self, silo: SiloAddress) -> None:
        """
        Establish a connection to a remote silo.

        If a connection already exists, this is a no-op. The transport
        manages reconnection internally if the connection drops while
        the silo remains in the active membership.
        """
        ...

    @abstractmethod
    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        """
        Close the connection to a remote silo.

        Fails all pending requests to that silo with ConnectionError.
        Stops reconnection attempts.
        """
        ...

    @abstractmethod
    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """
        Send a request to a remote silo and await the response.

        Args:
            target: The destination silo.
            header: Serialized message header (opaque to transport).
            body: Serialized message body (opaque to transport).
            timeout: Request timeout in seconds. None uses the default.

        Returns:
            A tuple of (response_header, response_body).

        Raises:
            TimeoutError: If no response is received within the timeout.
            ConnectionError: If the connection to the target is lost.
            TransportError: For other transport-level failures.
        """
        ...

    @abstractmethod
    async def send_one_way(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> None:
        """
        Send a fire-and-forget message to a remote silo.

        Returns as soon as the message is queued for sending. No
        delivery confirmation is provided.

        Raises:
            ConnectionError: If no connection to the target exists.
        """
        ...

    @abstractmethod
    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        """
        Send a health-check ping to a remote silo and measure round-trip time.

        Used by the membership protocol for failure detection probes.

        Args:
            target: The silo to ping.
            timeout: Maximum time to wait for the pong response.

        Returns:
            Round-trip time in seconds.

        Raises:
            TimeoutError: If no pong is received within the timeout.
            ConnectionError: If the connection to the target is lost.
        """
        ...

    @abstractmethod
    def get_connected_silos(self) -> list[SiloAddress]:
        """Return a list of currently connected remote silos."""
        ...

    @abstractmethod
    def is_connected_to(self, silo: SiloAddress) -> bool:
        """Check if a connection to the given silo is currently active."""
        ...

    @property
    @abstractmethod
    def local_silo(self) -> SiloAddress:
        """The identity of the local silo."""
        ...

    # -- Events --

    @abstractmethod
    def on_connection_established(
        self, callback: Callable[[SiloAddress], Awaitable[None]]
    ) -> None:
        """Register a callback invoked when a new silo connection is established."""
        ...

    @abstractmethod
    def on_connection_lost(
        self, callback: Callable[[SiloAddress, Exception | None], Awaitable[None]]
    ) -> None:
        """Register a callback invoked when a silo connection is lost."""
        ...
```

### 6.3 IGatewayTransport -- Client-to-Silo

```python
class IGatewayTransport(ABC):
    """
    Abstract interface for gateway (client-to-silo) transport.

    This interface has two sides:
    - Server side (GatewayListener): runs on a silo, accepts client connections
    - Client side (GatewayClient): runs in the client process, connects to silos

    Both sides are defined in this interface so that a single transport
    implementation covers the full client-to-cluster path.
    """
    pass


class IGatewayListener(ABC):
    """
    Server side of the gateway transport. Runs on a silo to accept
    external client connections.
    """

    @abstractmethod
    async def start(
        self,
        listen_address: tuple[str, int],
        message_handler: Callable[[ClientAddress, TransportMessage], Awaitable[TransportMessage | None]],
    ) -> None:
        """
        Start accepting client connections.

        Args:
            listen_address: (host, port) to listen on.
            message_handler: Callback for incoming client messages.
                For requests, return a TransportMessage response.
                For one-way messages, return None.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop accepting connections and close all client connections."""
        ...

    @abstractmethod
    async def send_to_client(
        self,
        client: ClientAddress,
        header: bytes,
        body: bytes,
    ) -> None:
        """
        Push a message to a connected client (server-initiated).

        Used for grain observers, streaming, and notifications.

        Raises:
            ConnectionError: If the client is not connected.
        """
        ...

    @abstractmethod
    def get_connected_clients(self) -> list[ClientAddress]:
        """Return a list of currently connected clients."""
        ...

    @abstractmethod
    async def disconnect_client(self, client: ClientAddress) -> None:
        """Forcefully disconnect a client."""
        ...

    @abstractmethod
    def on_client_connected(
        self, callback: Callable[[ClientAddress], Awaitable[None]]
    ) -> None:
        """Register a callback invoked when a new client connects."""
        ...

    @abstractmethod
    def on_client_disconnected(
        self, callback: Callable[[ClientAddress, Exception | None], Awaitable[None]]
    ) -> None:
        """Register a callback invoked when a client disconnects."""
        ...


class IGatewayClient(ABC):
    """
    Client side of the gateway transport. Runs in an external client
    process to communicate with the cluster.
    """

    @abstractmethod
    async def connect(
        self,
        gateway_addresses: list[tuple[str, int]],
        client_id: str,
        message_handler: Callable[[TransportMessage], Awaitable[None]],
    ) -> None:
        """
        Connect to one or more gateway silos.

        The transport manages connections to multiple gateways for fault
        tolerance. If a gateway connection drops, the transport
        reconnects or fails over to another gateway.

        Args:
            gateway_addresses: List of (host, port) gateway endpoints.
            client_id: Unique identifier for this client.
            message_handler: Callback for server-initiated messages
                (observer notifications, stream events).
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from all gateways."""
        ...

    @abstractmethod
    async def send_request(
        self,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """
        Send a grain call request through a gateway and await the response.

        The transport selects a gateway (round-robin, least-loaded, etc.)
        and sends the request through it.

        Returns:
            A tuple of (response_header, response_body).

        Raises:
            TimeoutError: If no response within the timeout.
            ConnectionError: If no gateway is reachable.
        """
        ...

    @abstractmethod
    async def send_one_way(self, header: bytes, body: bytes) -> None:
        """Send a fire-and-forget grain call through a gateway."""
        ...

    @abstractmethod
    async def update_gateways(
        self, gateway_addresses: list[tuple[str, int]]
    ) -> None:
        """
        Update the list of known gateways.

        Called periodically by the client runtime after refreshing the
        gateway list from the membership table. The transport connects
        to new gateways and disconnects from removed ones.
        """
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if at least one gateway connection is active."""
        ...

    @abstractmethod
    def on_connection_state_changed(
        self, callback: Callable[[bool], Awaitable[None]]
    ) -> None:
        """
        Register a callback invoked when the overall connection state changes.

        Args:
            callback: Called with True when at least one gateway is connected,
                False when all gateways are disconnected.
        """
        ...
```

### 6.4 Transport Errors

```python
class TransportError(Exception):
    """Base class for transport-level errors."""
    pass


class HandshakeError(TransportError):
    """Raised when the connection handshake fails."""
    pass


class MessageTooLargeError(TransportError):
    """Raised when a message exceeds the configured maximum size."""
    pass


class BackpressureError(TransportError):
    """Raised when backpressure prevents sending (non-blocking mode)."""
    pass
```

### 6.5 Transport Factory and Registration

```python
class TransportFactory(ABC):
    """Factory for creating transport instances. Used by the silo builder."""

    @abstractmethod
    def create_cluster_transport(
        self, options: TransportOptions
    ) -> IClusterTransport:
        """Create an intra-cluster transport instance."""
        ...

    @abstractmethod
    def create_gateway_listener(
        self, options: TransportOptions
    ) -> IGatewayListener:
        """Create a gateway listener (server side)."""
        ...

    @abstractmethod
    def create_gateway_client(
        self, options: TransportOptions
    ) -> IGatewayClient:
        """Create a gateway client (client side)."""
        ...
```

Usage in silo configuration:

```python
# Default: custom TCP mesh
silo = SiloBuilder() \
    .use_tcp_transport() \
    .build()

# MQTT for both cluster and gateway
silo = SiloBuilder() \
    .use_mqtt_transport(broker_url="mqtt://broker:1883") \
    .build()

# Hybrid: TCP for cluster, MQTT for gateway
silo = SiloBuilder() \
    .use_tcp_cluster_transport() \
    .use_mqtt_gateway_transport(broker_url="mqtt://broker:1883") \
    .build()

# Hybrid: TCP for cluster, WebSocket for gateway
silo = SiloBuilder() \
    .use_tcp_cluster_transport() \
    .use_websocket_gateway_transport(listen_port=8080) \
    .build()
```

---

## 7. Recommendation

### 7.1 Default Transport: Custom TCP Mesh

The default intra-cluster transport should be a custom TCP mesh, as designed
in Section 4. Rationale:

- **Zero dependencies**: No external broker or server required.
- **Lowest latency**: Direct silo-to-silo communication.
- **Proven architecture**: This is exactly what Orleans uses in production.
- **Maximum control**: Framing, backpressure, and health checks are tuned
  for the virtual actor workload.
- **Natural asyncio fit**: `StreamReader`/`StreamWriter` provide efficient,
  non-blocking I/O.

The custom TCP transport also serves as the default gateway listener for
simple deployments (same-network clients).

### 7.2 Best Gateway Transport for Web Clients: WebSocket

For browser-based and web clients, WebSocket is the clear choice:

- **Native browser support**: No plugins, proxies, or bridges required.
- **NAT/firewall friendly**: Uses standard HTTP upgrade on port 80/443.
- **Full-duplex**: Supports both request/response and server push.
- **Excellent Python library**: `websockets` is mature and asyncio-native.
- **Low implementation effort**: The framing layer from the TCP transport
  can be reused directly inside WebSocket binary frames.

Implementation priority: After the TCP transport is working, a WebSocket
gateway can be built quickly by wrapping the same message framing in
WebSocket binary messages.

### 7.3 Best Transport for IoT/Edge Scenarios: MQTT v5

For IoT and smart home deployments, MQTT v5 as a gateway transport:

- **IoT lingua franca**: Devices likely already speak MQTT.
- **NAT traversal**: Devices connect outbound to the broker.
- **Constrained device support**: Minimal overhead, small packet sizes.
- **Existing infrastructure**: The team likely already operates an MQTT
  broker for IoT workloads.
- **Shared subscriptions**: Enable load balancing across gateway silos.

The recommended pattern is the hybrid approach described in Section 5.6:
TCP mesh for intra-cluster, MQTT gateway for IoT clients.

For intra-cluster transport in IoT/edge deployments where silos run on
constrained hardware without direct connectivity, MQTT can also serve as
the cluster transport, accepting the latency trade-off.

### 7.4 Implementation Priority

The recommended order of implementation:

**Phase 1 -- Core (implement first)**:
1. **Custom TCP mesh** (IClusterTransport + IGatewayListener + IGatewayClient)
   - This is the foundational transport. All other development depends on it.
   - Covers both intra-cluster and basic gateway use cases.
   - Estimated effort: 2-3 weeks for a production-quality implementation.

**Phase 2 -- IoT gateway**:
2. **MQTT v5 gateway** (IGatewayListener + IGatewayClient only)
   - Addresses the team's primary IoT use case.
   - The IClusterTransport remains TCP mesh.
   - Estimated effort: 1-2 weeks.

**Phase 3 -- Web gateway**:
3. **WebSocket gateway** (IGatewayListener + IGatewayClient only)
   - Enables browser and web application clients.
   - Estimated effort: 1 week (reuses TCP framing).

**Phase 4 -- Optional/future**:
4. **Zenoh transport** (IClusterTransport + IGatewayListener + IGatewayClient)
   - Peer-to-peer mode for cluster, router mode for gateway.
   - Could potentially unify cluster and gateway transport in a single protocol.
   - Especially interesting for edge deployments with mixed connectivity.
5. **MQTT v5 cluster transport** (IClusterTransport)
   - For edge deployments where silos cannot reach each other directly.
   - Lower priority; only needed for specific deployment topologies.
6. **Unix Domain Socket optimization**
   - Transparent optimization for same-host silo pairs.
   - Drop-in replacement detected automatically.

### 7.5 Summary Table

| Use Case | Recommended Transport | Alternative |
|---|---|---|
| Intra-cluster (default) | Custom TCP mesh | Zenoh (P2P mode) |
| Intra-cluster (edge/IoT silos) | MQTT v5 | Zenoh (router mode) |
| Gateway (web/browser clients) | WebSocket | Zenoh (WS transport) |
| Gateway (IoT devices) | MQTT v5 | Zenoh (edge-native) |
| Gateway (service clients, same network) | Custom TCP | Zenoh (P2P) |
| Gateway (mobile clients) | WebSocket or MQTT v5 | Zenoh |
| Same-host optimization | Unix Domain Sockets | Zenoh (shared memory) |

---

## References

- Orleans networking internals: `orleans-networking.md`
- Orleans cluster architecture: `orleans-cluster.md`
- MQTT v5 specification: https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html
- Zenoh project: https://zenoh.io/
- Zenoh protocol (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-core-zenoh-protocol/
- zenoh-python: https://github.com/eclipse-zenoh/zenoh-python
- ZeroMQ guide: https://zguide.zeromq.org/
- Python `websockets` library: https://websockets.readthedocs.io/
- aiomqtt (MQTT for asyncio): https://github.com/sbtinstruments/aiomqtt
- uvloop: https://github.com/MagicStack/uvloop
