# Orleans Advanced Features: Transactions, Concurrency, and More

This document covers the advanced features of Microsoft Orleans that are essential
for building a Python implementation. It covers the concurrency model, transactions,
observers, grain services, placement strategies, and other advanced mechanisms.

---

## 1. Turn-Based Concurrency Model

### Single-Threaded Execution Per Grain

Orleans grain activations have a **single-threaded execution model**. By default,
each grain processes one request from beginning to completion before the next request
can begin processing. This is the fundamental concurrency guarantee of the Virtual
Actor model.

Key properties:
- Each grain activation has its own **TaskScheduler** (in .NET terms).
- Sub-tasks spawned via `await`, `ContinueWith`, or `Task.Factory.StartNew` dispatch
  on the same per-activation scheduler and inherit the single-threaded model.
- Execution is limited to a single thread at a time -- the activation executes one
  "turn" at a time.
- While a grain awaits an asynchronous operation (e.g., a call to another grain),
  it **cannot process any other incoming requests** (unless reentrancy is enabled).

### Turn-Based Scheduling

A "turn" is a synchronous segment of code between two consecutive `await` points.
The scheduler ensures:
1. Only one turn executes at a time per grain activation.
2. Turns from the same request execute in order.
3. No concurrent access to grain state within a single activation (unless reentrant).

### Deadlock Scenario

If Grain A calls Grain B, and simultaneously Grain B calls Grain A, a deadlock can
occur because neither grain can process the incoming request while waiting for its
outgoing call to complete. This is the primary motivation for reentrancy options.

### Synchronous Blocking is Forbidden

The following methods must NEVER be called in grain code as they can deadlock:
- `Task.Wait()`
- `Task.Result`
- `Task.WaitAny(...)` / `Task.WaitAll(...)`
- `task.GetAwaiter().GetResult()`

If synchronous blocking work is unavoidable, it should be offloaded to a thread pool
thread via `await Task.Run(() => blockingWork())`.

### External Tasks and Task Schedulers

- `await`, `Task.Factory.StartNew`, `Task.ContinueWith`, `Task.WhenAny`,
  `Task.WhenAll`, `Task.Delay` all respect the current task scheduler (the grain's
  scheduler).
- `Task.Run` and the `endMethod` of `Task.Factory.FromAsync` do NOT respect the
  current scheduler -- they run on the .NET thread pool (`TaskScheduler.Default`).
- Code after `await Task.Run(...)` returns to the grain's scheduler.
- `ConfigureAwait(false)` should NEVER be used in grain code (it escapes the grain
  scheduler). It is acceptable in library code.

### Python Implementation Notes

In Python, the equivalent of this model is an **asyncio event loop per grain** (or
a shared event loop with per-grain message queues). Each grain should have an inbox
queue, and a coroutine loop that processes messages one at a time. The "turn" model
maps to Python's cooperative multitasking with `await`.

---

## 2. Reentrancy

Reentrancy allows multiple requests to interleave their execution within a single
grain activation. This breaks the default single-request-at-a-time guarantee but
prevents deadlocks and improves throughput.

**Even with reentrancy, execution is still single-threaded.** Multiple requests can
interleave at `await` points, but they never run in parallel.

### Reentrancy Options

| Option | Scope | Description |
|--------|-------|-------------|
| `[Reentrant]` | Grain class | All methods can freely interleave with each other. |
| `[AlwaysInterleave]` | Interface method | The marked method always interleaves with any other request. |
| `[ReadOnly]` | Interface method | Can run concurrently with other `[ReadOnly]` methods only. |
| `[MayInterleave(predicate)]` | Grain class | A static predicate method determines per-call interleaving. |
| `AllowCallChainReentrancy()` | Call site | Allows reentrancy only for callers further down the call chain (scoped). |

### [Reentrant] Attribute

```csharp
[Reentrant]
public class MyGrain : Grain, IMyGrain
{
    // All methods in this grain can interleave.
    // Execution of Foo and Bar may interleave at await points:
    // Line 1, Line 3, Line 2, Line 4 is a valid execution order.
}
```

When a grain is marked `[Reentrant]`:
- Turns from different requests can interleave.
- Execution is still single-threaded (one turn at a time).
- The grain code must be written to handle potential interleaving of state modifications.

### [AlwaysInterleave] Attribute

Applied to an interface method. The marked method:
- Always interleaves with any other request (including non-interleaving methods).
- Any other request can interleave with it.

```csharp
public interface ISlowpokeGrain : IGrainWithIntegerKey
{
    Task GoSlow();

    [AlwaysInterleave]
    Task GoFast();
}
```

Two concurrent `GoSlow()` calls execute sequentially (total ~20s for two 10s calls).
Three concurrent `GoFast()` calls interleave (total ~10s for three 10s calls).

