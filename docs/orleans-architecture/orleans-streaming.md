# Orleans Streaming System -- Comprehensive Reference

This document provides a thorough reference for the Microsoft Orleans Virtual Actors
streaming subsystem, intended to guide a Python implementation. It covers concepts,
interfaces, providers, subscription models, delivery guarantees, the PubSub system,
and custom provider implementation.

Sources:
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/streams-programming-apis
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/stream-providers
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/streams-quick-start
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/broadcast-channel
- https://learn.microsoft.com/en-us/dotnet/orleans/streaming/streams-why
- https://learn.microsoft.com/en-us/dotnet/orleans/implementation/streams-implementation/
- https://learn.microsoft.com/en-us/dotnet/api/orleans.streams.iasyncstream-1
- https://learn.microsoft.com/en-us/dotnet/api/orleans.streams.iqueueadapter
- https://learn.microsoft.com/en-us/dotnet/api/orleans.streams.iqueueadapterfactory
- https://learn.microsoft.com/en-us/dotnet/api/orleans.streams.iqueueadapterreceiver
- https://learn.microsoft.com/en-us/dotnet/api/orleans.streams.streamsequencetoken
- https://learn.microsoft.com/en-us/dotnet/api/orleans.runtime.streamid
- https://github.com/dotnet/orleans/tree/main/src/Orleans.Streaming

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Core Concepts](#2-core-concepts)
3. [Stream Identity](#3-stream-identity)
4. [Core Interfaces](#4-core-interfaces)
5. [Producers and Consumers](#5-producers-and-consumers)
6. [Explicit vs Implicit Subscriptions](#6-explicit-vs-implicit-subscriptions)
7. [Stream Providers](#7-stream-providers)
8. [Persistent Streams vs Simple Message Streams](#8-persistent-streams-vs-simple-message-streams)
9. [Broadcast Channels](#9-broadcast-channels)
10. [Stream Lifecycle and Delivery Guarantees](#10-stream-lifecycle-and-delivery-guarantees)
11. [Rewindable Streams](#11-rewindable-streams)
12. [Stream Filtering](#12-stream-filtering)
13. [PubSub System](#13-pubsub-system)
14. [Pulling Agents and Queue Architecture](#14-pulling-agents-and-queue-architecture)
15. [Queue Cache and Backpressure](#15-queue-cache-and-backpressure)
16. [Custom Stream Provider Implementation](#16-custom-stream-provider-implementation)
17. [Stream Sequence Tokens](#17-stream-sequence-tokens)
18. [Failure Recovery](#18-failure-recovery)
19. [Configuration Patterns](#19-configuration-patterns)
20. [Summary of Key Types for Python Implementation](#20-summary-of-key-types-for-python-implementation)

---

## 1. Design Philosophy

Orleans Streaming was designed to address limitations of existing stream processing
systems (Kafka, Event Hubs, Storm, Spark Streaming) which are optimized for uniform
dataflow graphs applied identically to all items. Orleans targets scenarios requiring:

1. **Flexible stream processing logic** -- each stream/consumer can have completely
   different processing logic (stateful or stateless, with side effects, calling
   external APIs). Not limited to declarative dataflow.

2. **Dynamic topologies** -- subscription graphs change at runtime. Users subscribe
   and unsubscribe constantly. Processing logic can evolve per-consumer without
   tearing down the pipeline.

3. **Fine-grained stream granularity** -- each link/node in the topology is a
   logical entity that can be independently managed. Different links can have
   different transports, delivery guarantees, and checkpointing strategies.

4. **Distribution** -- scalable, elastic, reliable, efficient, and responsive
   (near-real-time).

The key insight: streams in Orleans are **virtual** (like grains). A stream always
logically exists, is never explicitly created or destroyed, and can never fail.
Subscriptions are durable and survive activation/deactivation cycles.

---

## 2. Core Concepts

### Streams are Virtual

- A stream always exists. It is not explicitly created or destroyed.
- A stream can never fail (the abstraction never fails; underlying transport may).
- Streams are identified by logical names (StreamId).

### Decoupling in Time and Space

- Producer and consumer may be on different servers or in different time zones.
- The system withstands failures transparently.

### Lightweight and Dynamic

- Designed for a large number of streams that come and go at high rate.
- Stream bindings (subscriptions) are dynamic -- grains connect/disconnect at high rate.

### Uniform Across Grains and Clients

- The same APIs work inside grains and from Orleans external clients.
- No special client-side APIs needed (replaces grain observers for many use cases).

### Transparent Lifecycle Management

- After subscribing, a consumer receives events even across failures.
- The runtime manages all resources for active streams and garbage-collects unused ones.

---

## 3. Stream Identity

Streams are identified by a `StreamId`, which combines a **namespace** (string) and
a **key** (which can be a GUID, string, or long).

### StreamId Structure (Orleans 7+)

```csharp
[GenerateSerializer]
[Immutable]
[Serializable]
public readonly struct StreamId : IComparable<StreamId>, IEquatable<StreamId>,
    ISpanFormattable, ISerializable
{
    // Properties
    ReadOnlyMemory<byte> FullKey { get; }
    ReadOnlyMemory<byte> Key { get; }
    ReadOnlyMemory<byte> Namespace { get; }

    // Factory methods
    static StreamId Create(string ns, Guid key);
    static StreamId Create(string ns, string key);
    static StreamId Create(string ns, long key);
    static StreamId Create(IStreamIdentity identity);
    static StreamId Create(ReadOnlySpan<byte> ns, ReadOnlySpan<byte> key);

    // Utility
    string GetKeyAsString();
    string GetNamespace();
}
```

### Legacy Stream Identity (Orleans 3.x)

In older versions, streams were identified by a `Guid` + `string namespace` pair:

```csharp
public interface IStreamIdentity
{
    Guid Guid { get; }
    string Namespace { get; }
}
```

### Identity Semantics

- The namespace acts like a "type" for the stream (analogous to grain type).
- The key acts like a "primary key" for the stream (analogous to grain key).
- Example: Stream `<PlayerGuid, "PlayerEvents">` vs `<RoomGuid, "ChatMessages">`.
- Two streams with different namespaces but the same key are completely independent.
- Streams with the same namespace and key on different providers are also independent.

### Python Implementation Note

For a Python implementation, `StreamId` should be a frozen/immutable dataclass or
named tuple with `namespace: str` and `key: str` (or `key: Union[str, UUID, int]`).

```python
@dataclass(frozen=True)
class StreamId:
    namespace: str
    key: str  # Normalized to string; could accept UUID or int in factory

    @classmethod
    def create(cls, namespace: str, key) -> "StreamId":
        return cls(namespace=namespace, key=str(key))
```

---

## 4. Core Interfaces

### IAsyncStream<T>

The central abstraction. It is a **distributed rendezvous** between producers and
consumers. It implements both the observer (producer) and observable (consumer)
interfaces. Modeled after Reactive Extensions (Rx) but fully asynchronous.

```csharp
public interface IAsyncStream<T> :
    IAsyncObserver<T>,           // Producer side
    IAsyncObservable<T>,         // Consumer side
    IAsyncBatchObserver<T>,      // Batch producer side
    IAsyncBatchObservable<T>,    // Batch consumer side
    IAsyncBatchProducer<T>,      // Batch producer (newer)
    IComparable<IAsyncStream<T>>,
    IEquatable<IAsyncStream<T>>
{
    // Properties
    bool IsRewindable { get; }
    string ProviderName { get; }
    StreamId StreamId { get; }

    // Own method
    Task<IList<StreamSubscriptionHandle<T>>> GetAllSubscriptionHandles();
}
```

### IAsyncObserver<T> (Producer Interface)

```csharp
public interface IAsyncObserver<in T>
{
    Task OnNextAsync(T item, StreamSequenceToken token = null);
    Task OnCompletedAsync();
    Task OnErrorAsync(Exception ex);
}
```

- `OnNextAsync` -- push a single event to the stream.
- `OnCompletedAsync` -- signal the stream is complete.
- `OnErrorAsync` -- signal an error on the stream.
- The returned Task represents when the event has been accepted (meaning varies by
  provider: for durable queues, it means durably saved; for best-effort, already
  complete).

### IAsyncObservable<T> (Consumer Interface)

```csharp
public interface IAsyncObservable<T>
{
    Task<StreamSubscriptionHandle<T>> SubscribeAsync(IAsyncObserver<T> observer);
    Task<StreamSubscriptionHandle<T>> SubscribeAsync(
        IAsyncObserver<T> observer,
        StreamSequenceToken token,
        StreamFilterPredicate filterFunc = null,
        object filterData = null);
    Task<StreamSubscriptionHandle<T>> SubscribeAsync(
        IAsyncObserver<T> observer,
        StreamSequenceToken token,
        string filterClassName);
}
```

### IAsyncBatchObserver<T>

```csharp
public interface IAsyncBatchObserver<in T>
{
    Task OnNextAsync(IList<SequentialItem<T>> items);
}
```

### IAsyncBatchProducer<T>

```csharp
public interface IAsyncBatchProducer<in T>
{
    Task OnNextBatchAsync(IEnumerable<T> batch, StreamSequenceToken token = null);
}
```

### StreamSubscriptionHandle<T>

An opaque handle representing a subscription. Used to unsubscribe or resume.

```csharp
public abstract class StreamSubscriptionHandle<T> :
    IEquatable<StreamSubscriptionHandle<T>>
{
    // Resume with new observer (after reactivation)
    abstract Task<StreamSubscriptionHandle<T>> ResumeAsync(
        IAsyncObserver<T> observer,
        StreamSequenceToken token = null);

    // Unsubscribe
    abstract Task UnsubscribeAsync();

    // Identity
    abstract StreamId StreamId { get; }
    abstract string ProviderName { get; }
}
```

### IStreamProvider

Factory for obtaining stream handles.

```csharp
public interface IStreamProvider
{
    string Name { get; }
    IAsyncStream<T> GetStream<T>(StreamId streamId);
    bool IsRewindable { get; }
}
```

Grains obtain the provider via:
```csharp
IStreamProvider streamProvider = this.GetStreamProvider("ProviderName");
```

Clients obtain it via:
```csharp
IStreamProvider streamProvider = client.GetStreamProvider("ProviderName");
```

### Python Equivalents

```python
from abc import ABC, abstractmethod
from typing import Generic, TypeVar, Optional, List, Callable, Awaitable

T = TypeVar('T')

class AsyncObserver(ABC, Generic[T]):
    @abstractmethod
    async def on_next(self, item: T, token: Optional['StreamSequenceToken'] = None) -> None: ...

    @abstractmethod
    async def on_completed(self) -> None: ...

    @abstractmethod
    async def on_error(self, error: Exception) -> None: ...


class StreamSubscriptionHandle(ABC, Generic[T]):
    @abstractmethod
    async def resume(self, observer: AsyncObserver[T],
                     token: Optional['StreamSequenceToken'] = None) -> 'StreamSubscriptionHandle[T]': ...

    @abstractmethod
    async def unsubscribe(self) -> None: ...

    @property
    @abstractmethod
    def stream_id(self) -> StreamId: ...


class AsyncObservable(ABC, Generic[T]):
    @abstractmethod
    async def subscribe(self, observer: AsyncObserver[T],
                        token: Optional['StreamSequenceToken'] = None,
                        filter_func: Optional[Callable] = None
                        ) -> StreamSubscriptionHandle[T]: ...


class AsyncStream(AsyncObserver[T], AsyncObservable[T], Generic[T]):
    @property
    @abstractmethod
    def is_rewindable(self) -> bool: ...

    @property
    @abstractmethod
    def stream_id(self) -> StreamId: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    async def get_all_subscription_handles(self) -> List[StreamSubscriptionHandle[T]]: ...


class StreamProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def get_stream(self, stream_id: StreamId) -> AsyncStream: ...

    @property
    @abstractmethod
    def is_rewindable(self) -> bool: ...
```

---

## 5. Producers and Consumers

### Producing Events

1. Get a stream provider reference.
2. Get a stream handle via `GetStream`.
3. Call `OnNextAsync` to push events.

```csharp
// Inside a grain
IStreamProvider streamProvider = this.GetStreamProvider("StreamProvider");
StreamId streamId = StreamId.Create("MyNamespace", this.GetPrimaryKey());
IAsyncStream<int> stream = streamProvider.GetStream<int>(streamId);

// Produce an event
await stream.OnNextAsync(42);

// Produce a batch
await stream.OnNextBatchAsync(new[] { 1, 2, 3 });

// Signal completion
await stream.OnCompletedAsync();
```

### Consuming Events

1. Get a stream handle (same as above).
2. Subscribe with an observer or lambda delegates.

```csharp
// Subscribe with lambdas
StreamSubscriptionHandle<int> handle = await stream.SubscribeAsync(
    async (data, token) => {
        Console.WriteLine(data);
    });

// Subscribe with an IAsyncObserver implementation
StreamSubscriptionHandle<int> handle = await stream.SubscribeAsync(myObserver);

// Unsubscribe
await handle.UnsubscribeAsync();
```

### Multiplicity

- A stream can have **multiple producers** and **multiple consumers**.
- A message published by a producer is delivered to **all** current subscribers.
- A consumer can subscribe to the same stream **multiple times** -- it receives
  each event once per subscription.
- `GetAllSubscriptionHandles()` returns all active subscriptions for the caller.

```csharp
IList<StreamSubscriptionHandle<T>> handles =
    await stream.GetAllSubscriptionHandles();
```

---

## 6. Explicit vs Implicit Subscriptions

### Explicit Subscriptions

- Consumer must call `SubscribeAsync` to subscribe.
- Subscription is durable: survives grain deactivation/reactivation.
- On reactivation, the grain must **re-attach processing logic** via `ResumeAsync`.
- Grain can subscribe multiple times (multiplicity).
- Grain can unsubscribe.

**Pattern for explicit subscription recovery:**

```csharp
public override async Task OnActivateAsync(CancellationToken cancellationToken)
{
    var streamProvider = this.GetStreamProvider("ProviderName");
    var streamId = StreamId.Create("MyNamespace", this.GetPrimaryKey());
    var stream = streamProvider.GetStream<string>(streamId);

    // Re-attach to all existing subscriptions
    var handles = await stream.GetAllSubscriptionHandles();
    foreach (var handle in handles)
    {
        await handle.ResumeAsync(this);  // or new observer instance
    }
}
```

### Implicit Subscriptions

- Declared via attribute: `[ImplicitStreamSubscription("Namespace")]`
- The runtime automatically subscribes grains based on their type and identity.
- Mapping rule: stream `<GUID, Namespace>` maps to grain `<GUID, GrainType>`
  where `GrainType` has `[ImplicitStreamSubscription("Namespace")]`.
- No multiplicity: exactly one implicit subscription per stream namespace per grain type.
- Cannot unsubscribe (subscription is permanent for the grain type).
- Grain still must attach processing logic on activation.

**Implicit subscription grain pattern (Orleans 7+):**

```csharp
[ImplicitStreamSubscription("RANDOMDATA")]
public class ReceiverGrain : Grain, IReceiverGrain, IAsyncObserver<int>,
    IStreamSubscriptionObserver
{
    // Called when implicit subscription is activated
    public async Task OnSubscribed(IStreamSubscriptionHandleFactory handleFactory)
    {
        var handle = handleFactory.Create<int>();
        await handle.ResumeAsync(this);
    }

    public Task OnNextAsync(int item, StreamSequenceToken token = null)
    {
        Console.WriteLine($"Received: {item}");
        return Task.CompletedTask;
    }

    public Task OnCompletedAsync() => Task.CompletedTask;
    public Task OnErrorAsync(Exception ex) => Task.CompletedTask;
}
```

**Alternative pattern (attach in OnActivateAsync):**

```csharp
[ImplicitStreamSubscription("RANDOMDATA")]
public class ReceiverGrain : Grain, IReceiverGrain
{
    public override async Task OnActivateAsync(CancellationToken ct)
    {
        var streamProvider = this.GetStreamProvider("StreamProvider");
        var streamId = StreamId.Create("RANDOMDATA", this.GetPrimaryKey());
        var stream = streamProvider.GetStream<int>(streamId);
        await stream.SubscribeAsync(
            async (data, token) => Console.WriteLine(data));
    }
}
```

### Key Differences Summary

| Aspect | Explicit | Implicit |
|---|---|---|
| How subscribed | `SubscribeAsync()` call | `[ImplicitStreamSubscription]` attribute |
| Multiplicity | Multiple subscriptions allowed | Exactly one per namespace per grain type |
| Unsubscribe | Yes, via `UnsubscribeAsync()` | No |
| Triggers activation | No (grain must be active) | Yes (stream event activates grain) |
| Recovery | Must call `ResumeAsync` | Must attach observer (no need to resume) |
| Producer awareness | Producer may need to know consumer | Producer needs no knowledge of consumers |

### Python Implementation Note

Implicit subscriptions should be implemented as a class decorator or metadata:

```python
def implicit_stream_subscription(namespace: str):
    def decorator(cls):
        if not hasattr(cls, '_stream_subscriptions'):
            cls._stream_subscriptions = []
        cls._stream_subscriptions.append(namespace)
        return cls
    return decorator

@implicit_stream_subscription("RANDOMDATA")
class ReceiverGrain(Grain):
    async def on_next(self, item, token=None):
        print(f"Received: {item}")
```

---

## 7. Stream Providers

Stream providers are the extensibility point for the streaming system. They determine
how events are transported, stored, and delivered.

### Provider Types

#### 1. Memory Streams (In-Memory Provider)

- Uses direct grain messaging to deliver events (no external queue).
- Not durable -- events lost on silo restart.
- Suitable only for development and testing.
- Replaced SMS (Simple Message Streams) in Orleans 7+.

```csharp
// Silo configuration
siloBuilder.AddMemoryStreams("StreamProvider")
    .AddMemoryGrainStorage("PubSubStore");

// Client configuration
clientBuilder.AddMemoryStreams("StreamProvider");
```

#### 2. Azure Event Hubs Provider

- Uses Azure Event Hubs for durable, high-throughput event ingestion.
- **Rewindable** -- supports subscribing from arbitrary point in time.
- Handles millions of events per second.
- Partitioned (maps to Orleans queue concept).
- NuGet: `Microsoft.Orleans.Streaming.EventHubs`

#### 3. Azure Queue Storage Provider

- Uses Azure Storage Queues for durable event delivery.
- **Not rewindable**.
- Does not guarantee FIFO order in failure cases.
- At-least-once delivery semantics.
- Uses pulling agents on the consumer side.
- NuGet: `Microsoft.Orleans.Streaming.AzureStorage`

```csharp
siloBuilder
    .AddAzureQueueStreams("AzureQueueProvider",
        optionsBuilder => optionsBuilder.ConfigureAzureQueue(
            options => options.Configure(
                opt => opt.QueueServiceClient = new QueueServiceClient(endpoint, credential))))
    .AddAzureTableGrainStorage("PubSubStore",
        options => options.TableServiceClient = new TableServiceClient(endpoint, credential));
```

#### 4. Amazon SQS Provider

- Uses Amazon Simple Queue Service.
- Similar architecture to Azure Queue provider (pulling agents).
- Implemented via `SQSAdapterFactory`.

#### 5. Simple Message Streams (SMS) -- Orleans 3.x Legacy

- Direct grain-to-grain messaging over TCP.
- Not durable, best-effort at-most-once delivery.
- Producer's `OnNextAsync` returns a Task representing consumer processing status.
- Producer can implement application-level retry.
- Replaced by Memory Streams and Broadcast Channels in Orleans 7+.

```csharp
// Orleans 3.x
hostBuilder.AddSimpleMessageStreamProvider("SMSProvider")
    .AddMemoryGrainStorage("PubSubStore");
```

### Provider Architecture

All persistent stream providers share the `PersistentStreamProvider` base, which is
parameterized with:

- `IQueueAdapterFactory` -- creates queue adapters for specific technologies
- `IStreamQueueMapper` -- maps streams to queues
- `IStreamQueueBalancer` -- balances queues across silos

```
PersistentStreamProvider
    |
    +-- IQueueAdapterFactory
    |       |
    |       +-- CreateAdapter() -> IQueueAdapter
    |       +-- GetStreamQueueMapper() -> IStreamQueueMapper
    |       +-- GetQueueAdapterCache() -> IQueueAdapterCache
    |       +-- GetDeliveryFailureHandler(QueueId) -> IStreamFailureHandler
    |
    +-- Pulling Agents (one per queue partition)
    |       |
    |       +-- IQueueAdapterReceiver (per queue)
    |       +-- IQueueCache (per agent)
    |
    +-- PubSub (subscription tracking)
```

---

## 8. Persistent Streams vs Simple Message Streams

### Simple Message Streams (SMS / Memory Streams)

- Events delivered via direct grain calls (TCP).
- No backing queue or storage.
- Producer's `OnNextAsync` completes when the consumer has processed the event
  (unless FireAndForget mode).
- Best-effort at-most-once delivery.
- No rewindability.
- PubSub binding (subscription tracking) IS reliable even though message delivery
  is best-effort.

### Persistent Streams

- Events go through a durable queue (Event Hubs, Azure Queues, SQS, etc.).
- Decouples producer from consumer via the queue.
- Uses **pulling agents** to dequeue and deliver to consumers.
- At-least-once delivery (events are only deleted from queue after successful delivery).
- Some providers support rewindability (Event Hubs).
- Supports backpressure via queue cache.

### When to Use Which

| Scenario | Recommended |
|---|---|
| Development/testing | Memory Streams |
| Low-latency, same-cluster, loss-acceptable | Memory Streams |
| Durable, cross-failure delivery | Persistent Streams |
| High-throughput event ingestion | Event Hubs provider |
| Simple durable queuing | Azure Queue provider |
| Replay from history | Event Hubs (rewindable) |

---

## 9. Broadcast Channels

Broadcast channels (Orleans 7+) are a specialized mechanism distinct from streams:

### Key Differences from Streams

| Feature | Broadcast Channels | Orleans Streams |
|---|---|---|
| Subscription model | Implicit only (automatic) | Explicit or implicit |
| Persistence | Not persistent | Can be persistent |
| Delivery guarantee | Best-effort, fire-and-forget | Provider-dependent |
| Message history | None | Rewindable with some providers |
| Configuration | Simple -- channel name only | Requires stream provider config |

### Broadcast Channel Interfaces

```csharp
// Consumer grain implements this
public interface IOnBroadcastChannelSubscribed
{
    Task OnSubscribed(IBroadcastChannelSubscription subscription);
}

public interface IBroadcastChannelSubscription
{
    Task Attach<T>(Func<T, Task> onPublished, Func<Exception, Task> onError = null);
}

// Producer uses this
public interface IBroadcastChannelWriter<T>
{
    Task Publish(T item);
}

public interface IBroadcastChannelProvider
{
    IBroadcastChannelWriter<T> GetChannelWriter<T>(ChannelId channelId);
}
```

### Usage Pattern

```csharp
// Configuration
siloBuilder.AddBroadcastChannel("LiveStockTicker");

// Consumer grain
[ImplicitChannelSubscription]
public class StockGrain : Grain, IStockGrain, IOnBroadcastChannelSubscribed
{
    public Task OnSubscribed(IBroadcastChannelSubscription subscription) =>
        subscription.Attach<Stock>(OnStockUpdated, OnError);

    private Task OnStockUpdated(Stock stock) { /* handle */ return Task.CompletedTask; }
    private static Task OnError(Exception ex) { /* handle */ return Task.CompletedTask; }
}

// Producer (e.g., background service)
IBroadcastChannelProvider provider =
    clusterClient.GetBroadcastChannelProvider("LiveStockTicker");
ChannelId channelId = ChannelId.Create("LiveStockTicker", Guid.Empty);
IBroadcastChannelWriter<Stock> writer = provider.GetChannelWriter<Stock>(channelId);
await writer.Publish(stockUpdate);
```

---

## 10. Stream Lifecycle and Delivery Guarantees

### Stream Subscription Semantics

- **Sequential Consistency**: once a subscription Task resolves successfully, the
  consumer will see ALL events generated after the subscription point.
- For rewindable streams, the consumer can specify a `StreamSequenceToken` to
  subscribe from an arbitrary past point.

### Delivery Guarantees by Provider

| Provider | Delivery Guarantee | FIFO Order |
|---|---|---|
| Memory Streams | Best-effort, at-most-once | Yes (if producer awaits OnNextAsync) |
| Azure Queue | At-least-once | Yes in normal operation; may break on failure |
| Event Hubs | At-least-once | Yes within partition |
| SMS (3.x) | Best-effort, at-most-once | Yes (if producer awaits OnNextAsync) |
| Broadcast Channel | Best-effort, fire-and-forget | Yes |

### Subscription Durability

- Subscriptions are **durable** -- they survive grain deactivation.
- A subscription belongs to a **grain identity**, not a specific activation.
- If a subscribed grain deactivates and a new event arrives, the grain is
  automatically **reactivated** to receive it.
- The grain must re-attach processing logic (`ResumeAsync`) on reactivation.

### Producer Lifecycle

- Producers do not need special recovery logic.
- If a producer grain deactivates and later reactivates, it just gets a new stream
  handle and produces as usual.

### Consumer Lifecycle

1. Consumer subscribes -> gets `StreamSubscriptionHandle`.
2. Consumer processes events via `IAsyncObserver` callbacks.
3. If consumer grain deactivates: subscription persists in PubSub.
4. When new event arrives: grain reactivates automatically.
5. In `OnActivateAsync`, grain calls `GetAllSubscriptionHandles()` and
   `ResumeAsync()` on each handle to re-attach processing logic.
6. Events that arrived during deactivation may be replayed (persistent streams)
   or lost (non-persistent streams).

---

## 11. Rewindable Streams

### Concept

A rewindable stream supports subscribing from a previous point in time using a
`StreamSequenceToken`. This enables:

- Recovery without losing events (checkpoint + rewind).
- Replay of historical events.
- Late-joining consumers catching up.

### Which Providers Support Rewindability

| Provider | Rewindable |
|---|---|
| Event Hubs | Yes (up to retention period) |
| Azure Queues | No |
| Memory Streams | No |
| SMS (3.x) | No |
| Broadcast Channels | No |

### Usage

```csharp
// Subscribe from a specific point
StreamSequenceToken checkpointToken = LoadCheckpoint();
var handle = await stream.SubscribeAsync(observer, checkpointToken);

// Resume from a specific point after reactivation
await handle.ResumeAsync(observer, checkpointToken);
```

### Recovery Pattern with Rewindable Streams

1. Consumer grain periodically checkpoints its state + latest sequence token.
2. On recovery (reactivation), load the checkpoint.
3. Re-subscribe or resume from the checkpointed token.
4. All events since the checkpoint are replayed.
5. No events are lost.

### Stream Property

```csharp
bool isRewindable = stream.IsRewindable;
// Also on the adapter:
bool isRewindable = queueAdapter.IsRewindable;
```

---

## 12. Stream Filtering

Orleans supports server-side stream filtering via `StreamFilterPredicate`.

### StreamFilterPredicate Delegate

```csharp
public delegate bool StreamFilterPredicate(
    IStreamIdentity stream,
    object filterData,
    object item);
```

- `stream` -- identity of the stream being filtered.
- `filterData` -- arbitrary data passed by the subscriber at subscription time.
- `item` -- the actual event being checked.
- Returns `true` to deliver the event, `false` to skip it.

### Usage in Subscription

```csharp
// Subscribe with a filter
StreamFilterPredicate filter = (stream, filterData, item) =>
{
    int threshold = (int)filterData;
    return (int)item > threshold;
};

var handle = await stream.SubscribeAsync(
    observer,
    token: null,
    filterFunc: filter,
    filterData: 50  // Only receive items > 50
);
```

### Alternative: Filter by Class Name

```csharp
var handle = await stream.SubscribeAsync(
    observer,
    token: null,
    filterClassName: "MyApp.Filters.MyCustomFilter");
```

This loads the filter class by name, which must implement the filtering logic.

### Important Notes

- Filtering happens on the **delivery side** (before passing to consumer), not on
  the queue side.
- For persistent streams, the event is still dequeued from the queue even if
  filtered out -- filtering does not reduce queue load.
- The filter predicate must be serializable (it is stored in the PubSub subscription).
- Filter data must also be serializable.

### Python Implementation Note

```python
# Filter as a callable
StreamFilterPredicate = Callable[[StreamId, Any, Any], bool]

async def subscribe(self, observer, token=None,
                    filter_func: Optional[StreamFilterPredicate] = None,
                    filter_data: Any = None) -> StreamSubscriptionHandle:
    ...
```

---

## 13. PubSub System

### Overview

The PubSub (Publish-Subscribe) system is the runtime component that tracks all
stream subscriptions. It serves as the rendezvous point between producers and
consumers.

### Implementation

- PubSub is implemented as **grains** called `PubSubRendezvousGrain`.
- These grains use Orleans declarative persistence.
- The storage provider is named `"PubSubStore"`.
- Applications choose the storage backend for PubSub data.

### PubSub Responsibilities

1. **Track subscriptions**: maintain a registry of which consumers are subscribed
   to which streams.
2. **Match producers to consumers**: when a pulling agent needs to deliver events,
   it queries PubSub for the consumer list.
3. **Persist subscriptions**: subscriptions survive silo restarts (if backed by
   durable storage).
4. **Notify agents**: when new consumers subscribe, PubSub notifies the relevant
   pulling agent so it can start delivering to the new consumer.

### PubSub Data Flow

```
Producer -> Queue -> Pulling Agent -> PubSub (lookup consumers) -> Consumers
                                  |
                                  +-> Agent caches consumer list locally
                                  +-> Agent subscribes to PubSub for updates
```

### Storage Configuration

```csharp
// Use Azure Table Storage for PubSub
siloBuilder.AddAzureTableGrainStorage("PubSubStore",
    options => options.TableServiceClient = new TableServiceClient(endpoint, credential));

// Use memory storage (development only)
siloBuilder.AddMemoryGrainStorage("PubSubStore");
```

### PubSub Guarantees

- PubSub provides **strong streaming subscription semantics**.
- The handshake between pulling agents and PubSub guarantees that once a consumer
  subscribes, it will see all events generated after the subscription.
- PubSub is itself fault-tolerant (implemented as persistent grains).

### How Implicit Subscriptions Work with PubSub

For implicit subscriptions:
1. The runtime discovers grain types with `[ImplicitStreamSubscription]` at startup.
2. When a pulling agent dequeues an event for stream `<Key, Namespace>`, it checks
   the implicit subscription registry.
3. It activates the target grain `<Key, GrainType>` and delivers the event.
4. No explicit PubSub entry is needed for implicit subscriptions -- the mapping is
   computed from type metadata.

### Python Implementation Note

The PubSub system should be implemented as a special grain type with persistent state:

```python
class PubSubState:
    subscriptions: Dict[StreamId, List[SubscriptionInfo]]

class SubscriptionInfo:
    consumer_id: GrainId
    filter_func: Optional[Callable]
    filter_data: Any
    sequence_token: Optional[StreamSequenceToken]

class PubSubRendezvousGrain(Grain):
    """One instance per stream. Tracks all subscriptions for that stream."""
    state: PubSubState

    async def register_consumer(self, stream_id, consumer_id, filter_func, filter_data, token):
        # Add to subscriptions, persist state
        ...

    async def unregister_consumer(self, stream_id, consumer_id):
        # Remove from subscriptions, persist state
        ...

    async def get_all_consumers(self, stream_id) -> List[SubscriptionInfo]:
        ...
```

---

## 14. Pulling Agents and Queue Architecture

### Pulling Agents

Pulling agents are the core mechanism for persistent streams. They:

1. Run inside silos (alongside application grains).
2. Are implemented as **SystemTargets** (internal runtime grains).
3. Are subject to single-threaded concurrency (like grains).
4. Use regular Orleans grain messaging.
5. Are as lightweight as grains -- creating one is as cheap as creating a grain.

### One Agent Per Queue Partition

- Each pulling agent is responsible for one queue partition.
- The agent runs a periodic timer that calls `IQueueAdapterReceiver.GetQueueMessagesAsync`.
- Dequeued messages are placed in the agent's `IQueueCache`.
- For each message, the agent inspects the stream ID and looks up consumers via PubSub.

### StreamQueueMapper

`IStreamQueueMapper` provides:
1. List of all queues.
2. Mapping from stream IDs to queue IDs (determines which queue a stream's events go to).

```csharp
public interface IStreamQueueMapper
{
    IEnumerable<QueueId> GetAllQueues();
    QueueId GetQueueForStream(StreamId streamId);
}
```

The default implementation (`HashRingStreamQueueMapper`) uses consistent hashing.

### StreamQueueBalancer

`IStreamQueueBalancer` distributes queues across silos:
- Prevents bottlenecks.
- Supports elasticity (rebalancing when silos join/leave).
- Several built-in implementations for different scales (small/large queue counts)
  and environments (Azure, on-prem, static).

```csharp
public interface IStreamQueueBalancer
{
    IEnumerable<QueueId> GetMyQueues();
    // ... balancing and rebalancing methods
}
```

### Configuration Example

```csharp
hostBuilder.AddPersistentStreams(
    "StreamProviderName",
    GeneratorAdapterFactory.Create,
    providerConfigurator =>
        providerConfigurator
            .Configure<HashRingStreamQueueMapperOptions>(
                ob => ob.Configure(options => options.TotalQueueCount = 8))
            .UseDynamicClusterConfigDeploymentBalancer());
```

### Pulling Protocol Detail

```
Timer fires -> Agent calls receiver.GetQueueMessagesAsync(maxCount)
           -> Messages placed in IQueueCache
           -> For each message:
                - Extract StreamId
                - Lookup consumers via PubSub (cached locally)
                - Deliver to each consumer (one at a time, await each)
                - When all consumers processed: mark as deliverable for deletion
           -> Agent subscribes to PubSub for new consumer notifications
```

### Python Implementation Note

```python
class PullingAgent:
    """One per queue partition. Runs as a system-level async task."""
    def __init__(self, queue_id: QueueId, receiver: QueueAdapterReceiver,
                 cache: QueueCache, pubsub: PubSubRendezvousGrain):
        self.queue_id = queue_id
        self.receiver = receiver
        self.cache = cache
        self.pubsub = pubsub

    async def run(self):
        while True:
            messages = await self.receiver.get_queue_messages(max_count=100)
            for msg in messages:
                self.cache.add(msg)
                consumers = await self.pubsub.get_all_consumers(msg.stream_id)
                for consumer in consumers:
                    await consumer.deliver(msg)
            await asyncio.sleep(self.poll_interval)
```

---

## 15. Queue Cache and Backpressure

### IQueueCache

Per-agent data structure that decouples dequeuing from delivery:

```csharp
public interface IQueueCache
{
    // Add messages to cache
    void AddToCache(IList<IBatchContainer> messages);

    // Get a cursor for a specific consumer
    IQueueCacheCursor GetCacheCursor(StreamId streamId, StreamSequenceToken token);

    // Check if cache is under pressure
    bool IsUnderPressure();

    // Get max addable count
    int GetMaxAddCount();
}
```

### IQueueCacheCursor

Per-consumer cursor that tracks delivery progress independently:

```csharp
public interface IQueueCacheCursor
{
    bool MoveNext();
    IBatchContainer GetCurrent(out Exception exception);
    void Refresh(StreamSequenceToken token);
}
```

### Why a Cache?

Consider 3 consumers on the same stream, one of which is slow:
- Without cache: slow consumer blocks dequeuing, affecting all consumers.
- With cache: events are buffered. Each consumer has its own cursor and progresses
  independently. Fast consumers get events immediately; slow consumers catch up later.

### Backpressure Mechanism

Two levels of backpressure:

1. **Agent to Consumer**: Standard Orleans grain messaging. Events delivered one at a
   time (or in limited batches), awaiting each delivery. Natural rate limiting per
   consumer.

2. **Queue to Agent (Cache-based)**: When the cache fills up (due to slow consumers),
   the agent **slows the dequeue rate** rather than dropping events:
   - Cache has a configurable maximum size.
   - Cache never throws away undelivered events.
   - When cache pressure is detected, dequeue frequency decreases.
   - When pressure is relieved, dequeue rate returns to normal.
   - Cache uses internal "cache buckets" to track per-consumer delivery progress
     for responsive, self-adjusting behavior.

### IBatchContainer

The unit of data flowing through the queue system:

```csharp
public interface IBatchContainer
{
    StreamId StreamId { get; }
    StreamSequenceToken SequenceToken { get; }

    IEnumerable<Tuple<T, StreamSequenceToken>> GetEvents<T>();
    bool ImportRequestContext();
}
```

---

## 16. Custom Stream Provider Implementation

To create a custom stream provider, implement the queue adapter pattern.

### Required Interfaces

#### IQueueAdapterFactory

```csharp
public interface IQueueAdapterFactory
{
    // Create the adapter
    Task<IQueueAdapter> CreateAdapter();

    // Create the queue mapper
    IStreamQueueMapper GetStreamQueueMapper();

    // Create the cache (for persistent streams)
    IQueueAdapterCache GetQueueAdapterCache();

    // Get failure handler for a queue
    Task<IStreamFailureHandler> GetDeliveryFailureHandler(QueueId queueId);
}
```

#### IQueueAdapter

```csharp
public interface IQueueAdapter
{
    string Name { get; }
    bool IsRewindable { get; }
    StreamProviderDirection Direction { get; }  // ReadOnly, WriteOnly, ReadWrite

    // Enqueue events
    Task QueueMessageBatchAsync<T>(
        StreamId streamId,
        IEnumerable<T> events,
        StreamSequenceToken token,
        Dictionary<string, object> requestContext);

    // Create a receiver for a queue partition
    IQueueAdapterReceiver CreateReceiver(QueueId queueId);
}
```

#### IQueueAdapterReceiver

```csharp
public interface IQueueAdapterReceiver
{
    // Initialize
    Task Initialize(TimeSpan timeout);

    // Pull messages from the queue
    Task<IList<IBatchContainer>> GetQueueMessagesAsync(int maxCount);

    // Acknowledge delivered messages
    Task MessagesDeliveredAsync(IList<IBatchContainer> messages);

    // Shutdown
    Task Shutdown(TimeSpan timeout);
}
```

#### IQueueAdapterCache

```csharp
public interface IQueueAdapterCache
{
    IQueueCache CreateQueueCache(QueueId queueId);
}
```

### Registration Pattern

```csharp
// Register custom stream provider
hostBuilder.AddPersistentStreams(
    "MyStreamProvider",
    MyQueueAdapterFactory.Create,
    providerConfigurator =>
    {
        providerConfigurator.Configure<HashRingStreamQueueMapperOptions>(
            ob => ob.Configure(options => options.TotalQueueCount = 4));
    });
```

### Existing Implementations (as reference)

- `MemoryAdapterFactory<TSerializer>` -- in-memory streams
- `AzureQueueAdapterFactory` -- Azure Queue Storage
- `EventHubAdapterFactory` -- Azure Event Hubs
- `SQSAdapterFactory` -- Amazon SQS
- `GeneratorAdapterFactory` -- test data generator

### StreamProviderDirection Enum

```csharp
public enum StreamProviderDirection
{
    ReadOnly,
    WriteOnly,
    ReadWrite
}
```

### Python Implementation Skeleton

```python
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

class QueueId:
    """Identifies a specific queue partition."""
    def __init__(self, provider_name: str, queue_number: int):
        self.provider_name = provider_name
        self.queue_number = queue_number

class BatchContainer(ABC):
    """Unit of data in the queue."""
    @property
    @abstractmethod
    def stream_id(self) -> StreamId: ...

    @property
    @abstractmethod
    def sequence_token(self) -> Optional[StreamSequenceToken]: ...

    @abstractmethod
    def get_events(self) -> List[Any]: ...

class QueueAdapterReceiver(ABC):
    @abstractmethod
    async def initialize(self, timeout: float) -> None: ...

    @abstractmethod
    async def get_queue_messages(self, max_count: int) -> List[BatchContainer]: ...

    @abstractmethod
    async def messages_delivered(self, messages: List[BatchContainer]) -> None: ...

    @abstractmethod
    async def shutdown(self, timeout: float) -> None: ...

class QueueAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_rewindable(self) -> bool: ...

    @abstractmethod
    async def queue_message_batch(self, stream_id: StreamId,
                                   events: List[Any],
                                   token: Optional[StreamSequenceToken] = None,
                                   context: Optional[Dict] = None) -> None: ...

    @abstractmethod
    def create_receiver(self, queue_id: QueueId) -> QueueAdapterReceiver: ...

class StreamQueueMapper(ABC):
    @abstractmethod
    def get_all_queues(self) -> List[QueueId]: ...

    @abstractmethod
    def get_queue_for_stream(self, stream_id: StreamId) -> QueueId: ...

class QueueCache(ABC):
    @abstractmethod
    def add_to_cache(self, messages: List[BatchContainer]) -> None: ...

    @abstractmethod
    def get_cache_cursor(self, stream_id: StreamId,
                          token: Optional[StreamSequenceToken] = None) -> 'QueueCacheCursor': ...

    @abstractmethod
    def is_under_pressure(self) -> bool: ...

class QueueAdapterFactory(ABC):
    @abstractmethod
    async def create_adapter(self) -> QueueAdapter: ...

    @abstractmethod
    def get_stream_queue_mapper(self) -> StreamQueueMapper: ...

    @abstractmethod
    def get_queue_adapter_cache(self) -> Optional[Any]: ...
```

---

## 17. Stream Sequence Tokens

### StreamSequenceToken

Abstract base class representing a position in a stream.

```csharp
[Serializable]
[GenerateSerializer]
public abstract class StreamSequenceToken :
    IComparable<StreamSequenceToken>,
    IEquatable<StreamSequenceToken>
{
    // Number of event batches prior to this batch
    public abstract long SequenceNumber { get; }

    // Number of events within this batch prior to this event
    public abstract int EventIndex { get; }

    public abstract int CompareTo(StreamSequenceToken other);
    public abstract bool Equals(StreamSequenceToken other);
}
```

### EventSequenceToken (Common Implementation)

```csharp
public class EventSequenceToken : StreamSequenceToken
{
    public long SequenceNumber { get; set; }
    public int EventIndex { get; set; }

    public EventSequenceToken(long seqNumber, int eventIndex = 0) { ... }
}
```

### Usage Patterns

```csharp
// Subscribe from a specific token
await stream.SubscribeAsync(observer, savedToken);

// Resume from a specific token
await handle.ResumeAsync(observer, savedToken);

// Producer can pass token with events
await stream.OnNextAsync(data, myToken);

// Consumer receives token with each event
Task OnNextAsync(T item, StreamSequenceToken token)
{
    // token tells you the position of this event
    SaveCheckpoint(token);
}
```

### Extension Methods

```csharp
// Compare tokens
bool isNewer = token1.Newer(token2);
bool isOlder = token1.Older(token2);
```

### Python Implementation

```python
@dataclass(frozen=True)
class StreamSequenceToken:
    sequence_number: int
    event_index: int = 0

    def __lt__(self, other: 'StreamSequenceToken') -> bool:
        if self.sequence_number != other.sequence_number:
            return self.sequence_number < other.sequence_number
        return self.event_index < other.event_index

    def __eq__(self, other: 'StreamSequenceToken') -> bool:
        return (self.sequence_number == other.sequence_number and
                self.event_index == other.event_index)

    def __le__(self, other): return self < other or self == other
    def __gt__(self, other): return not self <= other
    def __ge__(self, other): return not self < other

    def newer_than(self, other: 'StreamSequenceToken') -> bool:
        return self > other

    def older_than(self, other: 'StreamSequenceToken') -> bool:
        return self < other
```

---

## 18. Failure Recovery

### Producer Recovery

- No special action needed.
- Get a new stream handle and produce as normal.

### Consumer Recovery (Explicit Subscriptions)

```
1. Grain reactivates (triggered by new event or external call)
2. OnActivateAsync:
   a. Get stream provider and stream handle
   b. Call stream.GetAllSubscriptionHandles()
   c. For each handle:
      - Option A: handle.ResumeAsync(observer)       -- resume from latest
      - Option B: handle.ResumeAsync(observer, token) -- resume from checkpoint
      - Option C: handle.UnsubscribeAsync()           -- if no longer interested
3. Events start flowing to the new observer
```

### Consumer Recovery (Implicit Subscriptions)

```
1. Grain activates (triggered by stream event via implicit subscription)
2. OnSubscribed(IStreamSubscriptionHandleFactory):
   a. Create handle from factory
   b. handle.ResumeAsync(this)
3. Events start flowing
```

OR (alternative pattern):

```
1. OnActivateAsync:
   a. Get stream and subscribe (attaches processing logic)
2. Events start flowing
```

### Checkpointing Pattern for Rewindable Streams

```csharp
public class CheckpointingGrain : Grain, IAsyncObserver<MyEvent>
{
    private StreamSequenceToken _lastToken;
    private MyState _state;

    public override async Task OnActivateAsync(CancellationToken ct)
    {
        // Load checkpoint
        (_state, _lastToken) = await LoadCheckpoint();

        var stream = GetStreamProvider("EventHubs")
            .GetStream<MyEvent>(StreamId.Create("Events", this.GetPrimaryKey()));

        var handles = await stream.GetAllSubscriptionHandles();
        foreach (var handle in handles)
        {
            // Resume from checkpointed position
            await handle.ResumeAsync(this, _lastToken);
        }
    }

    public async Task OnNextAsync(MyEvent item, StreamSequenceToken token)
    {
        _state.Process(item);
        _lastToken = token;

        // Periodically checkpoint
        if (ShouldCheckpoint())
            await SaveCheckpoint(_state, _lastToken);
    }
}
```

---

## 19. Configuration Patterns

### Memory Streams (Development)

```csharp
// Silo
siloBuilder
    .AddMemoryStreams("StreamProvider")
    .AddMemoryGrainStorage("PubSubStore");

// Client
clientBuilder.AddMemoryStreams("StreamProvider");
```

### Azure Queue Streams

```csharp
siloBuilder
    .AddAzureQueueStreams("AzureQueueProvider",
        optionsBuilder => optionsBuilder.ConfigureAzureQueue(
            options => options.Configure(
                opt => opt.QueueServiceClient = new QueueServiceClient(endpoint, credential))))
    .AddAzureTableGrainStorage("PubSubStore",
        options => options.TableServiceClient = new TableServiceClient(endpoint, credential));
```

### Azure Event Hubs

```csharp
siloBuilder.AddEventHubStreams("EventHubProvider",
    configurator =>
    {
        configurator.ConfigureEventHub(ob => ob.Configure(options =>
        {
            options.ConfigureEventHubConnection(connectionString, "MyEventHub",
                "$Default");
        }));
        configurator.UseAzureTableCheckpointer(ob => ob.Configure(options =>
        {
            options.TableServiceClient = new TableServiceClient(endpoint, credential);
        }));
    });
```

### Custom Persistent Streams

```csharp
siloBuilder.AddPersistentStreams(
    "MyProvider",
    MyAdapterFactory.Create,
    configurator =>
    {
        configurator.Configure<HashRingStreamQueueMapperOptions>(
            ob => ob.Configure(opt => opt.TotalQueueCount = 8));
        configurator.UseDynamicClusterConfigDeploymentBalancer();
    });
```

### .NET Aspire Integration

```csharp
// AppHost
var storage = builder.AddAzureStorage("storage");
var queues = storage.AddQueues("streaming");
var orleans = builder.AddOrleans("cluster")
    .WithClustering(builder.AddRedis("redis"))
    .WithStreaming("AzureQueueProvider", queues);

// Silo
builder.AddKeyedAzureQueueServiceClient("streaming");
builder.UseOrleans();
```

---

## 20. Summary of Key Types for Python Implementation

### Core Value Types

| Orleans Type | Python Equivalent | Purpose |
|---|---|---|
| `StreamId` | `@dataclass(frozen=True)` | Identifies a stream (namespace + key) |
| `StreamSequenceToken` | `@dataclass(frozen=True)` | Position in a stream |
| `QueueId` | `@dataclass(frozen=True)` | Identifies a queue partition |
| `ChannelId` | `@dataclass(frozen=True)` | Identifies a broadcast channel |

### Core Abstract Interfaces

| Orleans Interface | Purpose |
|---|---|
| `IAsyncObserver<T>` | Consumer callback: on_next, on_error, on_completed |
| `IAsyncObservable<T>` | Subscribe to events |
| `IAsyncStream<T>` | Combined producer + consumer handle |
| `StreamSubscriptionHandle<T>` | Manage a specific subscription |
| `IStreamProvider` | Factory for streams |

### Queue/Provider Interfaces

| Orleans Interface | Purpose |
|---|---|
| `IQueueAdapter` | Enqueue events, create receivers |
| `IQueueAdapterReceiver` | Dequeue events from a queue partition |
| `IQueueAdapterFactory` | Create adapter, mapper, cache |
| `IQueueAdapterCache` | Create per-queue caches |
| `IQueueCache` | Buffer and decouple dequeue from delivery |
| `IQueueCacheCursor` | Per-consumer delivery progress tracker |
| `IBatchContainer` | Unit of data in queue (stream_id + events + token) |
| `IStreamQueueMapper` | Map streams to queue partitions |
| `IStreamQueueBalancer` | Distribute queues across silos |
| `IStreamFailureHandler` | Handle delivery failures |

### Subscription/PubSub Types

| Orleans Type | Purpose |
|---|---|
| `PubSubRendezvousGrain` | Tracks subscriptions per stream |
| `ImplicitStreamSubscriptionAttribute` | Declare implicit subscriptions |
| `IStreamSubscriptionObserver` | Callback for implicit subscription setup |
| `StreamFilterPredicate` | Filter events before delivery |

### Broadcast Channel Types

| Orleans Type | Purpose |
|---|---|
| `IBroadcastChannelProvider` | Get channel writers |
| `IBroadcastChannelWriter<T>` | Publish to broadcast channel |
| `IOnBroadcastChannelSubscribed` | Consumer callback for broadcast setup |
| `IBroadcastChannelSubscription` | Attach handler to broadcast channel |
| `ImplicitChannelSubscriptionAttribute` | Declare implicit channel subscription |

### Architectural Components

```
                            +-------------------+
                            |   Application     |
                            |   (Grains/Client) |
                            +--------+----------+
                                     |
                          IAsyncStream<T> API
                          (produce / subscribe)
                                     |
                            +--------v----------+
                            |  Stream Provider   |
                            |  (named instance)  |
                            +--------+----------+
                                     |
                    +----------------+----------------+
                    |                                 |
            +-------v--------+              +---------v---------+
            | PubSub System  |              | Queue Adapter     |
            | (subscriptions)|              | (IQueueAdapter)   |
            +-------+--------+              +---------+---------+
                    |                                 |
            +-------v--------+    +----------+--------v---------+
            | PubSubRendez-  |    |          |                  |
            | vousGrain      |    | Enqueue  |    Pulling       |
            | (per stream)   |    |          |    Agents        |
            +----------------+    +----+     |  (per partition) |
                                       |     +--------+---------+
                                       |              |
                                  +----v----+   +-----v------+
                                  | Queue   |   | QueueCache |
                                  | Storage |   | (per agent)|
                                  +---------+   +-----+------+
                                                      |
                                                +-----v------+
                                                | Cursors    |
                                                | (per       |
                                                |  consumer) |
                                                +------------+
```

### Minimum Implementation Checklist

For a Python Orleans-like streaming system, the minimum required components are:

1. **StreamId** -- immutable identifier (namespace + key).
2. **StreamSequenceToken** -- comparable position marker.
3. **AsyncObserver protocol** -- on_next, on_error, on_completed callbacks.
4. **AsyncStream** -- combined produce/consume handle.
5. **StreamSubscriptionHandle** -- resume, unsubscribe.
6. **StreamProvider** -- factory and registry.
7. **PubSub** -- subscription tracking (can start with in-memory dict, then persistent grain).
8. **ImplicitStreamSubscription** -- decorator for automatic subscription.
9. **Memory stream provider** -- for development (direct async delivery).
10. **QueueAdapter abstraction** -- for persistent stream providers.
11. **PullingAgent** -- async task per queue partition for persistent streams.
12. **QueueCache** -- per-agent buffer with per-consumer cursors.
13. **StreamQueueMapper** -- consistent hash from stream to queue.
14. **Backpressure** -- slow dequeue when cache fills.

Optional but valuable:
- StreamFilterPredicate support
- Rewindable stream support (requires QueueAdapter.IsRewindable + token-based subscribe)
- Broadcast channels
- Batch operations (OnNextBatchAsync)
- Queue balancer (for multi-silo scenarios)
