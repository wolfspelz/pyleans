# Orleans Serialization, Networking, and RPC

This document covers the internals of Microsoft Orleans' serialization system, networking
layer, and RPC mechanism. It is intended as a reference for building a Python implementation
of the Orleans virtual actor model.

---

## Table of Contents

1. [Serialization System](#1-serialization-system)
2. [Serialization Formats and Evolution](#2-serialization-formats-and-evolution)
3. [Immutable Types Optimization](#3-immutable-types-optimization)
4. [Grain Method Invocation (RPC)](#4-grain-method-invocation-rpc)
5. [Message Format and Routing](#5-message-format-and-routing)
6. [Silo-to-Silo Communication Protocol](#6-silo-to-silo-communication-protocol)
7. [Client-to-Silo Gateway Protocol](#7-client-to-silo-gateway-protocol)
8. [Connection Multiplexing](#8-connection-multiplexing)
9. [Request/Response Correlation](#9-requestresponse-correlation)
10. [One-Way Messages (Fire and Forget)](#10-one-way-messages-fire-and-forget)
11. [Cancellation Token Support](#11-cancellation-token-support)
12. [Generic Grain Support](#12-generic-grain-support)
13. [Implementation Notes for Python](#13-implementation-notes-for-python)

---

## 1. Serialization System

### 1.1 Overview

Orleans includes a high-performance, extensible serialization framework called
**Orleans.Serialization**. There are two distinct serialization contexts:

- **Grain call serialization**: Serializes objects passed to and from grains over the
  network (the primary focus of this document).
- **Grain storage serialization**: Serializes objects to/from storage backends (uses
  `IGrainStorageSerializer`, typically defaults to Newtonsoft.Json).

### 1.2 Design Goals

The serialization system is designed for:

- **High performance**: Optimized binary format.
- **High fidelity**: Faithfully represents most of .NET's type system, including:
  - Generics
  - Polymorphism
  - Inheritance hierarchies
  - Object identity (reference tracking)
  - Cyclic graphs
- **Flexibility**: Supports custom serializers, surrogates, converters, and delegation to
  external serialization libraries (System.Text.Json, Newtonsoft.Json, MessagePack,
  Google.Protobuf).
- **Version tolerance**: Application types can evolve over time (adding/removing members,
  numeric widening/narrowing, type renaming).

### 1.3 The `[GenerateSerializer]` Attribute and Code Generation

Orleans uses compile-time C# source generators to produce serialization code. The key
attribute is `[GenerateSerializer]`, which must be applied to any type that will be
serialized in grain calls.

```csharp
[GenerateSerializer]
public class Employee
{
    [Id(0)]
    public string Name { get; set; }
}
```

When `[GenerateSerializer]` is applied to a type, Orleans generates at build time:
- A **serializer** (codec) for writing/reading the type to/from a binary stream.
- A **copier** for deep-copying instances of the type (used for isolation guarantees).

The source generator runs during compilation (since Orleans 7.0+, there is no
runtime/initialization-time code generation).

### 1.4 The `[Id]` Field Annotation

Each serializable member (property or field) must be annotated with `[Id(n)]` where `n` is
a stable, unique integer identifier within the type's inheritance level.

Key rules:
- IDs are **scoped to the inheritance level**, not the type as a whole. A base class and a
  subclass can both have members with `[Id(0)]`.
- IDs must be unique within a single level of the inheritance hierarchy.
- IDs should start at zero for each type.
- IDs must remain stable across versions -- changing an ID is a breaking change.
- Fields can be added or removed freely (version tolerance), but IDs cannot be reused for
  different fields.

```csharp
[GenerateSerializer]
public class Publication
{
    [Id(0)]
    public string Title { get; set; }
}

[GenerateSerializer]
public class Book : Publication
{
    [Id(0)]  // OK: scoped to Book, not to Publication
    public string ISBN { get; set; }
}
```

### 1.5 Record Types

Record types with primary constructors have implicit IDs for their constructor parameters:
- Parameter order defines implicit IDs (changing parameter order is a breaking change).
- Members defined in the record body do NOT share identities with primary constructor
  parameters, so `[Id(0)]` in the body does not clash with the first constructor param.
- You can opt out with `[GenerateSerializer(IncludePrimaryConstructorParameters = false)]`.

```csharp
[GenerateSerializer]
public record MyRecord(string A, string B)
{
    [Id(0)]  // Does not clash with A
    public string C { get; init; }
}
```

### 1.6 Type Aliases with `[Alias]`

By default, types are encoded using their full .NET type name. This is fragile across
renames and assembly moves. The `[Alias("name")]` attribute provides a stable,
rename-resilient type identifier.

- Aliases are **globally scoped** -- no two types can share an alias.
- For generic types, include the arity: `[Alias("mytype`2")]` for `MyType<T, U>`.
- Best practice: always alias serializable types.

### 1.7 Three Phases of Serialization

Orleans serialization operates in three phases:

1. **Deep copy**: Objects are immediately deep-copied to ensure isolation between caller and
   callee. This prevents the caller from modifying arguments after sending.
2. **Serialize**: Before putting on the wire, objects are serialized to a byte stream.
3. **Deserialize**: On the receiving end, objects are reconstructed from the byte stream.

All three phases respect **object identity**: if the same object is referenced multiple
times in the argument graph, it is serialized once and all references are correctly
restored on deserialization.

### 1.8 Custom Serializers

Orleans supports several levels of custom serialization:

#### IGeneralizedCodec / IGeneralizedCopier / ITypeFilter (Orleans 7+)

For implementing a serializer that handles multiple types:

```csharp
internal sealed class CustomOrleansSerializer :
    IGeneralizedCodec, IGeneralizedCopier, ITypeFilter
{
    void IFieldCodec.WriteField<TBufferWriter>(
        ref Writer<TBufferWriter> writer,
        uint fieldIdDelta,
        Type expectedType,
        object value);

    object IFieldCodec.ReadValue<TInput>(
        ref Reader<TInput> reader, Field field);

    bool IGeneralizedCodec.IsSupportedType(Type type);
    object IDeepCopier.DeepCopy(object input, CopyContext context);
    bool IGeneralizedCopier.IsSupportedType(Type type);
}
```

Register via DI:
```csharp
services.AddSingleton<IGeneralizedCodec, CustomOrleansSerializer>();
services.AddSingleton<IGeneralizedCopier, CustomOrleansSerializer>();
services.AddSingleton<ITypeFilter, CustomOrleansSerializer>();
```

#### Surrogates for Foreign Types

When you cannot annotate a type with `[GenerateSerializer]` (e.g., third-party types), you
create a **surrogate** type and a **converter**:

```csharp
[GenerateSerializer]
public struct ForeignTypeSurrogate
{
    [Id(0)] public int Num;
    [Id(1)] public string String;
}

[RegisterConverter]
public sealed class ForeignTypeConverter :
    IConverter<ForeignType, ForeignTypeSurrogate>
{
    public ForeignType ConvertFromSurrogate(in ForeignTypeSurrogate surrogate) => ...;
    public ForeignTypeSurrogate ConvertToSurrogate(in ForeignType value) => ...;
}
```

For non-sealed foreign types that participate in inheritance hierarchies, additionally
implement `IPopulator<TValue, TSurrogate>`.

#### External Serializer Providers

Orleans can delegate to external serializers for specific types:
- **Newtonsoft.Json**: `AddNewtonsoftJsonSerializer(isSupported: type => ...)`
- **System.Text.Json**: `AddJsonSerializer(isSupported: type => ...)`
- **MessagePack**: `AddMessagePackSerializer(isSerializable: type => ...)`

Types marked with `[GenerateSerializer]` always take precedence over external serializers.

### 1.9 Serializer Comparison

| Feature             | Orleans Native | MessagePack | System.Text.Json |
|---------------------|---------------|-------------|-----------------|
| Format              | Binary        | Binary      | Text (JSON)     |
| .NET type fidelity  | Excellent     | Good        | Limited         |
| Object identity     | Yes           | No          | No              |
| Payload size        | Small         | Smallest    | Largest         |
| Cross-platform      | .NET only     | Any MsgPack | Any JSON        |
| Version tolerance   | Yes           | Yes         | Yes             |
| Setup required      | None          | Explicit    | Explicit        |

---

## 2. Serialization Formats and Evolution

### 2.1 Wire Format (Orleans Native Serializer)

The Orleans native serializer uses a custom binary format. Key characteristics:

- **Field-tagged**: Each serialized field is prefixed with its `[Id]` value (encoded as a
  delta from the previous field ID to minimize size).
- **Type-prefixed**: Type information is embedded when the runtime type differs from the
  expected/declared type (enabling polymorphism).
- **Length-prefixed for variable data**: Strings, byte arrays, and nested objects use length
  prefixes.
- **Object identity tracking**: A reference table is maintained during serialization. When
  an object is encountered a second time, only a back-reference is written.
- **Hierarchical encoding**: Each level of the inheritance hierarchy is serialized
  separately, with its own set of field IDs.

The `Writer<TBufferWriter>` and `Reader<TInput>` structs provide the low-level I/O.
`Field` structs carry the field header (field ID delta, wire type, and optionally a type
reference).

### 2.2 Version Tolerance Rules

The serialization format supports forward and backward compatibility under these rules:

**Compound types (class and struct):**
- Inheritance is supported; modifying the hierarchy is NOT (cannot add/change/remove base
  classes).
- Field types cannot change (with numeric exceptions below).
- Fields can be added or removed at any level of the hierarchy.
- Field IDs cannot change.
- Field IDs must be unique per inheritance level but CAN overlap across levels.

**Numerics:**
- Signedness changes are NOT allowed (`int` <-> `uint` is invalid).
- Width changes ARE allowed (`int` -> `long`, `ulong` -> `ushort`).
  - Narrowing throws at runtime if the value overflows.
  - `double` -> `float` is only valid if the value is within `float` range.
  - `decimal` has a narrower range than `double`/`float`.

**Best practices:**
- Always give types aliases with `[Alias("...")]`.
- Do not change `record` to `class` or vice versa.
- Do not add new base classes to existing types.
- Start member IDs at zero for each type.
- Widen numeric types as needed; narrowing is allowed but risky.

---

## 3. Immutable Types Optimization

### 3.1 Deep Copy Avoidance

During grain calls, Orleans deep-copies all arguments and return values to ensure isolation.
This is expensive. For immutable data, copying is unnecessary.

Orleans provides three mechanisms to skip copying:

#### `[Immutable]` on a Type

```csharp
[Immutable]
public class MyImmutableType
{
    public int MyValue { get; }
    public MyImmutableType(int value) { MyValue = value; }
}
```

Instances of this type will NEVER be copied during grain calls.

#### `[Immutable]` on a Parameter

```csharp
public interface ISummerGrain : IGrain
{
    ValueTask<int> Sum([Immutable] List<int> values);
}
```

The `values` parameter will not be copied for this specific call.

#### `[Immutable]` on a Property/Field

```csharp
[GenerateSerializer]
public sealed class MyType
{
    [Id(0), Immutable]
    public List<int> ReferenceData { get; set; }  // Not copied

    [Id(1)]
    public List<int> RunningTotals { get; set; }   // Copied normally
}
```

#### `Immutable<T>` Wrapper

A wrapper class that signals both sender and receiver will not modify the value:

```csharp
// In grain interface
Task<Immutable<byte[]>> ProcessRequest(Immutable<byte[]> request);

// Creating
Immutable<byte[]> immutable = new(buffer);

// Accessing
byte[] buffer = immutable.Value;
```

### 3.2 Immutability Contract

Immutability in Orleans is a **dual-sided commitment**: neither the sender nor the receiver
will modify the data. The safest approach is bitwise immutability (never modify the object
at all). Logical immutability (thread-safe mutations) is possible but discouraged.

---

## 4. Grain Method Invocation (RPC)

### 4.1 How a Grain Call Becomes a Network Message

The RPC flow in Orleans follows these steps:

1. **Obtain a grain reference**: The caller gets a grain reference via
   `IGrainFactory.GetGrain<TInterface>(key)`. This returns a **proxy object** -- a
   generated class inheriting from `GrainReference` that implements the grain interface.

2. **Method call on proxy**: When the caller invokes a method on the grain reference, the
   generated proxy code:
   a. Deep-copies all arguments (unless marked immutable).
   b. Creates an `InvokeMethodRequest` containing:
      - The interface ID (identifies which grain interface)
      - The method ID (identifies which method on the interface)
      - The serialized arguments
   c. Wraps this in a `Message` object with routing headers.

3. **Message routing**: The runtime determines which silo hosts the target grain:
   a. Consults the local **grain directory cache**.
   b. If cache miss, queries the distributed **grain directory** (a distributed hash table
      partitioned across silos using consistent hashing with virtual nodes).
   c. The grain directory returns the `SiloAddress` of the silo hosting the grain
      activation, or indicates the grain needs to be activated.

4. **Message transmission**: The message is serialized and sent over TCP to the target silo.

5. **Message reception**: The target silo:
   a. Deserializes the message.
   b. Looks up the grain activation.
   c. Enqueues the request in the grain's **single-threaded scheduler**.
   d. When the grain's turn comes, dispatches the method call.

6. **Response**: When the grain method completes:
   a. The return value is deep-copied (unless immutable).
   b. A response message is created with the same correlation ID.
   c. The response is serialized and sent back to the calling silo.
   d. The calling silo matches the response to the pending `Task` and completes it.

7. **Error propagation**: If the grain method throws an exception, the exception is
   serialized and sent back as a fault response. The caller's `Task` is faulted with the
   exception. If the exception type is unavailable on the calling side, it is wrapped in
   `UnavailableExceptionFallbackException` (preserving message, type name, and stack trace).

### 4.2 Generated Grain Reference (Proxy)

Orleans generates a grain reference class for each grain interface at compile time:
- Inherits from `Orleans.Runtime.GrainReference`.
- Implements the grain interface.
- Each interface method implementation serializes arguments and calls into the Orleans
  messaging layer.
- The grain reference encapsulates the **grain identity** (grain type + grain key).

### 4.3 Grain Identity

A grain identity (`GrainId`) consists of two parts:
- **Grain type** (`GrainType`): A string identifying the grain class. By convention, derived
  from the class name with "Grain" suffix removed and lowercased (e.g.,
  `ShoppingCartGrain` -> `shoppingcart`). Can be overridden with `[GrainType("name")]`.
- **Grain key** (`IdSpan`): A string identifying the specific instance. Can be constructed
  from `Guid`, `long`, `string`, or compound keys (`Guid+string`, `long+string`).

The full identity is written as `graintype/key`, e.g., `shoppingcart/bob65`.

### 4.4 Interface and Method Identification

Each grain interface and method has a numeric identifier:
- **Interface ID**: Computed from the interface type (hash of the fully qualified type name).
- **Method ID**: Computed from the method signature within the interface.

These IDs are embedded in every request message to identify which method to dispatch.

### 4.5 Request Scheduling and Single-Threaded Execution

Grain activations execute requests **single-threaded by default**:
- Each request runs to completion before the next starts.
- This prevents concurrent state access bugs.
- Interleaving can be enabled with:
  - `[Reentrant]` on the grain class (all methods can interleave)
  - `[AlwaysInterleave]` on a specific interface method
  - `[ReadOnly]` on methods that do not modify state
  - `[MayInterleave(nameof(Predicate))]` for dynamic per-request decisions
  - `RequestContext.AllowCallChainReentrancy()` for call-chain-scoped reentrancy

### 4.6 Response Timeout

- Default response timeout: **30 seconds** (configurable via `SiloMessagingOptions` /
  `ClientMessagingOptions`).
- Per-method timeout: `[ResponseTimeout("00:00:05")]` on the interface method.
- On timeout, the returned `Task` faults with `TimeoutException`.
- Optionally, automatic retry on timeout can be configured (default: no retries).

---

## 5. Message Format and Routing

### 5.1 Message Structure

An Orleans `Message` contains:

**Header fields:**
- **Message ID / Correlation ID**: Unique identifier for request/response matching.
- **Target address**: `SiloAddress` + `GrainId` + `ActivationId` of the target grain.
- **Sender address**: `SiloAddress` + `GrainId` + `ActivationId` of the sending grain (or
  client).
- **Interface ID**: Numeric identifier of the grain interface being called.
- **Method ID**: Numeric identifier of the method being invoked.
- **Message category/direction**:
  - Categories: `Application`, `System` (internal runtime messages)
  - Directions: `Request`, `Response`, `OneWay`
- **Response type**: `Success`, `Error`, `Rejection`
- **Timeout**: When the request expires.
- **Call chain ID**: For reentrancy tracking across grain calls.
- **Request context**: Key-value pairs flowing with the request (tracing, user data).
- **CancellationToken state**: Whether cancellation was requested.

**Body:**
- **Serialized arguments** (for requests) or **serialized return value** (for responses).
- Serialized using the Orleans serialization framework.

### 5.2 Message Serialization on the Wire

Messages are written to TCP connections using a length-prefixed binary protocol:
1. **Header length** (4 bytes): Length of the serialized header.
2. **Body length** (4 bytes): Length of the serialized body.
3. **Header bytes**: Serialized message headers.
4. **Body bytes**: Serialized message body (arguments or return value).

### 5.3 Message Routing

Message routing follows this flow:

1. **Local check**: If the target grain is on the local silo, deliver directly to the
   grain's scheduler queue (no serialization needed; just deep copy).

2. **Directory lookup**: The grain directory is consulted to find which silo hosts the
   target grain activation:
   - **Local cache check** first (avoids network round trips).
   - **Distributed directory query** if cache miss. The directory is a DHT partitioned
     across silos using consistent hashing with ~30 virtual nodes per silo.
   - If no activation exists, the directory selects a silo for placement (using a placement
     strategy) and creates the activation.

3. **Forward to silo**: The message is sent to the target silo over the TCP connection.

4. **Silo-local delivery**: The target silo locates the grain activation and enqueues the
   message for processing.

### 5.4 Delivery Guarantees

- **Default: at-most-once**: A message is delivered once or not at all. No automatic
  retries. If the response does not arrive within the timeout, the `Task` faults with
  `TimeoutException`.
- **With retries: at-least-once**: If automatic retries on timeout are configured, a
  message may be delivered more than once. Orleans does NOT deduplicate messages.
- Orleans does NOT provide exactly-once delivery.

---

## 6. Silo-to-Silo Communication Protocol

### 6.1 Connection Management

Orleans silos maintain persistent TCP connections to each other:
- Connections are established on demand when a silo first needs to send a message to
  another silo.
- Connections are long-lived and reused for all messages between a pair of silos.
- Orleans uses .NET's `System.IO.Pipelines` (high-performance I/O) for connection
  handling.
- Connection health is monitored; failed connections trigger reconnection attempts.

### 6.2 Membership Protocol Integration

Silo-to-silo connections are deeply integrated with the cluster membership protocol:

- Silos discover each other via the **membership table** (`IMembershipTable`), backed by
  durable storage (Azure Table, SQL, Redis, Consul, ZooKeeper, DynamoDB, Cassandra, Cosmos
  DB, or in-memory for dev).
- Failure detection uses **direct peer-to-peer probes** (heartbeats sent over the same TCP
  connections used for grain messages). This ensures probes accurately reflect actual
  network health.
- Each silo monitors a configurable set of other silos (default: 10, selected via
  consistent hashing on silo identities).
- Failure detection parameters:
  - Probes sent every 10 seconds
  - 3 missed probes to suspect a silo
  - 2 suspicions required to declare dead
  - Suspicions valid for 3 minutes
  - Typical detection time: ~15 seconds

### 6.3 Silo Identity

A silo is identified by `ip:port:epoch` where:
- `ip`: The silo's IP address
- `port`: The silo's listening port
- `epoch`: The time in ticks when the silo started (ensures uniqueness across restarts)

### 6.4 Protocol Flow

1. On startup, a silo registers itself in the membership table.
2. It reads the membership table to discover other silos.
3. It establishes TCP connections to silos it needs to communicate with.
4. It begins sending and receiving messages (multiplexed on the connection).
5. It participates in the probe protocol for failure detection.
6. If a silo is declared dead, its connections are closed and messages to its grains are
   re-routed (grains are reactivated on other silos).

### 6.5 Self-Monitoring and Indirect Probing

Orleans incorporates ideas from Hashicorp's Lifeguard research:

- **Self-monitoring**: Each silo scores its own health based on:
  - Active status in membership table
  - No suspicions from other silos
  - Successful probe responses
  - Thread pool responsiveness
  - Timer accuracy
  An unhealthy silo (score 1-8) gets increased probe timeouts, reducing the chance it
  incorrectly votes out healthy silos.

- **Indirect probing**: When a silo has only 2 probe attempts left before voting a target
  dead, it asks a random intermediary silo to probe the target. If the intermediary also
  fails to reach the target (and declares itself healthy), the vote is cast. With default
  settings, a negative indirect probe counts as 2 votes, enabling faster detection.

---

## 7. Client-to-Silo Gateway Protocol

### 7.1 Overview

External clients (non-silo processes) connect to the Orleans cluster through **gateway
endpoints** on silos. The client-to-silo protocol differs from silo-to-silo in several
ways:

- Clients discover gateway silos via the **membership table** (using a gateway list
  provider such as `UseAzureStorageClustering`, `UseRedisClustering`, etc.).
- The gateway list is periodically refreshed (default: every 1 minute, configurable via
  `GatewayOptions.GatewayListRefreshPeriod`).
- Clients do NOT participate in the membership protocol or failure detection.

### 7.2 Co-hosted Clients

The recommended approach is co-hosting client code in the same process as grain code:
- The client communicates directly with the local silo.
- No separate gateway connection needed.
- Takes advantage of the silo's cluster topology knowledge.
- For local grains, no serialization or network communication is needed at all.
- Eliminates a network hop and serialization round-trip.

### 7.3 External Client Connection

For external clients:
1. Client obtains the gateway list from the membership table.
2. Client establishes TCP connections to one or more gateway silos.
3. Client sends grain call messages through the gateway.
4. The gateway silo routes messages to the appropriate silo (locally if the target grain is
   on the same silo, otherwise forwards to the correct silo).
5. Responses flow back through the same path.

### 7.4 Client Connectivity and Resilience

- If a client cannot connect to any silo, it throws `SiloUnavailableException`.
- An `IClientConnectionRetryFilter` can be registered for custom retry logic.
- Once connected, the client attempts to recover indefinitely from connectivity issues.
- If a connection issue occurs during a grain call, `SiloUnavailableException` is thrown;
  the grain reference remains valid for future retry.

---

## 8. Connection Multiplexing

### 8.1 Single Connection per Silo Pair

Orleans uses a **single TCP connection** (or a small number of connections) between each
pair of communicating silos. All messages between those silos are **multiplexed** over this
connection.

Key characteristics:
- Multiple concurrent grain calls share the same connection.
- Messages are interleaved at the message level (not byte level) -- each message is sent as
  a complete unit.
- The `System.IO.Pipelines` API provides efficient, zero-copy I/O.
- Backpressure is handled at the connection level.

### 8.2 Connection Lifecycle

- Connections are established lazily on first use.
- Connections are maintained as long as both silos are alive.
- If a connection fails, it is re-established automatically.
- When a silo is declared dead, its connections are torn down.

---

## 9. Request/Response Correlation

### 9.1 Correlation Mechanism

Every request message carries a unique **message ID** (correlation ID). When the target
silo processes the request and produces a response, the response carries the same
correlation ID.

The flow:
1. Caller generates a unique message ID and stores a `TaskCompletionSource` keyed by that
   ID.
2. Request message with the ID is sent.
3. Response message arrives with the same ID.
4. Caller looks up the `TaskCompletionSource` by ID and completes it with the result (or
   faults it with an exception).

### 9.2 Timeout Management

- Each pending request has a timer. If the response does not arrive within the configured
  timeout (default 30 seconds), the `TaskCompletionSource` is faulted with
  `TimeoutException`.
- Per-method timeouts can override the global default using `[ResponseTimeout(...)]`.

---

## 10. One-Way Messages (Fire and Forget)

### 10.1 The `[OneWay]` Attribute

Grain interface methods can be marked `[OneWay]` to indicate fire-and-forget semantics:

```csharp
public interface IOneWayGrain : IGrainWithGuidKey
{
    [OneWay]
    Task Notify(MyData data);
}
```

### 10.2 Behavior

- The returned `Task` completes **immediately** on the caller side (as soon as the message
  is queued, before the callee receives it).
- **No response message** is sent back.
- Failures are NOT signaled to the caller:
  - No completion signal
  - No exception propagation
  - No delivery guarantee
- One-way methods must return `Task` or `ValueTask` (not generic variants like `Task<T>`).
- Saves the cost of the response message, which can improve performance in specific cases.
- Use with care -- only appropriate when the caller does not need to know if the call
  succeeded.

### 10.3 Wire-Level Difference

One-way messages:
- Have the `Direction` field set to `OneWay` instead of `Request`.
- The sender does NOT create a `TaskCompletionSource` or start a timeout timer.
- The receiver processes the message but does NOT send a response.

---

## 11. Cancellation Token Support

### 11.1 CancellationToken in Grain Methods

Orleans supports passing `System.Threading.CancellationToken` as a parameter to grain
methods:

```csharp
public interface IProcessingGrain : IGrainWithGuidKey
{
    Task<string> ProcessDataAsync(
        string data, int chunks,
        CancellationToken cancellationToken = default);
}
```

### 11.2 How It Works

- **Pre-call check**: If the token is already cancelled before the call, Orleans throws
  `OperationCanceledException` immediately without sending a message.
- **Enqueued request cancellation**: If a request is cancelled while waiting in the grain's
  queue (before execution starts), it is cancelled without execution.
- **During execution**: The runtime listens for cancellation and propagates the signal to
  the remote grain. Cancellation callbacks registered on the token run on the grain's
  scheduler (safe for grain state access).
- **Post-completion**: Once a call completes, subsequent cancellation signals are NOT
  propagated.

### 11.3 Cross-Silo Cancellation

- Cancellation works across silo boundaries.
- Client-to-grain calls support cancellation regardless of hosting silo.
- Grain-to-grain calls support cross-silo cancellation.
- Network partitions may delay cancellation signal delivery.

### 11.4 Automatic Timeout Cancellation

When `CancelRequestOnTimeout` is enabled (default: `true`):
- Orleans automatically sends a cancellation signal when a request times out.
- `WaitForCancellationAcknowledgement` (default: `false`) controls whether to wait for the
  target to acknowledge the cancellation.

### 11.5 Cancellation Batching

Orleans optimizes performance by batching cancellation requests when many occur
simultaneously (e.g., during shutdown or mass timeout).

### 11.6 Backward Compatibility

- Adding a `CancellationToken` parameter to an existing method is backward compatible.
  Older callers that do not send it will cause the grain to receive `CancellationToken.None`.
- Removing a `CancellationToken` parameter is also backward compatible. The runtime ignores
  the extra token from older callers.

### 11.7 Legacy: GrainCancellationToken

Before direct `CancellationToken` support, Orleans provided `GrainCancellationToken` and
`GrainCancellationTokenSource`:
- `GrainCancellationToken` wraps a standard `CancellationToken`.
- Cancellation requires explicitly calling `await GrainCancellationTokenSource.Cancel()`.
- This is now legacy; prefer `CancellationToken` directly.

---

## 12. Generic Grain Support

### 12.1 Generic Grain Interfaces and Classes

Orleans fully supports generic grain interfaces and classes:

```csharp
public interface IDictionaryGrain<K, V> : IGrainWithStringKey
{
    Task Set(K key, V value);
    Task<V> Get(K key);
}

[GrainType("dict`2")]
public class DictionaryGrain<K, V> : Grain, IDictionaryGrain<K, V>
{
    // implementation
}
```

### 12.2 Generic Grain Type Names

For generic grains, the grain type name must include the **generic arity** using backtick
notation:
- `DictionaryGrain<K, V>` -> grain type name `dict`2` (backtick + number of type params)
- This is the same convention used in .NET metadata.

### 12.3 Generic Serialization

- Serializers for generic types are also generic.
- `[Serializer(typeof(MyGenericType<T>))]` targets an open generic type.
- Orleans creates concrete serializer instances at runtime for each closed generic type
  actually used (e.g., one each for `MyGenericType<int>` and `MyGenericType<string>`).

### 12.4 Type Aliases for Generics

Generic type aliases include the arity: `[Alias("mytype`2")]` for a two-parameter generic.

---

## 13. Implementation Notes for Python

This section highlights key aspects to consider when building a Python implementation.

### 13.1 Serialization

- The Orleans native serializer is a custom binary format, not a standard like protobuf or
  msgpack. For Python interop, consider:
  - Implementing the Orleans wire format natively (complex but enables direct interop).
  - Using a common format (MessagePack, JSON) if interop with .NET Orleans is not required.
  - If using Orleans as a reference architecture (not interop), a simpler tagged binary
    format with field IDs and type tags would capture the key ideas.
- Key concepts to preserve: field-ID-based encoding (not field-name), version tolerance,
  object identity/reference tracking, type polymorphism.
- Python equivalent of `[GenerateSerializer]` + `[Id]`: could use decorators and
  metaclasses, or a schema definition DSL.

### 13.2 Deep Copy

- Orleans deep-copies arguments to ensure isolation. In Python:
  - `copy.deepcopy()` provides equivalent semantics but is slow.
  - For simple types, serialization round-trip can serve as deep copy.
  - Consider whether Python's GIL makes deep-copy less critical (it doesn't -- async code
    can still interleave).

### 13.3 Grain References and Proxies

- The C# implementation uses compile-time code generation for proxy classes. In Python:
  - Use `__getattr__` magic methods or metaclasses to generate proxy method calls
    dynamically.
  - Each method call constructs a message with interface ID, method ID, and serialized args.

### 13.4 Message Format

Key fields for a minimal message:
- Correlation ID (UUID or incrementing integer)
- Direction (Request / Response / OneWay)
- Source address (silo + grain ID)
- Target address (silo + grain ID)
- Interface ID
- Method ID
- Serialized body (arguments or return value)
- Response type (Success / Error / Rejection) for responses
- Timeout / expiry
- CancellationToken state

### 13.5 Networking

- Use `asyncio` TCP connections with `asyncio.StreamReader`/`StreamWriter` or a more
  efficient library like `uvloop`.
- Single connection per silo pair, multiplexed.
- Length-prefix framing: 4-byte header length + 4-byte body length + header + body.
- Maintain a dictionary of `correlation_id -> asyncio.Future` for response matching.

### 13.6 Grain Directory

- Implement as a distributed hash table with consistent hashing.
- Each silo owns ~30 virtual nodes on the hash ring.
- Local cache on each silo for frequently accessed grains.
- Strong consistency via the Virtual Synchrony methodology (or simplified: epoch-based
  ownership with range locking during view changes).

### 13.7 One-Way and Cancellation

- One-way: complete the caller's future immediately, do not create a pending-response
  entry.
- Cancellation: maintain a mapping of active requests to cancellation state. When
  cancellation is signaled, send a cancellation message to the target silo. The target
  checks the cancellation state at cooperative yield points.

### 13.8 IAsyncEnumerable (Streaming Responses)

Orleans supports `IAsyncEnumerable<T>` return types from grain methods:
- Items are batched (default: 100 per batch, configurable via `WithBatchSize(n)`).
- This reduces network round trips while enabling streaming semantics.
- Python equivalent: `async for` with `AsyncIterator` protocol.

---

## Sources

- [Orleans Serialization](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization)
- [Orleans Serialization Customization](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization-customization)
- [Orleans Serialization Configuration](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization-configuration)
- [Orleans Serialization Immutability](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization-immutability)
- [Orleans Grain Development](https://learn.microsoft.com/en-us/dotnet/orleans/grains/)
- [Orleans Grain References](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-references)
- [Orleans Grain Identity](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-identity)
- [Orleans Request Scheduling](https://learn.microsoft.com/en-us/dotnet/orleans/grains/request-scheduling)
- [Orleans One-Way Requests](https://learn.microsoft.com/en-us/dotnet/orleans/grains/oneway)
- [Orleans Cancellation Tokens](https://learn.microsoft.com/en-us/dotnet/orleans/grains/cancellation-tokens)
- [Orleans Code Generation](https://learn.microsoft.com/en-us/dotnet/orleans/grains/code-generation)
- [Orleans Grain Lifecycle](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-lifecycle)
- [Orleans Messaging Delivery Guarantees](https://learn.microsoft.com/en-us/dotnet/orleans/implementation/messaging-delivery-guarantees)
- [Orleans Cluster Management](https://learn.microsoft.com/en-us/dotnet/orleans/implementation/cluster-management)
- [Orleans Grain Directory](https://learn.microsoft.com/en-us/dotnet/orleans/implementation/grain-directory)
- [Orleans Clients](https://learn.microsoft.com/en-us/dotnet/orleans/host/client)
- [Orleans Architecture Principles](https://learn.microsoft.com/en-us/dotnet/orleans/resources/orleans-architecture-principles-and-approach)
- [Orleans Technical Report (MSR-TR-2014-41)](https://research.microsoft.com/pubs/210931/Orleans-MSR-TR-2014-41.pdf)