### [ReadOnly] Attribute

Indicates a method does not modify grain state. ReadOnly methods can execute
concurrently with other ReadOnly methods but not with write methods.

```csharp
public interface IMyGrain : IGrainWithIntegerKey
{
    Task<int> IncrementCount(int incrementBy);  // Write method

    [ReadOnly]
    Task<int> GetCount();  // Read-only, interleaves with other ReadOnly calls
}
```

### [MayInterleave] Predicate-Based Reentrancy

Allows per-call interleaving decisions based on the incoming request:

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
}
```

### Call Chain Reentrancy

Prevents deadlocks in specific call chains without making the entire grain reentrant:

```csharp
public async ValueTask JoinRoom(string roomName)
{
    // This prevents the callback from deadlocking
    using var scope = RequestContext.AllowCallChainReentrancy();
    var roomGrain = GrainFactory.GetGrain<IChatRoomGrain>(roomName);
    await roomGrain.OnJoinRoom(this.AsReference<IUserGrain>());
}
```

- Only the specific callee grain (and grains further down the chain) can call back
  into this grain.
- Reentrancy is scoped to the `using` block lifetime.
- `SuppressCallChainReentrancy()` can disable it for sub-calls.

### Python Implementation Notes

For Python:
- The default inbox processor dequeues one message, processes it to completion, then
  dequeues the next.
- `[Reentrant]` means: when an `await` yields, allow the next message in the inbox
  to start processing.
- `[ReadOnly]` means: track a read/write lock; multiple readers can proceed, but a
  writer waits for exclusive access.
- `[AlwaysInterleave]` means: the method can always start even if another method is
  in progress.
- Call chain reentrancy: tag messages with a call chain ID, and allow interleaving
  only for messages from that chain.

---

## 3. ACID Transactions Across Grains

Orleans supports distributed ACID transactions against persistent grain state using
the `Microsoft.Orleans.Transactions` package.

### Transaction Setup

Both silo and client must opt in:

```csharp
// Silo
siloBuilder.UseTransactions();

// Client
clientBuilder.UseTransactions();
```

Without this configuration, transactional calls throw `OrleansTransactionsDisabledException`.

### Transactional State Storage

Transactions use `ITransactionalStateStorage<TState>`, a storage abstraction specific
to transactions (different from `IGrainStorage`). Example:

```csharp
silo.AddAzureTableTransactionalStateStorage(
    "TransactionStore",
    options => options.TableServiceClient = new TableServiceClient(connectionString));
```

A bridge to `IGrainStorage` exists for development but is less efficient and may be
removed in the future.

### Transaction Options (TransactionAttribute)

Grain interface methods are marked with `[Transaction(TransactionOption)]`:

| TransactionOption | Behavior |
|-------------------|----------|
| `Create` | Always starts a new transaction context. |
| `Join` | Must be called within an existing transaction; fails otherwise. |
| `CreateOrJoin` | Joins existing transaction or creates a new one. |
| `Suppress` | Not transactional; if called in a transaction, context is NOT passed. |
| `Supported` | Not transactional but passes transaction context if present. |
| `NotAllowed` | Not transactional; throws if called within a transaction. |

### Example: ATM Transfer Pattern

```csharp
// Interface - ATM always creates a transaction
public interface IAtmGrain : IGrainWithIntegerKey
{
    [Transaction(TransactionOption.Create)]
    Task Transfer(string fromId, string toId, decimal amount);
}

// Interface - Account joins an existing transaction
public interface IAccountGrain : IGrainWithStringKey
{
    [Transaction(TransactionOption.Join)]
    Task Withdraw(decimal amount);

    [Transaction(TransactionOption.Join)]
    Task Deposit(decimal amount);

    [Transaction(TransactionOption.CreateOrJoin)]
    Task<decimal> GetBalance();
}
```

### ITransactionalState<TState> Interface

All reads and writes to transactional state go through synchronous functions:

```csharp
public interface ITransactionalState<TState> where TState : class, new()
{
    Task<TResult> PerformRead<TResult>(Func<TState, TResult> readFunction);
    Task<TResult> PerformUpdate<TResult>(Func<TState, TResult> updateFunction);
}
```

The transaction system ensures:
- All changes across multiple grains in a transaction are committed atomically.
- If any part fails, all changes are rolled back.
- State access via `PerformRead` / `PerformUpdate` allows the system to track and
  manage changes transactionally.

### Grain Implementation

```csharp
[Reentrant]  // REQUIRED for transactional grains
public class AccountGrain : Grain, IAccountGrain
{
    private readonly ITransactionalState<Balance> _balance;

