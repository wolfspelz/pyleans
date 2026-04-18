# Microsoft Orleans -- Grains (Virtual Actors) Deep Dive

This document provides a thorough reference on Microsoft Orleans Grains, covering all
major concepts needed to build a Python implementation of the virtual actor model.

Sources:
- https://learn.microsoft.com/en-us/dotnet/orleans/overview
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-identity
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-lifecycle
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-references
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/request-scheduling
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-placement
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/stateless-worker-grains
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/timers-and-reminders
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/interceptors
- https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-persistence/
- https://github.com/dotnet/orleans

---

## Table of Contents

1. [What is a Grain? (Virtual Actor = Identity + Behavior + State)](#1-what-is-a-grain)
2. [Grain Identity](#2-grain-identity)
3. [Grain Interfaces and Implementation Classes](#3-grain-interfaces-and-implementation-classes)
4. [Grain Lifecycle: Activation, Deactivation, Idle Collection](#4-grain-lifecycle)
5. [Single-Threaded Execution Model (Turn-Based Concurrency)](#5-single-threaded-execution-model)
6. [Grain Activation vs Grain Reference (Proxy)](#6-grain-activation-vs-grain-reference)
7. [Reentrancy and Interleaving](#7-reentrancy-and-interleaving)
8. [Stateless Worker Grains](#8-stateless-worker-grains)
9. [Grain Timers and Reminders](#9-grain-timers-and-reminders)
10. [Grain Call Filters (Interceptors)](#10-grain-call-filters)
11. [Grain Placement Strategies](#11-grain-placement-strategies)
12. [Grain Persistence (State Storage)](#12-grain-persistence)

---

## 1. What is a Grain?

A **grain** is the fundamental building block of Orleans -- a **virtual actor**. It is composed
of three elements:

1. **Stable identity** -- a user-defined key that uniquely identifies the grain forever.
2. **Behavior** -- methods defined by the grain's interface(s).
3. **State** -- volatile in-memory data and/or persistent data stored in external systems.

### Virtual Actor Model

Orleans implements the **Virtual Actor** abstraction where actors exist *perpetually* in a
virtual sense:

- A grain **cannot be explicitly created or destroyed**. It always *exists* conceptually.
- A grain is **always addressable** -- you can send a message to any grain identity at any
  time, regardless of whether it is currently loaded in memory.
- The Orleans runtime **automatically instantiates** (activates) grains on demand when a
  message arrives for them.
- Grains that are idle for a period are **automatically removed** from memory
  (deactivated/collected), freeing resources.
- If a server fails, the grain will be **transparently re-activated** on another server when
  the next message arrives.

This is distinct from classical actor systems (like Erlang/Akka) where actors must be
explicitly created and their lifecycle is manually managed.

### Key Architecture

```
Cluster
  +-- Silo 1
  |     +-- Grain A (activated)
  |     +-- Grain B (activated)
  +-- Silo 2
  |     +-- Grain C (activated)
  +-- Silo N
        +-- ...
```

- **Silo**: A host process that runs grain activations. A cluster has one or more silos.
- **Cluster**: A group of silos that coordinate for scalability and fault tolerance.
- **Grain activation**: A live, in-memory instance of a grain on a specific silo.

---

## 2. Grain Identity

Every grain has a unique identity consisting of two parts:

1. **Grain type name** -- identifies the grain *class* (e.g., `"shoppingcart"`).
2. **Grain key** -- identifies the specific logical *instance* (e.g., `"bob65"`).

The full identity is conventionally written as `grain_type/key`, for example:
`shoppingcart/bob65`.

### Grain Type Name

By default, Orleans derives the grain type name from the class name:
- Removes the "Grain" suffix if present.
- Converts to lowercase.

Example: `ShoppingCartGrain` becomes `"shoppingcart"`.

You can override with an attribute:

```csharp
[GrainType("cart")]
public class ShoppingCartGrain : IShoppingCartGrain
{
    // ...
}
```

### Grain Key Types

Orleans supports five key types through marker interfaces:

| Interface | Key Type | Example |
|-----------|----------|---------|
| `IGrainWithGuidKey` | `System.Guid` | `Guid.NewGuid()` |
| `IGrainWithIntegerKey` | `System.Int64` (long) | `1`, `42` |
| `IGrainWithStringKey` | `System.String` | `"my-grain-key"` |
| `IGrainWithGuidCompoundKey` | `Guid` + `String` | `(guid, "extension")` |
| `IGrainWithIntegerCompoundKey` | `Int64` + `String` | `(42, "extension")` |

All key types are ultimately encoded as strings internally.

#### GUID Keys

```csharp
// Create reference
var grain = grainFactory.GetGrain<IExample>(Guid.NewGuid());

// Retrieve key inside grain
public override Task OnActivateAsync()
{
    Guid primaryKey = this.GetPrimaryKey();
    return base.OnActivateAsync();
}
```

#### Integer Keys

```csharp
var grain = grainFactory.GetGrain<IExample>(1);

// Inside grain
long primaryKey = this.GetPrimaryKeyLong();
```

#### String Keys

```csharp
var grain = grainFactory.GetGrain<IExample>("myGrainKey");

// Inside grain
string primaryKey = this.GetPrimaryKeyString();
```

#### Compound Keys

```csharp
public interface IExampleGrain : IGrainWithIntegerCompoundKey
{
    Task Hello();
}

// Create reference with compound key
var grain = grainFactory.GetGrain<IExample>(0, "a string!", null);

// Retrieve inside grain
public Task Hello()
{
    long primaryKey = this.GetPrimaryKeyLong(out string keyExtension);
    Console.WriteLine($"Hello from {keyExtension}");
    return Task.CompletedTask;
}
```

### Singleton Grains

For singleton grains, use a well-known fixed value like `"default"` or `0` as the key.

### Why Logical Identifiers?

In distributed systems, object references (memory addresses) are limited to a single
process. Orleans uses logical identifiers so grain references:
- Remain valid across process lifetimes.
- Are portable across network boundaries.
- Can be stored persistently and later retrieved.
- Survive complete system restarts.

---

## 3. Grain Interfaces and Implementation Classes

### Grain Interface

A grain interface defines the contract. All methods **must** return `Task`, `Task<T>`,
or `ValueTask<T>`:

```csharp
public interface IPlayerGrain : IGrainWithGuidKey
{
    Task<IGameGrain> GetCurrentGame(CancellationToken cancellationToken = default);
    Task JoinGame(IGameGrain game, CancellationToken cancellationToken = default);
    Task LeaveGame(IGameGrain game, CancellationToken cancellationToken = default);
}
```

**Key design rules:**
- Every method is async (returns Task/ValueTask).
- Interfaces inherit from one of the `IGrainWith*Key` marker interfaces.
- Methods can accept and return grain references (other grain interfaces).
- CancellationToken support is available.

### Grain Implementation Class

A grain class implements one or more grain interfaces and extends the `Grain` base class:

```csharp
public class PlayerGrain : Grain, IPlayerGrain
{
    private IGameGrain _currentGame;

    public Task<IGameGrain> GetCurrentGame(CancellationToken cancellationToken = default)
    {
        return Task.FromResult(_currentGame);
    }

    public Task JoinGame(IGameGrain game, CancellationToken cancellationToken = default)
    {
        _currentGame = game;
        Console.WriteLine($"Player {GetPrimaryKey()} joined game {game.GetPrimaryKey()}");
        return Task.CompletedTask;
    }

    public Task LeaveGame(IGameGrain game, CancellationToken cancellationToken = default)
    {
        _currentGame = null;
        Console.WriteLine($"Player {GetPrimaryKey()} left game {game.GetPrimaryKey()}");
        return Task.CompletedTask;
    }
}
```

### Return Value Patterns

```csharp
// Returning a value (non-async)
public Task<SomeType> GrainMethod1()
{
    return Task.FromResult(GetSomeType());
}

// Void method (non-async)
public Task GrainMethod2()
{
    return Task.CompletedTask;
}

// Async method with value
public async Task<SomeType> GrainMethod3()
{
    return await GetSomeTypeAsync();
}

// Async void method
public async Task GrainMethod4()
{
    await DoSomethingAsync();
}
```

### Error Propagation

- Exceptions thrown from grain methods are propagated across hosts back to the caller.
- Exceptions must be serializable.
- Exceptions do NOT cause grain deactivation (unlike some actor systems), UNLESS the
  exception is `InconsistentStateException` (from storage operations).

### Response Timeout

Grain method calls have a default 30-second timeout. Per-method timeouts can be set:

```csharp
public interface IPlayerGrain : IGrainWithGuidKey
{
    [ResponseTimeout("00:00:05")] // 5 second timeout
    Task LeaveGame(IGameGrain game, CancellationToken cancellationToken = default);
}
```

---

## 4. Grain Lifecycle

### Lifecycle Stages

Orleans grains have an observable lifecycle with predefined stages:

```csharp
public static class GrainLifecycleStage
{
    public const int First = int.MinValue;
    public const int SetupState = 1_000;    // Load state from storage
    public const int Activate = 2_000;       // Call OnActivateAsync / OnDeactivateAsync
    public const int Last = int.MaxValue;
}
```

### Activation

When a message arrives for a grain that is not currently active in the cluster:

1. The runtime selects a silo using the **placement strategy**.
2. The silo creates an instance of the grain class (via DI container).
3. **SetupState** stage: persistent state is loaded from storage.
4. **Activate** stage: `OnActivateAsync()` is called.
5. The grain is now ready to process requests.

```csharp
public class MyGrain : Grain, IMyGrain
{
    public override async Task OnActivateAsync(CancellationToken cancellationToken)
    {
        // Initialization logic -- load data, set up timers, etc.
        await base.OnActivateAsync(cancellationToken);
    }
}
```

**If `OnActivateAsync` throws an exception, the activation fails** and the request that
triggered it is faulted back to the caller.

### Deactivation

Deactivation occurs when:
- The grain has been **idle** for a configurable period (default: 15 minutes).
- The silo is under **memory pressure** (if activation shedding is enabled).
- The grain explicitly calls `DeactivateOnIdle()`.
- The silo is shutting down gracefully.

```csharp
public override async Task OnDeactivateAsync(
    DeactivationReason reason, CancellationToken cancellationToken)
{
    // Cleanup logic -- best effort only
    await base.OnDeactivateAsync(reason, cancellationToken);
}
```

**IMPORTANT:** `OnDeactivateAsync` is NOT guaranteed to be called (e.g., server crash).
Do NOT rely on it for critical operations like persisting state.

### Idle Collection Configuration

```csharp
builder.Configure<GrainCollectionOptions>(options =>
{
    // Default idle timeout before collection
    options.CollectionAge = TimeSpan.FromMinutes(15);

    // How often idle collection runs
    options.CollectionQuantum = TimeSpan.FromMinutes(1);
});
```

### Memory-Based Activation Shedding (Orleans 9+)

Automatically deactivates grains under memory pressure:

```csharp
builder.Configure<GrainCollectionOptions>(options =>
{
    options.EnableActivationSheddingOnMemoryPressure = true;
    options.MemoryUsageLimitPercentage = 80;   // Start shedding at 80%
    options.MemoryUsageTargetPercentage = 75;   // Target after shedding
    options.MemoryUsagePollingPeriod = TimeSpan.FromSeconds(5);
});
```

Older/less-recently-used grains are deactivated first.

### Lifecycle Participation

Components can participate in the grain lifecycle via DI:

```csharp
public override void Participate(IGrainLifecycle lifecycle)
{
    base.Participate(lifecycle);
    lifecycle.Subscribe(
        this.GetType().FullName,
        GrainLifecycleStage.SetupState,
        OnSetupState);
}
```

Injected components can also participate without special grain logic:

```csharp
public class MyComponent : ILifecycleParticipant<IGrainLifecycle>
{
    public static MyComponent Create(IGrainContext context)
    {
        var component = new MyComponent();
        component.Participate(context.ObservableLifecycle);
        return component;
    }

    public void Participate(IGrainLifecycle lifecycle)
    {
        lifecycle.Subscribe<MyComponent>(GrainLifecycleStage.Activate, OnActivate);
    }

    private Task OnActivate(CancellationToken ct) { /* ... */ }
}
```

### Grain Migration (Orleans 8+)

Grain activations can move between silos while preserving in-memory state:

1. **Dehydration**: serialize state on source silo.
2. **Transfer**: send to target silo.
3. **Rehydration**: restore state before `OnActivateAsync`.

```csharp
public class MyGrain : Grain, IMyGrain, IGrainMigrationParticipant
{
    private int _cachedValue;

    public void OnDehydrate(IDehydrationContext ctx)
    {
        ctx.TryAddValue("cached", _cachedValue);
    }

    public void OnRehydrate(IRehydrationContext ctx)
    {
        ctx.TryGetValue("cached", out _cachedValue);
    }
}
```

Trigger migration: `this.MigrateOnIdle();`

Prevent migration: `[Immovable]` attribute.

---

## 5. Single-Threaded Execution Model

### Turn-Based Concurrency

Orleans grains use a **single-threaded, turn-based** execution model:

- Each grain activation processes **one request at a time** from start to finish.
- While a grain awaits an async operation, it does NOT process other requests (by default).
- Execution is always on a **single thread** -- no concurrent access to grain state.
- This eliminates the need for locks, mutexes, or other synchronization primitives.

This is the core safety guarantee: **grain state is never accessed concurrently**.

### What is a "Turn"?

A turn is a contiguous block of synchronous execution within a grain method. When a grain
method hits an `await`, the current turn ends. The next turn begins when the awaited
operation completes.

```
Request 1:  [Turn 1a]---await---[Turn 1b]---complete
Request 2:                                    [Turn 2a]---await---[Turn 2b]
```

By default, Request 2 cannot start until Request 1 is fully complete. With reentrancy
enabled, turns from different requests can interleave (but never run in parallel).

### Deadlock Potential

Because grains process one request at a time, circular call chains can deadlock:

```
Grain A calls Grain B
Grain B calls Grain A  (A is busy waiting for B, so this request queues)
-> DEADLOCK (both waiting for each other)
```

Solutions:
- Avoid circular call patterns.
- Use reentrancy (see Section 7).
- Use `AllowCallChainReentrancy()` for specific call sites.

---

## 6. Grain Activation vs Grain Reference

### Grain Reference (Proxy)

A **grain reference** is a lightweight proxy object that:
- Implements the same grain interface as the grain class.
- Encapsulates the grain's **logical identity** (type + key).
- Routes method calls to the correct grain activation, wherever it lives.
- Is independent of the grain's physical location.
- Remains valid across system restarts, silo failures, etc.
- Can be passed as a method argument, returned from methods, stored in persistent state.

```csharp
// From inside a grain
IPlayerGrain player = GrainFactory.GetGrain<IPlayerGrain>(playerId);

// From client code
IPlayerGrain player = client.GetGrain<IPlayerGrain>(playerId);
```

A grain reference contains three pieces of information:
1. The **grain type** (identifies the grain class).
2. The **grain key** (identifies the instance).
3. The **interface type** (which methods are available on this proxy).

**Creating a grain reference does NOT activate the grain.** The grain is only activated
when a method is actually invoked on the reference.

### Grain Activation (Live Instance)

A **grain activation** is a live, in-memory instance:
- Created by the runtime on a specific silo.
- Has actual state loaded.
- Processes requests.
- Only ONE activation per grain identity exists in the cluster at any time
  (single-activation guarantee, enforced by the grain directory).
- Exception: stateless worker grains can have multiple activations.

### Key Differences

| Aspect | Grain Reference | Grain Activation |
|--------|----------------|-----------------|
| What | Proxy/stub object | Live in-memory instance |
| Where | Anywhere (client, any silo) | Specific silo |
| Lifetime | Forever (logical) | Temporary (until deactivation) |
| Uniqueness | Many references to same grain | One activation per grain* |
| State | None | Holds grain state |
| Purpose | Route calls | Process calls |

*Except stateless workers.

### Disambiguation

When multiple classes implement the same interface, disambiguation is needed:

```csharp
// Using unique marker interfaces (preferred)
ICounterGrain up = grainFactory.GetGrain<IUpCounterGrain>("my-counter");

// Using grain class name prefix
ICounterGrain up = grainFactory.GetGrain<ICounterGrain>("key", grainClassNamePrefix: "Up");

// Using explicit GrainId
ICounterGrain up = grainFactory.GetGrain<ICounterGrain>(
    GrainId.Create("up-counter", "my-counter"));
```

---

## 7. Reentrancy and Interleaving

By default, grains are **non-reentrant**: they process one request completely before
starting the next. This is safe but can cause deadlocks and reduce throughput.

Orleans provides several mechanisms to allow controlled interleaving:

### 7.1 Reentrant Grains (`[Reentrant]`)

All methods in the grain can interleave:

```csharp
[Reentrant]
public class MyGrain : Grain, IMyGrain
{
    // All requests can interleave at await points.
    // Execution is still single-threaded (one turn at a time),
    // but turns from different requests can alternate.
}
```

**Interleaving example** (with `[Reentrant]`):

```
Foo():  line 1 (await) --------> line 2 (done)
Bar():              line 3 (await) --------> line 4 (done)

Possible execution: line 1, line 3, line 2, line 4
```

Without reentrancy: `line 1, line 2, line 3, line 4` (strictly sequential).

### 7.2 Always-Interleave Methods (`[AlwaysInterleave]`)

Mark specific interface methods that should always interleave:

```csharp
public interface ISlowpokeGrain : IGrainWithIntegerKey
{
    Task GoSlow();

    [AlwaysInterleave]
    Task GoFast();
}
```

- `GoFast()` calls will interleave with any other request.
- `GoSlow()` calls are still processed sequentially with respect to each other.

### 7.3 Read-Only Methods (`[ReadOnly]`)

Methods that don't modify state can run concurrently with other read-only methods:

```csharp
public interface IMyGrain : IGrainWithIntegerKey
{
    Task<int> IncrementCount(int incrementBy);

    [ReadOnly]
    Task<int> GetCount();
}
```

Multiple `GetCount()` calls can execute concurrently. `IncrementCount()` still requires
exclusive access.

### 7.4 Call Chain Reentrancy

Enable reentrancy for a specific call chain to prevent deadlocks:

```csharp
public async ValueTask JoinRoom(string roomName)
{
    // Allow the room grain to call back into this grain
    using var scope = RequestContext.AllowCallChainReentrancy();
    var roomGrain = GrainFactory.GetGrain<IChatRoomGrain>(roomName);
    await roomGrain.OnJoinRoom(this.AsReference<IUserGrain>());
}
```

This allows ONLY the called grain to call back, and only until the scope is disposed.
More targeted than `[Reentrant]` or `[AlwaysInterleave]`.

### 7.5 Predicate-Based Interleaving (`[MayInterleave]`)

Decide interleaving on a per-call basis using a predicate:

```csharp
[MayInterleave(nameof(ArgHasInterleaveAttribute))]
public class MyGrain : Grain, IMyGrain
{
    public static bool ArgHasInterleaveAttribute(IInvokable req)
    {
        return req.Arguments.Length == 1
            && req.Arguments[0]?.GetType()
                    .GetCustomAttribute<InterleaveAttribute>() != null;
    }

    public Task Process(object payload) { /* ... */ }
}
```

### Summary Table

| Mechanism | Scope | Description |
|-----------|-------|-------------|
| `[Reentrant]` | Grain class | All methods can interleave |
| `[AlwaysInterleave]` | Interface method | This method always interleaves |
| `[ReadOnly]` | Interface method | Concurrent with other ReadOnly methods |
| `[MayInterleave]` | Grain class | Predicate decides per-call |
| `AllowCallChainReentrancy()` | Call site | Reentrancy for downstream callers only |

---

## 8. Stateless Worker Grains

Stateless workers are a special grain type for functional, stateless operations that need
to scale out easily.

```csharp
[StatelessWorker]
public class MyStatelessWorkerGrain : Grain, IMyStatelessWorkerGrain
{
    // ...
}
```

### Properties

1. **Multiple activations**: The runtime creates multiple activations across different
   silos (unlike normal grains which have exactly one activation).
2. **Local execution**: Requests are processed locally on the receiving silo (no network hop).
3. **Auto-scaling**: The runtime creates additional activations when existing ones are busy,
   up to a configurable maximum (default = number of CPU cores).
4. **Not individually addressable**: Two requests to the same stateless worker may hit
   different activations.
5. **Not registered in grain directory** (no need since they're not uniquely routed).

### Usage

```csharp
// Typically use a fixed key (e.g., 0)
var worker = GrainFactory.GetGrain<IMyStatelessWorkerGrain>(0);
await worker.Process(args);
```

### Limiting activations per silo

```csharp
[StatelessWorker(1)] // max 1 activation per silo
public class MyLonelyWorkerGrain : ILonelyWorkerGrain
{
    // ...
}
```

### Use Cases

- **Decompression/routing**: Stateless preprocessing before forwarding to target grains.
- **Hot cache**: Scale out frequently-accessed cached data across silos.
- **Reduce-style aggregation**: First-level local aggregation before forwarding to a
  global aggregator, avoiding overload.

### Reentrancy Note

Stateless workers are **non-reentrant** by default (same as regular grains). Add
`[Reentrant]` if needed.

---

## 9. Grain Timers and Reminders

Orleans provides two periodic scheduling mechanisms:

### 9.1 Timers

Timers are **in-memory, non-durable** periodic callbacks tied to a grain activation.

```csharp
public class MyGrain : Grain, IMyGrain
{
    private IGrainTimer? _timer;

    public override Task OnActivateAsync(CancellationToken cancellationToken)
    {
        _timer = this.RegisterGrainTimer(
            static (state, ct) => state.DoWorkAsync(ct),
            this,
            new GrainTimerCreationOptions
            {
                DueTime = TimeSpan.FromSeconds(5),   // First tick after 5s
                Period = TimeSpan.FromSeconds(10),    // Then every 10s
                KeepAlive = true                      // Prevent idle collection
            });

        return Task.CompletedTask;
    }

    private Task DoWorkAsync(CancellationToken cancellationToken)
    {
        return Task.CompletedTask;
    }
}
```

**Timer properties:**
- Tied to a single activation -- destroyed when grain deactivates or silo crashes.
- Execute within the grain's single-threaded context.
- The period is measured from when the callback Task *completes* to the next invocation
  (not from start to start). Callbacks never overlap.
- By default, timer callbacks do NOT prevent idle collection. Set `KeepAlive = true` to
  keep the grain alive.
- By default, timer callbacks do NOT interleave with other grain calls. Set
  `Interleave = true` to allow interleaving.
- Timer callbacks are subject to grain call filters and visible in distributed tracing.
- Timers can be updated via `_timer.Change(newDueTime, newPeriod)`.
- Cancel by disposing: `_timer.Dispose()`.

### 9.2 Reminders

Reminders are **persistent, durable** periodic triggers associated with a grain identity
(not a specific activation).

```csharp
public class MyGrain : Grain, IMyGrain, IRemindable
{
    public async Task StartReminder()
    {
        IGrainReminder reminder = await RegisterOrUpdateReminder(
            reminderName: "check-inventory",
            dueTime: TimeSpan.FromMinutes(1),
            period: TimeSpan.FromHours(1));
    }

    public Task ReceiveReminder(string reminderName, TickStatus status)
    {
        Console.WriteLine($"Reminder {reminderName} fired!");
        return Task.CompletedTask;
    }

    public async Task StopReminder()
    {
        IGrainReminder reminder = await GetReminder("check-inventory");
        await UnregisterReminder(reminder);
    }
}
```

**Reminder properties:**
- Survive grain deactivation, silo crashes, and cluster restarts.
- Stored in persistent storage (Azure Table, SQL, Redis, Cosmos DB, or in-memory for dev).
- If a grain is not activated when a reminder fires, the grain is reactivated.
- Not suitable for high-frequency timers -- periods should be minutes, hours, or days.
- Reminder delivery is via message, subject to same interleaving rules as grain calls.
- If a reminder tick is missed (cluster down), it is skipped -- only the next tick fires.
- The grain must implement `IRemindable` to receive reminder callbacks.
- Reminders must be explicitly canceled (they survive indefinitely otherwise).

### Timers vs Reminders

| Feature | Timers | Reminders |
|---------|--------|-----------|
| Durability | None (in-memory) | Persistent (survives restarts) |
| Frequency | High (seconds) | Low (minutes/hours/days) |
| Tied to | Specific activation | Grain identity |
| Setup | `RegisterGrainTimer()` | `RegisterOrUpdateReminder()` |
| Cleanup | Dispose | `UnregisterReminder()` |
| Reactivates grain? | No | Yes |
| Storage required? | No | Yes |

### Combined Pattern

Use a reminder to periodically wake up a grain, which then starts a local timer for
fine-grained work:

```
Reminder (every 5 min) --> Activates grain --> Grain starts timer (every 10s)
                          If grain deactivates, timer is lost
                          Next reminder tick reactivates and restarts timer
```

---

## 10. Grain Call Filters

Grain call filters (interceptors) provide cross-cutting concerns for grain method calls.
They form a pipeline around grain method invocations.

### 10.1 Incoming Call Filters

Execute on the **callee** (receiving grain) side:

```csharp
public interface IIncomingGrainCallFilter
{
    Task Invoke(IIncomingGrainCallContext context);
}
```

The context provides:
- `Grain` -- the grain being invoked.
- `InterfaceMethod` -- MethodInfo of the interface method.
- `ImplementationMethod` -- MethodInfo of the grain class method.
- `Arguments` -- method arguments.
- `Result` -- get/set the return value.
- `Invoke()` -- call the next filter or the grain method.

#### Silo-wide filter (registered via DI)

```csharp
public class LoggingCallFilter : IIncomingGrainCallFilter
{
    private readonly ILogger<LoggingCallFilter> _logger;

    public LoggingCallFilter(ILogger<LoggingCallFilter> logger) => _logger = logger;

    public async Task Invoke(IIncomingGrainCallContext context)
    {
        try
        {
            await context.Invoke();
            _logger.LogInformation(
                "{GrainType}.{MethodName} returned {Result}",
                context.Grain.GetType(), context.MethodName, context.Result);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "{GrainType}.{MethodName} threw an exception",
                context.Grain.GetType(), context.MethodName);
            throw;
        }
    }
}

// Registration
siloHostBuilder.AddIncomingGrainCallFilter<LoggingCallFilter>();
```

#### Per-grain filter (grain implements the interface)

```csharp
public class MyFilteredGrain : Grain, IMyGrain, IIncomingGrainCallFilter
{
    public async Task Invoke(IIncomingGrainCallContext context)
    {
        await context.Invoke();

        if (string.Equals(context.InterfaceMethod.Name, nameof(GetFavoriteNumber)))
        {
            context.Result = 38; // Override the result
        }
    }

    public Task<int> GetFavoriteNumber() => Task.FromResult(7);
}
```

### 10.2 Outgoing Call Filters

Execute on the **caller** side (client or grain making the call):

```csharp
public interface IOutgoingGrainCallFilter
{
    Task Invoke(IOutgoingGrainCallContext context);
}

// Registration (on silo or client)
builder.AddOutgoingGrainCallFilter<OutgoingLoggingCallFilter>();
```

### Filter Execution Order

1. Silo-wide `IIncomingGrainCallFilter` implementations (in DI registration order).
2. Per-grain filter (if grain implements `IIncomingGrainCallFilter`).
3. Actual grain method.

Each filter must call `context.Invoke()` to pass control to the next filter.

### Use Cases

- **Authorization**: Check RequestContext or arguments before allowing a call.
- **Logging/Telemetry**: Log timing, arguments, results.
- **Error handling**: Transform or wrap exceptions.
- **Result modification**: Modify return values.

---

## 11. Grain Placement Strategies

When a grain needs to be activated, Orleans must decide **which silo** to place it on.
This is called **placement**.

### Built-in Strategies

| Strategy | Attribute | Description |
|----------|-----------|-------------|
| **Resource-Optimized** (default in 9.2+) | `[ResourceOptimizedPlacement]` | Balances based on CPU, memory, and activation count |
| **Random** | `[RandomPlacement]` | Random compatible server |
| **Prefer Local** | `[PreferLocalPlacement]` | Local silo if compatible, else random |
| **Hash-Based** | `[HashBasedPlacement]` | Hash grain ID to select server (not stable across membership changes) |
| **Activation-Count-Based** | `[ActivationCountBasedPlacement]` | Least loaded server by activation count (Power of Two Choices algorithm) |
| **Stateless Worker** | `[StatelessWorker]` | Prefer local, multiple activations allowed |
| **Silo-Role-Based** | `[SiloRoleBasedPlacement]` | Place on silos with specific role |

### Resource-Optimized Placement (Default)

```csharp
siloBuilder.Configure<ResourceOptimizedPlacementOptions>(options =>
{
    options.CpuUsageWeight = 40;
    options.MemoryUsageWeight = 20;
    options.AvailableMemoryWeight = 20;
    options.MaxAvailableMemoryWeight = 5;
    options.ActivationCountWeight = 15;
    options.LocalSiloPreferenceMargin = 5;
});
```

Uses adaptive smoothing algorithm to avoid saturating newly joined silos.

### Custom Placement Strategy

Implement `IPlacementDirector`:

```csharp
public class MyPlacementDirector : IPlacementDirector
{
    public Task<SiloAddress> OnAddActivation(
        PlacementStrategy strategy,
        PlacementTarget target,
        IPlacementContext context)
    {
        var silos = context.GetCompatibleSilos(target).OrderBy(s => s).ToArray();
        int silo = GetSiloNumber(target.GrainIdentity.PrimaryKey, silos.Length);
        return Task.FromResult(silos[silo]);
    }
}

// Define strategy and attribute
[Serializable]
public sealed class MyPlacementStrategy : PlacementStrategy { }

[AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
public sealed class MyPlacementAttribute : PlacementAttribute
{
    public MyPlacementAttribute() : base(new MyPlacementStrategy()) { }
}

// Apply to grain
[MyPlacement]
public class MyGrain : Grain, IMyGrain { }

// Register
services.AddPlacementDirector<MyPlacementStrategy, MyPlacementDirector>();
```

### Changing the Default Strategy

```csharp
// Revert to random (pre-9.2 default)
siloBuilder.Services.AddSingleton<PlacementStrategy, RandomPlacement>();

// Or use custom
siloBuilder.Services.AddSingleton<PlacementStrategy, MyPlacementStrategy>();
```

### Activation Repartitioning (Experimental)

Automatically migrates grains to be closer to the grains they communicate with most:

```csharp
siloBuilder.AddActivationRepartitioner();
siloBuilder.Configure<ActivationRepartitionerOptions>(options =>
{
    options.MaxEdgeCount = 10_000;
    options.MinRoundPeriod = TimeSpan.FromMinutes(1);
    options.MaxRoundPeriod = TimeSpan.FromMinutes(2);
    options.RecoveryPeriod = TimeSpan.FromMinutes(1);
});
```

---

## 12. Grain Persistence

Orleans provides a declarative persistence model for grain state.

### IPersistentState<T> (Recommended)

Inject persistent state into the grain via constructor:

```csharp
[Serializable]
public class ProfileState
{
    public string Name { get; set; }
    public DateTime DateOfBirth { get; set; }
}

public class UserGrain : Grain, IUserGrain
{
    private readonly IPersistentState<ProfileState> _profile;
    private readonly IPersistentState<CartState> _cart;

    public UserGrain(
        [PersistentState("profile", "profileStore")] IPersistentState<ProfileState> profile,
        [PersistentState("cart", "cartStore")] IPersistentState<CartState> cart)
    {
        _profile = profile;
        _cart = cart;
    }

    public Task<string> GetNameAsync() => Task.FromResult(_profile.State.Name);

    public async Task SetNameAsync(string name)
    {
        _profile.State.Name = name;
        await _profile.WriteStateAsync();
    }
}
```

### API

```csharp
public interface IStorage<TState>
{
    TState State { get; set; }   // The state object
    string Etag { get; }         // Optimistic concurrency tag
    bool RecordExists { get; }   // Whether state exists in storage

    Task ClearStateAsync();      // Delete from storage
    Task WriteStateAsync();      // Persist to storage
    Task ReadStateAsync();       // Reload from storage
}
```

### Key behaviors:
- State is **automatically loaded** during grain activation (SetupState stage), before
  `OnActivateAsync()` is called.
- State is **NOT automatically saved**. The grain must explicitly call `WriteStateAsync()`.
- Multiple named state objects per grain are supported.
- Different state objects can use different storage providers.
- ETags provide optimistic concurrency -- `InconsistentStateException` on conflicts.

### Legacy: Grain<TState>

```csharp
[StorageProvider(ProviderName = "store1")]
public class MyGrain : Grain<MyGrainState>, IMyGrain
{
    public async Task UpdateName(string name)
    {
        State.Name = name;
        await WriteStateAsync();
    }
}
```

### Storage Providers

Official packages:
- **Azure Table Storage** / **Azure Blob Storage** (`Microsoft.Orleans.Persistence.AzureStorage`)
- **Azure Cosmos DB** (`Microsoft.Orleans.Persistence.Cosmos`)
- **ADO.NET / SQL** (`Microsoft.Orleans.Persistence.AdoNet`)
- **Amazon DynamoDB** (`Microsoft.Orleans.Persistence.DynamoDB`)
- **Redis** (`Microsoft.Orleans.Persistence.Redis`)

Configuration example:

```csharp
siloBuilder
    .AddAzureTableGrainStorage(
        name: "profileStore",
        configureOptions: options =>
        {
            options.TableServiceClient = new TableServiceClient(endpoint, credential);
        })
    .AddRedisGrainStorage(
        name: "cartStore",
        configureOptions: options =>
        {
            options.ConfigurationOptions = new ConfigurationOptions
            {
                EndPoints = { "localhost:6379" }
            };
        });
```

### Custom Storage Provider

Implement `IGrainStorage`:

```csharp
public interface IGrainStorage
{
    Task ReadStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
    Task WriteStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
    Task ClearStateAsync<T>(string stateName, GrainId grainId, IGrainState<T> grainState);
}
```

---

## Summary: Key Concepts for Python Implementation

### Core Abstractions to Implement

1. **GrainIdentity** = (grain_type: str, grain_key: str)
2. **GrainReference** = proxy object with identity + interface that routes calls
3. **GrainActivation** = live instance with state, on a specific silo
4. **GrainInterface** = abstract protocol defining the grain's methods (all async)
5. **GrainBase** = base class providing `on_activate`, `on_deactivate`, `grain_factory`, etc.

### Core Runtime Components

1. **Grain Directory** = maps grain identity to silo address (single-activation guarantee)
2. **Placement Strategy** = decides which silo to activate a grain on
3. **Grain Lifecycle** = ordered stages: SetupState -> Activate -> Ready -> Deactivate
4. **Request Scheduler** = single-threaded, turn-based execution per grain
5. **Timer Service** = in-memory periodic callbacks
6. **Reminder Service** = persistent periodic triggers
7. **Call Filters** = interceptor pipeline for cross-cutting concerns
8. **State Persistence** = named state objects with explicit Read/Write/Clear

### Key Design Principles

- **Virtual existence**: grains always conceptually exist. No create/destroy API.
- **Location transparency**: callers use grain references, unaware of physical location.
- **Single-threaded safety**: no locks needed; one request at a time (with opt-in reentrancy).
- **Automatic lifecycle management**: runtime handles activation/deactivation.
- **Explicit persistence**: state is auto-loaded but must be explicitly saved.
- **Turn-based concurrency**: async/await is the natural fit (Python's asyncio maps well).