    public AccountGrain(
        [TransactionalState(nameof(balance))]
        ITransactionalState<Balance> balance) =>
        _balance = balance;

    public Task Deposit(decimal amount) =>
        _balance.PerformUpdate(b => b.Value += amount);

    public Task Withdraw(decimal amount) =>
        _balance.PerformUpdate(b =>
        {
            if (b.Value < amount)
                throw new InvalidOperationException("Overdraw");
            b.Value -= amount;
        });

    public Task<decimal> GetBalance() =>
        _balance.PerformRead(b => b.Value);
}
```

**Important**: Transactional grains MUST be marked `[Reentrant]` to allow the
transaction context to propagate correctly.

### Calling Transactions from Client

Use `ITransactionClient`:

```csharp
await transactionClient.RunTransaction(
    TransactionOption.Create,
    async () =>
    {
        await fromAccount.Withdraw(amount);
        await toAccount.Deposit(amount);
    });
```

### Transaction Error Handling

- `OrleansTransactionAbortedException`: Transaction failed and CAN be retried.
- `OrleansTransactionException`: Transaction terminated with unknown state (may have
  succeeded, failed, or still be in progress).
- Application exceptions during a transaction become the `InnerException` of
  `OrleansTransactionException`.
- Best practice: wait for the call timeout period (`SiloMessagingOptions.SystemResponseTimeout`)
  before verifying state or retrying.

### Transaction Implementation Details

- Transactions are distributed across grains that may be on different silos.
- The transaction manager coordinates prepare/commit/abort phases.
- `OnActivateAsync` CANNOT read transactional state (setup is not yet complete).
- Each transactional state is declared with a name and optional storage provider name
  via `[TransactionalState("stateName", "storageName")]`.

### Python Implementation Notes

For Python transactions:
- Implement a transaction coordinator that tracks participating grains.
- Each grain's transactional state should support versioning (read version, write
  version) for optimistic concurrency control.
- Two-phase commit: prepare phase (all participants confirm), commit phase (all apply).
- Rollback: if any participant fails during prepare, all abort.
- State access through `perform_read` / `perform_update` functions that capture
  reads and writes for transaction tracking.

---

## 4. Grain Observers (Pub/Sub Notifications)

Observers allow grains to send asynchronous notifications to clients or other grains.

### IGrainObserver Interface

Observer interfaces must inherit from `IGrainObserver`:

```csharp
public interface IChat : IGrainObserver
{
    Task ReceiveMessage(string message);
}
```

Methods should return `Task`, `Task<T>`, `ValueTask`, or `ValueTask<T>`. Avoid `void`
return types (can lead to `async void` which crashes on exceptions).

For fire-and-forget notifications, use `[OneWay]` attribute on the method.

### Observer Implementation (Client Side)

```csharp
public class Chat : IChat
{
    public Task ReceiveMessage(string message)
    {
        Console.WriteLine(message);
        return Task.CompletedTask;
    }
}
```

### ObserverManager<T>

A utility class for managing observer subscriptions with automatic cleanup:

```csharp
class HelloGrain : Grain, IHello
{
    private readonly ObserverManager<IChat> _subsManager;

    public HelloGrain(ILogger<HelloGrain> logger)
    {
        _subsManager = new ObserverManager<IChat>(
            TimeSpan.FromMinutes(5), logger);  // Auto-expire after 5 min
    }

    public Task Subscribe(IChat observer)
    {
        _subsManager.Subscribe(observer, observer);
        return Task.CompletedTask;
    }

    public Task UnSubscribe(IChat observer)
    {
        _subsManager.Unsubscribe(observer);
        return Task.CompletedTask;
    }

    public Task SendUpdateMessage(string message)
    {
        _subsManager.Notify(s => s.ReceiveMessage(message));
        return Task.CompletedTask;
    }
}
```

### Creating Observer References

**From a client:**
```csharp
Chat c = new Chat();
var obj = grainFactory.CreateObjectReference<IChat>(c);
await friend.Subscribe(obj);

// Cleanup when done:
await friend.Unsubscribe(obj);
grainFactory.DeleteObjectReference<IChat>(obj);
```

**From a grain (subscribing itself):**
```csharp
// Grains are already addressable, no CreateObjectReference needed
await someGrain.Subscribe(this.AsReference<IChat>());
```

### Observer Lifecycle and Reliability

- Observers are **inherently unreliable** -- clients can fail and never recover.
- `ObserverManager<T>` removes subscriptions after a configured duration.
- Active clients should **resubscribe on a timer** to keep subscriptions alive.
- Observer references use `WeakReference<T>` internally -- the object may be GC'd
  if no other references exist.
- Always call `DeleteObjectReference` when done to avoid memory leaks.

### Observer Execution Model

- Each observer reference processes requests one by one, to completion (non-reentrant).
- Multiple different observers can process requests in parallel.
- `[AlwaysInterleave]` and `[Reentrant]` attributes do NOT affect observer execution.

### CancellationToken Support (Orleans 9.0+)

Observer interface methods support `CancellationToken` parameters:

```csharp
public interface IDataObserver : IGrainObserver
{
    Task OnDataReceivedAsync(DataPayload data, CancellationToken ct = default);
}
```

### Python Implementation Notes

- Observers map to callback registrations. A grain maintains a list of observer
  references (which are essentially remote callable endpoints).
- `ObserverManager` is a dict of observer references with TTL-based expiration.
- `Notify` iterates all registered observers and sends them the notification.
- Unreliable delivery: failed notifications should remove the observer silently.

---

## 5. Grain Services

Grain services are **singleton services that run on every silo** from startup to
shutdown. They are special grains with no stable identity that support partitioned
responsibility across the cluster.

### Use Cases

- Background processing distributed across the cluster.
- Reminder services (Orleans Reminders are implemented as grain services).
- Distributed monitoring or data collection.

### Creating a Grain Service

**Step 1: Define the interface**
```csharp
public interface IDataService : IGrainService
{
    Task MyMethod();
}
```

**Step 2: Implement the service**
```csharp
[Reentrant]
public class DataService : GrainService, IDataService
{
    private readonly IGrainFactory _grainFactory;

    public DataService(
        IServiceProvider services,
        GrainId id,
        Silo silo,
        ILoggerFactory loggerFactory,
        IGrainFactory grainFactory)
        : base(id, silo, loggerFactory)
    {
        _grainFactory = grainFactory;
    }

    public override Task Init(IServiceProvider serviceProvider) =>
        base.Init(serviceProvider);

    public override Task Start() => base.Start();
    public override Task Stop() => base.Stop();

    public Task MyMethod()
    {
        // Custom logic
        return Task.CompletedTask;
    }
}
```

**Step 3: Define a client interface**
```csharp
public interface IDataServiceClient : IGrainServiceClient<IDataService>, IDataService
{
}
```

**Step 4: Implement the client proxy**
```csharp
public class DataServiceClient : GrainServiceClient<IDataService>, IDataServiceClient
{
    public DataServiceClient(IServiceProvider serviceProvider)
        : base(serviceProvider) { }

    private IDataService GrainService =>
        GetGrainService(CurrentGrainReference.GrainId);

    public Task MyMethod() => GrainService.MyMethod();
}
```

**Step 5: Register**
```csharp
siloBuilder.Services
    .AddGrainService<DataService>()
    .AddSingleton<IDataServiceClient, DataServiceClient>();
```

### Key Properties

- Grain services initialize when the silo starts, before the silo completes startup.
- They are NOT collected when idle -- they live for the silo's lifetime.
- The `GrainServiceClient` routes calls to the appropriate silo's grain service based
  on the calling grain's ID (partitioned responsibility).
- Calls from the client may go to ANY silo in the cluster (not necessarily local).

### Python Implementation Notes

- Grain services are background async tasks that start with each silo/runtime node.
- They can be implemented as special grain types with a fixed identity per silo.
- The client proxy uses a hash/ring to determine which silo's grain service is
  responsible for a given grain.

---

## 6. Request Context (Call Chain Context)

`RequestContext` allows application metadata to flow with requests across grain calls.

### API

```csharp
// Set a value (must be serializable)
RequestContext.Set("TraceId", Guid.NewGuid());

// Get a value
object traceId = RequestContext.Get("TraceId");
```

### Propagation Rules

- When a caller sends a request, the RequestContext contents are included in the
  Orleans message.
- When the receiving grain makes calls to other grains, the context propagates
  automatically.
- Context is maintained through `Task.StartNew` and `ContinueWith` (snapshot at
  scheduling time).
- **Context does NOT flow back with responses.** After awaiting a call, the original
  request's context is still active.
- Changes made by a callee do not propagate back to the caller.

### Use Cases

- Distributed tracing (trace IDs, correlation IDs).
- Activity tracking.
- Tenant identification in multi-tenant systems.
- Custom placement decisions (RequestContext data is available in placement filters
  via `PlacementTarget.RequestContextData`).

### Backing Storage

The backing storage is **async-local** (similar to Python's `contextvars`). Each
async context has its own copy.

### Best Practices

- Use simple types (strings, GUIDs, numbers) to minimize serialization overhead.
- Large or complex objects add noticeable overhead.

### Python Implementation Notes

- Use Python's `contextvars.ContextVar` for the async-local storage.
- When sending a message to another grain, serialize the current context dict and
  attach it to the message.
- When receiving a message, restore the context from the message metadata.
- Context is snapshot-based: copies are made at scheduling time.

---

## 7. Grain Extensions

Grain extensions allow adding extra behavior to grains without modifying the grain
class itself. Extensions implement `IGrainExtension`.

### How It Works

1. Define an extension interface inheriting from `IGrainExtension`.
2. Implement the extension class.
3. Register the extension globally or per-grain.
4. Access the extension from any grain reference via `grain.AsReference<IMyExtension>()`.

### Example: Deactivate Extension

```csharp
// Interface
public interface IGrainDeactivateExtension : IGrainExtension
{
    Task Deactivate(string msg);
}

// Implementation -- receives IGrainContext via DI
public sealed class GrainDeactivateExtension : IGrainDeactivateExtension
{
    private IGrainContext _context;

    public GrainDeactivateExtension(IGrainContext context)
    {
        _context = context;
    }

    public Task Deactivate(string msg)
    {
        _context.Deactivate(
            new DeactivationReason(DeactivationReasonCode.ApplicationRequested, msg));
        return Task.CompletedTask;
    }
}

// Registration
siloBuilder.AddGrainExtension<IGrainDeactivateExtension, GrainDeactivateExtension>();

// Usage
var ext = grain.AsReference<IGrainDeactivateExtension>();
await ext.Deactivate("Because I said so");
```

### Per-Grain Extensions

Extensions can also be registered per-grain in `OnActivateAsync` using
`GrainContext.SetComponent<T>(instance)`:

```csharp
public override Task OnActivateAsync()
{
    var accessor = new GrainStateAccessor<int>(
        getter: () => this.Value,
        setter: value => this.Value = value);
    ((IGrainBase)this).GrainContext.SetComponent<IGrainStateAccessor<int>>(accessor);
    return base.OnActivateAsync();
}
```

### Python Implementation Notes

- Extensions are essentially mixins or decorators that attach to a grain's message
  dispatch.
- The grain runtime checks if a method target is an extension interface and routes
  to the extension handler instead of the grain itself.
- Global extensions are registered with the runtime; per-grain extensions are
  registered during activation.

---

## 8. Dependency Injection in Grains

Orleans uses standard .NET dependency injection (Microsoft.Extensions.DependencyInjection)
for grains.

### Constructor Injection

Grains receive dependencies through their constructor:

```csharp
public class MyGrain : Grain, IMyGrain
{
    private readonly ILogger<MyGrain> _logger;
    private readonly IMyService _service;

    public MyGrain(ILogger<MyGrain> logger, IMyService service)
    {
        _logger = logger;
        _service = service;
    }
}
```

### Available Injectable Services

- `ILogger<T>` -- logging
- `IGrainFactory` -- creating grain references
- `IGrainContext` -- the grain's activation context
- `IServiceProvider` -- service locator (not recommended but available)
- Custom registered services (registered via `siloBuilder.Services`)

### Service Lifetimes

- **Singleton**: One instance for the entire silo process.
- **Transient**: New instance every time it is requested.
- **Scoped**: Scoped to the grain activation. Each grain activation gets its own
  scope (a new `IServiceScope` is created for each activation).

### Special DI Features

- `[TransactionalState("name")]` attribute on constructor parameters triggers DI of
  `ITransactionalState<T>` instances.
- `[PersistentState("name", "storage")]` attribute triggers DI of
  `IPersistentState<T>` instances.
- Components injected into grains can participate in the grain lifecycle by accepting
  `IGrainContext` and subscribing to `context.ObservableLifecycle`.

### Lifecycle-Aware Components

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
        lifecycle.Subscribe<MyComponent>(
            GrainLifecycleStage.Activate, OnActivate);
    }

    private Task OnActivate(CancellationToken ct) { /* ... */ }
}

// Registration
services.AddTransient<MyComponent>(sp =>
    MyComponent.Create(sp.GetRequiredService<IGrainContext>()));
```

### Python Implementation Notes

- Use a DI container or simple factory pattern.
- Grain constructors receive dependencies from the runtime.
- Scoped services: create a new scope per grain activation.
- Lifecycle-aware components: allow dependencies to hook into `on_activate` /
  `on_deactivate` events.

---

## 9. Cancellation and Timeouts

### CancellationToken Support

Orleans supports passing `CancellationToken` to grain interface methods (as the last
parameter). Features:

- Works for regular grain methods, streaming (`IAsyncEnumerable<T>`), and
  grain-to-grain calls.
- Cancellation is **cooperative**: the grain must check the token and respond.
- If the token is already canceled before the call, `OperationCanceledException`
  is thrown immediately without issuing the request.
- If a queued request is canceled before execution starts, it is canceled without
  being executed.
- Cancellation signals propagate across silo boundaries.

```csharp
public interface IProcessingGrain : IGrainWithGuidKey
{
    Task<string> ProcessDataAsync(
        string data, int chunks,
        CancellationToken cancellationToken = default);
}
```

### Configuration Options

```csharp
siloBuilder.Configure<SiloMessagingOptions>(options =>
{
    // Auto-cancel on timeout (default: true)
    options.CancelRequestOnTimeout = true;

    // Wait for callee acknowledgment (default: false)
    options.WaitForCancellationAcknowledgement = false;
});
```

### Timeout Configuration

- `SiloMessagingOptions.SystemResponseTimeout`: The automatic message timeout.
- Every message has an automatic timeout; if no reply arrives in time, the returned
  Task fails with a `TimeoutException`.

### Backward Compatibility

- Adding a `CancellationToken` parameter to an existing method is backward compatible.
  Old callers that don't provide a token will receive `CancellationToken.None`.
- Removing a `CancellationToken` parameter is also backward compatible. The runtime
  ignores the extra token.

### Legacy: GrainCancellationToken

Prior to native `CancellationToken` support, Orleans used `GrainCancellationToken`
and `GrainCancellationTokenSource`. This is now legacy. The `Cancel()` method on the
source returns a `Task` and may need retrying for robustness.

### Python Implementation Notes

- Use `asyncio.CancelledError` for cancellation propagation.
- Each grain call can carry a timeout. The runtime starts a timer and cancels the
  request if the timeout elapses.
- Cancellation tokens can be implemented as shared event objects that signal
  cancellation across grain boundaries.

---

## 10. Messaging Delivery Guarantees and Dead Letters

### Delivery Guarantees

- **Default: At-most-once delivery.** A message is delivered once or not at all.
  It is never delivered twice.
- With retries configured: **at-least-once delivery** (messages may arrive multiple
  times).
- Orleans does NOT durably store which messages have been delivered (deduplication
  is too costly).
- With infinite retries: messages eventually arrive because grains never enter a
  permanent failure state (a failed grain eventually reactivates on another silo).

### Message Timeouts

- Every Orleans message has an automatic timeout.
- Configurable via `SiloMessagingOptions.SystemResponseTimeout`.
- On timeout, the returned `Task` breaks with a `TimeoutException`.
- By default, Orleans does NOT perform automatic retries (application code can retry).

### Dead Letter Handling

Orleans does not have an explicit "dead letter queue" concept. Instead:
- Messages to non-existent grains cause the grain to be activated (Virtual Actor
  model -- grains are always virtually present).
- Messages that timeout are reported as `TimeoutException` to the caller.
- Messages to failed/unreachable silos are rerouted once the membership protocol
  detects the failure.
- The caller is responsible for retry logic.

### Python Implementation Notes

- Implement message timeouts using `asyncio.wait_for`.
- Default: at-most-once. If the call times out, return an error.
- Optional retry wrapper for at-least-once semantics.
- No dead letter queue needed if using virtual actor model (grains auto-activate).

---

## 11. Placement Strategies

Placement determines which silo activates a new grain. Orleans provides several
built-in strategies and supports custom strategies.

### Built-in Strategies

| Strategy | Attribute | Description |
|----------|-----------|-------------|
| **Resource-Optimized** (default in 9.2+) | `[ResourceOptimizedPlacement]` | Balances based on CPU, memory, and activation count with weighted scoring. |
| **Random** (default pre-9.2) | `[RandomPlacement]` | Randomly selects a compatible server. Works well with large numbers of grains. |
| **Prefer Local** | `[PreferLocalPlacement]` | Selects the local server if compatible, otherwise random. |
| **Hash-Based** | `[HashBasedPlacement]` | Hashes grain ID to select server. Deterministic but NOT stable across membership changes. |
| **Activation-Count-Based** | `[ActivationCountBasedPlacement]` | Selects least-loaded server based on activation counts. Uses "Power of Two Choices" algorithm. |
| **Stateless Worker** | `[StatelessWorker]` | Multiple activations per silo, prefer local, not registered in grain directory. |
| **Silo-Role-Based** | `[SiloRoleBasedPlacement]` | Deterministic placement on silos with a specific role. |

### Resource-Optimized Placement (Default)

Configurable weights for different resource metrics:

```csharp
siloBuilder.Configure<ResourceOptimizedPlacementOptions>(options =>
{
    options.CpuUsageWeight = 40;           // Favor low CPU usage
    options.MemoryUsageWeight = 20;        // Favor low memory usage
    options.AvailableMemoryWeight = 20;    // Favor more available memory
    options.MaxAvailableMemoryWeight = 5;  // Favor higher total memory
    options.ActivationCountWeight = 15;    // Favor fewer activations
    options.LocalSiloPreferenceMargin = 5; // Slight preference for local
});
```

Uses an adaptive algorithm with Kalman filtering to smooth resource signals and
avoid rapid saturation of newly joined silos.

### Activation-Count-Based Placement

- Based on "Power of Two Choices" (Mitzenmacher's thesis).
- Servers periodically publish activation counts.
- The director randomly selects N servers (default 2) and picks the one with the
  fewest predicted activations.
- Configurable via `ActivationCountBasedPlacementOptions`.

### Stateless Worker Grains

```csharp
[StatelessWorker]       // Default: max activations = CPU cores
[StatelessWorker(1)]    // Limit to 1 activation per silo
public class MyWorker : Grain, IMyWorker { }
```

Properties:
- Multiple activations of the same grain identity per silo.
- Prefer local execution (no network hop).
- Auto-scales up under load, scales down when idle.
- NOT registered in the grain directory.
- Non-reentrant by default (add `[Reentrant]` if desired).

### Custom Placement Strategy

**Step 1: Implement `IPlacementDirector`:**
```csharp
public class MyDirector : IPlacementDirector
{
    public Task<SiloAddress> OnAddActivation(
        PlacementStrategy strategy,
        PlacementTarget target,
        IPlacementContext context)
    {
        var silos = context.GetCompatibleSilos(target).OrderBy(s => s).ToArray();
        int idx = GetSiloNumber(target.GrainIdentity.PrimaryKey, silos.Length);
        return Task.FromResult(silos[idx]);
    }
}
```

**Step 2: Define strategy and attribute:**
```csharp
[Serializable]
public sealed class MyPlacementStrategy : PlacementStrategy { }

[AttributeUsage(AttributeTargets.Class, AllowMultiple = false)]
public sealed class MyPlacementAttribute : PlacementAttribute
{
    public MyPlacementAttribute() : base(new MyPlacementStrategy()) { }
}
```

**Step 3: Apply to grain and register:**
```csharp
[MyPlacement]
public class MyGrain : Grain, IMyGrain { }

// Registration
services.AddPlacementDirector<MyPlacementStrategy, MyDirector>();
```

### Changing the Default Strategy

```csharp
siloBuilder.Services.AddSingleton<PlacementStrategy, RandomPlacement>();
```

### Placement Filtering (Orleans 9.0+)

Filters narrow eligible silos before the placement strategy runs:
- Zone-aware placement
- Tiered deployments
- Hardware affinity

Custom filters implement `IPlacementFilterDirector`:

```csharp
internal sealed class MyFilter : IPlacementFilterDirector
{
    public IEnumerable<SiloAddress> Filter(
        PlacementFilterStrategy filterStrategy,
        PlacementTarget target,
        IEnumerable<SiloAddress> silos)
    {
        // Filter logic using target.RequestContextData, silo metadata, etc.
        return silos;
    }
}
```

### Activation Repartitioning (Experimental)

Monitors grain-to-grain communication patterns and migrates grains to minimize
network hops:
- Uses probabilistic tracking of communication edges.
- Maintains balanced activation distribution.
- Recovery period between rounds to stabilize.
- Configurable via `ActivationRepartitionerOptions`.

### Activation Rebalancing (Orleans 10.0, Experimental)

Cluster-wide redistribution of activations for balanced memory and activation counts:
- Singleton coordinator grain calculates entropy (measure of balance).
- Migrates activations from "heavy" to "light" silos.
- Session-based with configurable cycle periods.
- Considers both memory and activation counts.

### Python Implementation Notes

For a Python implementation:
- `RandomPlacement`: `random.choice(available_silos)`
- `PreferLocalPlacement`: use local silo if available, else random
- `HashBasedPlacement`: `hash(grain_id) % len(silos)`
- `ActivationCountBased`: track counts, use power-of-two-choices
- `StatelessWorker`: allow multiple activations per node, prefer local, scale
  up/down based on queue depth
- Custom placement: pluggable strategy interface
- Resource-optimized: collect CPU/memory metrics, weighted scoring

---

## 12. Grain Lifecycle

### Lifecycle Stages

```
First (int.MinValue)
  -> SetupState (1000)    -- Load persistent state from storage
    -> Activate (2000)    -- OnActivateAsync / OnDeactivateAsync
      -> Last (int.MaxValue)
```

### Key Methods

- `OnActivateAsync(CancellationToken)`: Called when a grain is activated. Used for
  initialization logic.
- `OnDeactivateAsync(DeactivationReason, CancellationToken)`: Called when a grain is
  deactivated. NOT guaranteed to be called (e.g., silo crash).
- `Participate(IGrainLifecycle)`: Override to hook into lifecycle stages.

### Custom Lifecycle Participation

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

### Grain Collection (Deactivation)

- Idle grains are deactivated after a configurable period (`CollectionAge`,
  default 15 minutes).
- Collection runs periodically (`CollectionQuantum`, default 1 minute).
- Memory-based activation shedding (Orleans 9.0+): automatically deactivates grains
  under memory pressure.

### Grain Migration (Orleans 8.0+)

Grains can be migrated to different silos while preserving in-memory state:
- **Dehydration**: Serialize state on source silo.
- **Transfer**: Send to target silo.
- **Rehydration**: Restore state before `OnActivateAsync`.
- Triggered by `this.MigrateOnIdle()` or automatic repartitioning/rebalancing.
- Grains using `Grain<TState>` or `IPersistentState<T>` automatically support migration.
- `[Immovable]` attribute prevents automatic migration.

### Python Implementation Notes

- Lifecycle is a series of hooks: `on_activate`, `on_deactivate`.
- Idle collection: track last activity time, deactivate after timeout.
- Migration: serialize grain state, send to target node, deserialize and reactivate.

---

## 13. Stateless Worker Grains

Stateless workers break the single-activation rule. Properties:

1. **Multiple activations** of the same grain on different silos.
2. **Prefer local execution** -- no network/serialization cost.
3. **Auto-scaling** -- runtime creates more activations when existing ones are busy,
   up to a configurable limit (default: CPU core count).
4. **Not individually addressable** -- two requests may hit different activations.
5. **Not in grain directory** -- no registration overhead.
6. **Scale down automatically** -- idle activations at the tail are collected.

### Use Cases

- Stateless computation (decompression, validation, routing).
- Scaled-out hot cache items.
- Reduce-style aggregation (pre-aggregate on local stateless workers, then send
  to a global aggregator).

### Python Implementation Notes

- Maintain a pool of worker instances per node.
- Route requests to the first idle instance.
- Create new instances on demand up to a limit.
- Deactivate idle tail instances periodically.

---

## 14. Summary Table: Key Attributes and Their Effects

| Attribute | Target | Effect |
|-----------|--------|--------|
| `[Reentrant]` | Grain class | All methods interleave at await points |
| `[AlwaysInterleave]` | Interface method | Method always interleaves with everything |
| `[ReadOnly]` | Interface method | Concurrent with other ReadOnly methods |
| `[MayInterleave(pred)]` | Grain class | Per-call interleave decision via predicate |
| `[StatelessWorker(n)]` | Grain class | Multiple activations, prefer local, no directory |
| `[Transaction(opt)]` | Interface method | ACID transaction participation |
| `[TransactionalState(name)]` | Constructor param | Inject transactional state |
| `[PersistentState(name, store)]` | Constructor param | Inject persistent state |
| `[OneWay]` | Interface method | Fire-and-forget (no response) |
| `[Immovable]` | Grain class | Prevent automatic migration |
| `[RandomPlacement]` | Grain class | Random silo selection |
| `[PreferLocalPlacement]` | Grain class | Prefer local silo |
| `[HashBasedPlacement]` | Grain class | Hash-based deterministic placement |
| `[ActivationCountBasedPlacement]` | Grain class | Least-loaded silo (power of two choices) |
| `[ResourceOptimizedPlacement]` | Grain class | Weighted resource-based placement |
| `[SiloRoleBasedPlacement]` | Grain class | Role-based deterministic placement |
| `[GrainType("name")]` | Grain class | Custom grain type name |

---

## Sources

- [Orleans Transactions](https://learn.microsoft.com/en-us/dotnet/orleans/grains/transactions)
- [Orleans Request Scheduling (Reentrancy)](https://learn.microsoft.com/en-us/dotnet/orleans/grains/request-scheduling)
- [Orleans Grain Placement](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-placement)
- [Orleans Observers](https://learn.microsoft.com/en-us/dotnet/orleans/grains/observers)
- [Orleans Grain Extensions](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-extensions)
- [Orleans Request Context](https://learn.microsoft.com/en-us/dotnet/orleans/grains/request-context)
- [Orleans Cancellation Tokens](https://learn.microsoft.com/en-us/dotnet/orleans/grains/cancellation-tokens)
- [Orleans Grain Lifecycle](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-lifecycle)
- [Orleans Grain Services](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grainservices)
- [Orleans External Tasks and Grains](https://learn.microsoft.com/en-us/dotnet/orleans/grains/external-tasks-and-grains)
- [Orleans Stateless Worker Grains](https://learn.microsoft.com/en-us/dotnet/orleans/grains/stateless-worker-grains)
- [Orleans Messaging Delivery Guarantees](https://learn.microsoft.com/en-us/dotnet/orleans/implementation/messaging-delivery-guarantees)
- [Orleans Grain Identity](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-identity)
