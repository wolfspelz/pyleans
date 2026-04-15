# Orleans Persistence and Storage System

## Table of Contents

1. [Overview](#overview)
2. [Grain State Persistence Model](#grain-state-persistence-model)
3. [IPersistentState and the Declarative Model](#ipersistentstate-and-the-declarative-model)
4. [IGrainStorage Interface](#igrainstorage-interface)
5. [IGrainState Interface](#igrainstate-interface)
6. [State Lifecycle: Read on Activation, Write on Explicit Call](#state-lifecycle)
7. [ETags and Optimistic Concurrency](#etags-and-optimistic-concurrency)
8. [Built-in Storage Providers](#built-in-storage-providers)
9. [Custom Storage Provider Implementation](#custom-storage-provider-implementation)
10. [Serialization](#serialization)
11. [Grain State Versioning](#grain-state-versioning)
12. [Event Sourcing and Journaled Grains](#event-sourcing-and-journaled-grains)
13. [Log-Consistency Providers](#log-consistency-providers)
14. [Key Design Patterns for Python Implementation](#key-design-patterns-for-python-implementation)

---

## Overview

Orleans grain persistence uses an extensible plugin model that allows storage providers for any database backend. The persistence model is designed for simplicity and is not intended to cover all data access patterns. Grains can also access databases directly without using the grain persistence model.

Key design goals:

1. Support multiple named persistent data objects per grain.
2. Allow multiple configured storage providers, each potentially with different configurations and backed by different storage systems.
3. Enable the community to develop and publish storage providers.
4. Give storage providers complete control over how they store grain state data in persistent backing store. Orleans does not provide a comprehensive ORM solution.

Architecture diagram concept:

```
UserGrain
  |-- "profile" state --> profileStore (e.g., Azure Table Storage)
  |-- "cart" state    --> cartStore   (e.g., Azure Blob Storage)
```

Each grain can have multiple named persistent data objects. Different states can be stored in different storage systems.

---

## IPersistentState and the Declarative Model

### The Modern Approach: IPersistentState<TState>

The recommended way to add storage to a grain is by injecting `IPersistentState<TState>` into the grain's constructor with an associated `[PersistentState]` attribute.

#### Interface Hierarchy (Orleans 7.0+)

```csharp
public interface IPersistentState<TState> : IStorage<TState>
{
}

public interface IStorage<TState> : IStorage
{
    TState State { get; set; }
}

public interface IStorage
{
    string Etag { get; }
    bool RecordExists { get; }
    Task ClearStateAsync();
    Task WriteStateAsync();
    Task ReadStateAsync();
}
```

#### Interface Hierarchy (Orleans 3.x)

```csharp
public interface IPersistentState<TState> where TState : new()
{
    TState State { get; set; }
    string Etag { get; }
    Task ClearStateAsync();
    Task WriteStateAsync();
    Task ReadStateAsync();
}
```

### Declaring Persistent State

The `[PersistentState]` attribute takes two arguments:
- `stateName`: The name of the state (used as a key in storage).
- `storageName`: The name of the registered storage provider.

```csharp
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

### State Class Definition

```csharp
[Serializable]
public class ProfileState
{
    public string Name { get; set; }
    public Date DateOfBirth { get; set; }
}
```

### Legacy Approach: Grain<TGrainState>

Grain classes inheriting from `Grain<TGrainState>` have their state loaded automatically from storage. This is still supported but considered legacy.

```csharp
[StorageProvider(ProviderName = "store1")]
public class MyGrain : Grain<MyGrainState>, IMyGrain
{
    // Access state via this.State property
    // Call this.ReadStateAsync(), this.WriteStateAsync(), this.ClearStateAsync()
}
```

The base class provides:

```csharp
protected virtual Task ReadStateAsync();
protected virtual Task WriteStateAsync();
protected virtual Task ClearStateAsync();
```

---

## IGrainStorage Interface

This is the core interface that all storage providers must implement. It is the bridge between the Orleans runtime and the underlying storage system.

### Orleans 7.0+ (Current)

```csharp
/// <summary>
/// Interface to be implemented for a storage able to read and write Orleans grain state data.
/// </summary>
public interface IGrainStorage
{
    /// <summary>Read data function for this storage instance.</summary>
    /// <param name="stateName">Name of the state for this grain</param>
    /// <param name="grainId">Grain ID</param>
    /// <param name="grainState">State data object to be populated for this grain.</param>
    /// <typeparam name="T">The grain state type.</typeparam>
    /// <returns>Completion promise for the Read operation on the specified grain.</returns>
    Task ReadStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState);

    /// <summary>Write data function for this storage instance.</summary>
    /// <param name="stateName">Name of the state for this grain</param>
    /// <param name="grainId">Grain ID</param>
    /// <param name="grainState">State data object to be written for this grain.</param>
    /// <typeparam name="T">The grain state type.</typeparam>
    /// <returns>Completion promise for the Write operation on the specified grain.</returns>
    Task WriteStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState);

    /// <summary>Delete / Clear data function for this storage instance.</summary>
    /// <param name="stateName">Name of the state for this grain</param>
    /// <param name="grainId">Grain ID</param>
    /// <param name="grainState">Copy of last-known state data object for this grain.</param>
    /// <typeparam name="T">The grain state type.</typeparam>
    /// <returns>Completion promise for the Delete operation on the specified grain.</returns>
    Task ClearStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState);
}
```

### Orleans 3.x (Legacy)

```csharp
public interface IGrainStorage
{
    Task ReadStateAsync(
        string grainType, GrainReference grainReference, IGrainState grainState);

    Task WriteStateAsync(
        string grainType, GrainReference grainReference, IGrainState grainState);

    Task ClearStateAsync(
        string grainType, GrainReference grainReference, IGrainState grainState);
}
```

Key differences between versions:
- Orleans 7.0+ uses `GrainId` (a struct) instead of `GrainReference` (a class).
- Orleans 7.0+ uses `stateName` instead of `grainType`.
- Orleans 7.0+ uses the generic `IGrainState<T>` instead of non-generic `IGrainState`.

---

## IGrainState Interface

The `IGrainState` interface is the container that the runtime passes to the storage provider. The storage provider is responsible for populating it on reads and reading from it on writes.

```csharp
public interface IGrainState<TState>
{
    /// <summary>The user-defined state object.</summary>
    TState State { get; set; }

    /// <summary>
    /// An opaque, provider-assigned ETag for the current state.
    /// Storage providers set this after ReadStateAsync and WriteStateAsync.
    /// </summary>
    string ETag { get; set; }

    /// <summary>
    /// Whether a record exists in storage for this grain.
    /// Set by the storage provider after ReadStateAsync.
    /// </summary>
    bool RecordExists { get; set; }
}
```

---

## State Lifecycle

### Read on Activation

- Grain state is **automatically read from storage when the grain activates**. The runtime calls `ReadStateAsync` on the storage provider before calling `OnActivateAsync()` on the grain.
- The state is **NOT loaded at constructor injection time**. Accessing the state in the constructor is invalid. It is only available by the time `OnActivateAsync` is called.
- If the storage provider fails to read the state, the grain activation fails. No call to `OnActivateAsync` will be made, and the original request to the grain faults back to the caller.

### Write on Explicit Call

- **Modifications to the state are NOT automatically persisted.** The grain developer must explicitly call `WriteStateAsync()` to persist changes.
- The Orleans Runtime conceptually takes a deep copy of the grain state data object for write operations. Under the covers, the runtime may use optimization rules and heuristics to avoid some deep copies, provided logical isolation semantics are preserved.

### Clear State

- `ClearStateAsync()` clears the grain's state in storage. Depending on the provider, this may optionally delete the grain state entirely.

### Re-reading State

- A grain can explicitly re-read its latest state from storage by calling `ReadStateAsync()`. This is useful when state may have been modified externally.
- During normal operation there is no need to call `ReadStateAsync()` since Orleans loads state automatically during activation.

### Failure Modes

**Read failures:**
- Failure during initial read fails the grain activation.
- Failure during explicit `ReadStateAsync()` results in a faulted Task that the grain can handle.
- Sending a message to a grain with a bad/missing storage provider returns `BadProviderConfigException`.

**Write failures:**
- Failure during `WriteStateAsync()` results in a faulted Task.
- Usually this faults back to the client caller.
- Grain code can catch and handle write failures explicitly.

---

## ETags and Optimistic Concurrency

Orleans uses ETags (Entity Tags) to implement optimistic concurrency control on grain state writes.

### How It Works

1. When state is read from storage, the storage provider sets an opaque `ETag` string on the `IGrainState` object.
2. When state is written back, the storage provider checks whether the ETag matches the one currently in storage.
3. If the ETags do not match (another write occurred since the read), the write fails.

### InconsistentStateException

When an ETag constraint violation is detected, the storage provider faults the write Task with `InconsistentStateException`:

```csharp
public class InconsistentStateException : OrleansException
{
    public InconsistentStateException(
        string message,
        string storedEtag,
        string currentEtag,
        Exception storageException)
        : base(message, storageException)
    {
        StoredEtag = storedEtag;
        CurrentEtag = currentEtag;
    }

    public InconsistentStateException(
        string storedEtag,
        string currentEtag,
        Exception storageException)
        : this(storageException.Message, storedEtag, currentEtag, storageException)
    {
    }

    /// <summary>The Etag value currently held in persistent storage.</summary>
    public string StoredEtag { get; }

    /// <summary>The Etag value currently held in memory, and attempting to be updated.</summary>
    public string CurrentEtag { get; }
}
```

### Provider Semantics

- An opaque, provider-specific ETag value (string) **may** be set by a storage provider as part of grain state metadata populated during read. Some providers may leave this as `null` if they do not use ETags.
- Any attempt to perform a write operation when the storage provider detects an ETag constraint violation **should** fault the write Task with a transient `InconsistentStateException`, wrapping the underlying storage exception.
- Any other failure condition from a storage operation **must** cause the returned Task to be broken with an exception indicating the underlying storage issue.

### Practical Implications

Since Orleans grains are single-threaded (only one request at a time per grain activation), ETag conflicts are most likely to occur in these scenarios:
- Multiple activations of the same grain exist across different silos (split-brain).
- External processes modify the storage directly.
- The grain calls `WriteStateAsync()` and the previous write has not yet completed (rare due to single-threaded nature).

---

## Built-in Storage Providers

### 1. In-Memory Storage

- **Package:** Built into Orleans core (`Microsoft.Orleans.Persistence.Memory` / `MemoryGrainStorage`)
- **Purpose:** Development and testing only. State is lost when the silo restarts.
- **Configuration:**

```csharp
siloBuilder.AddMemoryGrainStorage("memoryStore");
```

### 2. Azure Table Storage

- **Package:** `Microsoft.Orleans.Persistence.AzureStorage`
- **Characteristics:**
  - Stores state in a table row.
  - Splits state across multiple columns if it exceeds Azure Table limits.
  - Maximum 1 MB per row (Azure Table Storage limit).
- **Configuration (managed identity):**

```csharp
siloBuilder.AddAzureTableGrainStorage(
    name: "profileStore",
    configureOptions: options =>
    {
        options.ConfigureTableServiceClient(
            new Uri("https://<account>.table.core.windows.net"),
            new DefaultAzureCredential());
    });
```

- **Configuration (connection string):**

```csharp
siloBuilder.AddAzureTableGrainStorage(
    name: "profileStore",
    configureOptions: options =>
    {
        options.ConfigureTableServiceClient(
            "DefaultEndpointsProtocol=https;AccountName=data1;AccountKey=SOMETHING1");
    });
```

### 3. Azure Blob Storage

- **Package:** `Microsoft.Orleans.Persistence.AzureStorage`
- **Characteristics:**
  - Stores each grain's state as a separate blob.
  - Good for larger state objects (no 1MB row limit like Table Storage).
- **Configuration (managed identity):**

```csharp
siloBuilder.AddAzureBlobGrainStorage(
    name: "cartStore",
    configureOptions: options =>
    {
        options.ConfigureBlobServiceClient(
            new Uri("https://<account>.blob.core.windows.net"),
            new DefaultAzureCredential());
    });
```

### 4. Azure Cosmos DB

- **Package:** `Microsoft.Orleans.Persistence.Cosmos`
- **Configuration:**

```csharp
siloBuilder.AddCosmosGrainStorage(
    name: "cosmos",
    configureOptions: options =>
    {
        options.ConfigureCosmosClient(
            "https://myaccount.documents.azure.com:443/",
            new DefaultAzureCredential());
        options.DatabaseName = "Orleans";
        options.ContainerName = "OrleansStorage";
        options.IsResourceCreationEnabled = true;
    });
```

- **Options include:** `DatabaseName`, `ContainerName`, `IsResourceCreationEnabled`, `DeleteStateOnClear`, `StateFieldsToIndex`, `PartitionKeyPath`, `DatabaseThroughput`, `ContainerThroughputProperties`.
- **Supports custom partition key providers:**

```csharp
public class MyPartitionKeyProvider : IPartitionKeyProvider
{
    public ValueTask<string> GetPartitionKey(string grainType, GrainId grainId)
    {
        return new ValueTask<string>(grainId.Key.ToString()!);
    }
}
```

### 5. Amazon DynamoDB

- **Package:** `Microsoft.Orleans.Persistence.DynamoDB`
- **Configuration:**

```csharp
siloBuilder.AddDynamoDBGrainStorage(
    name: "profileStore",
    configureOptions: options =>
    {
        options.AccessKey = "<DynamoDB access key>";
        options.SecretKey = "<DynamoDB secret key>";
        options.Service = "<DynamoDB region name>"; // e.g., "us-west-2"
    });
```

- Supports access key + secret key, and access key + secret key + token authentication.
- Supports custom serializer via `GrainStorageSerializer` property.

### 6. ADO.NET (Relational Databases)

- **Package:** `Microsoft.Orleans.Persistence.AdoNet`
- **Supported databases:** SQL Server, MySQL/MariaDB, PostgreSQL, Oracle
- **Configuration:**

```csharp
siloBuilder.AddAdoNetGrainStorage("OrleansStorage", options =>
{
    options.Invariant = "<ADO.NET Invariant>";  // e.g., "System.Data.SqlClient"
    options.ConnectionString = "<ConnectionString>";
});
```

- **ADO.NET Invariant names:** These identify the database vendor:
  - SQL Server: `System.Data.SqlClient`
  - MySQL: `MySql.Data.MySqlClient`
  - PostgreSQL: `Npgsql`
  - Oracle: `Oracle.DataAccess.Client`

- **Options class:**

```csharp
public class AdoNetGrainStorageOptions
{
    [Redact]
    public string ConnectionString { get; set; }

    public int InitStage { get; set; } = DEFAULT_INIT_STAGE;
    public const int DEFAULT_INIT_STAGE = ServiceLifecycleStage.ApplicationServices;

    public const string DEFAULT_ADONET_INVARIANT = AdoNetInvariants.InvariantNameSqlServer;
    public string Invariant { get; set; } = DEFAULT_ADONET_INVARIANT;

    // Orleans 7.0+:
    public IGrainStorageSerializer GrainStorageSerializer { get; set; }
    public IStorageHasherPicker HashPicker { get; set; }

    // Orleans 3.x legacy:
    // public bool UseJsonFormat { get; set; }
    // public bool UseXmlFormat { get; set; }
    // public bool IndentJson { get; set; }
    // public TypeNameHandling? TypeNameHandling { get; set; }
}
```

- **Key ADO.NET design principles:**
  - Database-vendor agnostic via ADO.NET abstraction.
  - Queries stored in external SQL scripts, allowing runtime tuning.
  - Version/ETag represented as a signed 32-bit integer in the database.
  - Built-in capability to change storage data format during round-trip (e.g., JSON to binary).
  - Design is shardable -- avoids database-dependent features like IDENTITY columns.
  - Supports vendor-specific optimizations (e.g., native UPSERT in PostgreSQL, memory-optimized tables in SQL Server).

### 7. Redis

- **Package:** `Microsoft.Orleans.Persistence.Redis`
- **Configuration:**

```csharp
siloBuilder.AddRedisGrainStorage(
    name: "redis",
    configureOptions: options =>
    {
        options.ConfigurationOptions = new ConfigurationOptions
        {
            EndPoints = { "localhost:6379" },
            AbortOnConnectFail = false
        };
    });
```

- **Options:**
  - `ConfigurationOptions`: StackExchange.Redis client configuration.
  - `DeleteStateOnClear` (bool, default `false`): Whether to delete from Redis on ClearStateAsync.
  - `EntryExpiry` (TimeSpan?, default `null`): Optional TTL for entries.
  - `GrainStorageSerializer`: Custom serializer for grain state.
  - `GetStorageKey`: Custom function to generate Redis key. Default format: `{ServiceId}/state/{grainId}/{grainType}`.

---

## Custom Storage Provider Implementation

To create a custom storage provider, implement the `IGrainStorage` interface and register it as a named service.

### Step 1: Implement IGrainStorage

```csharp
public class MyCustomGrainStorage : IGrainStorage, ILifecycleParticipant<ISiloLifecycle>
{
    private readonly string _name;
    private readonly MyCustomStorageOptions _options;
    private readonly IGrainStorageSerializer _serializer;

    public MyCustomGrainStorage(
        string name,
        MyCustomStorageOptions options,
        IGrainStorageSerializer serializer)
    {
        _name = name;
        _options = options;
        _serializer = serializer;
    }

    public async Task ReadStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState)
    {
        // 1. Construct storage key from stateName and grainId
        var key = MakeKey(stateName, grainId);

        // 2. Read from your storage backend
        var storedData = await _storage.ReadAsync(key);

        if (storedData == null)
        {
            // No state found -- use defaults
            grainState.State = Activator.CreateInstance<T>();
            grainState.ETag = null;
            grainState.RecordExists = false;
            return;
        }

        // 3. Deserialize state
        grainState.State = _serializer.Deserialize<T>(storedData.Data);
        grainState.ETag = storedData.ETag;
        grainState.RecordExists = true;
    }

    public async Task WriteStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState)
    {
        var key = MakeKey(stateName, grainId);

        // 1. Serialize state
        var data = _serializer.Serialize<T>(grainState.State);

        // 2. Write to storage with ETag check
        try
        {
            var newETag = await _storage.WriteAsync(key, data, grainState.ETag);
            grainState.ETag = newETag;
            grainState.RecordExists = true;
        }
        catch (StorageConflictException ex)
        {
            // ETag mismatch -- throw InconsistentStateException
            throw new InconsistentStateException(
                storedEtag: ex.StoredETag,
                currentEtag: grainState.ETag,
                storageException: ex);
        }
    }

    public async Task ClearStateAsync<T>(
        string stateName, GrainId grainId, IGrainState<T> grainState)
    {
        var key = MakeKey(stateName, grainId);

        // Delete or reset in storage
        await _storage.DeleteAsync(key, grainState.ETag);

        grainState.State = Activator.CreateInstance<T>();
        grainState.ETag = null;
        grainState.RecordExists = false;
    }

    public void Participate(ISiloLifecycle lifecycle)
    {
        lifecycle.Subscribe(
            observerName: _name,
            stage: ServiceLifecycleStage.ApplicationServices,
            onStart: ct => InitializeAsync(ct));
    }
}
```

### Step 2: Register the Storage Provider

Storage providers are registered as named services in the DI container:

```csharp
public static class MyCustomStorageExtensions
{
    public static ISiloBuilder AddMyCustomGrainStorage(
        this ISiloBuilder builder,
        string name,
        Action<MyCustomStorageOptions> configureOptions)
    {
        return builder.ConfigureServices(services =>
        {
            services.AddOptions<MyCustomStorageOptions>(name)
                .Configure(configureOptions);

            services.AddSingletonNamedService<IGrainStorage>(name,
                (serviceProvider, key) =>
                {
                    var options = serviceProvider
                        .GetRequiredService<IOptionsMonitor<MyCustomStorageOptions>>()
                        .Get(key);
                    return new MyCustomGrainStorage(key, options);
                });
        });
    }
}
```

### Provider Registration Pattern

The Orleans runtime resolves a storage provider from the `IServiceProvider` when a grain is created:
- If a storage provider name is specified via `[PersistentState(stateName, storageName)]`, a **named** instance of `IGrainStorage` is resolved.
- Named services are registered using `AddSingletonNamedService<IGrainStorage>(name, factory)`.

---

## Serialization

### Two Kinds of Serialization in Orleans

1. **Grain call serialization**: Used to serialize objects passed to and from grains (method arguments and return values). Uses the high-performance Orleans.Serialization framework.
2. **Grain storage serialization**: Used to serialize objects to and from storage systems. Uses `IGrainStorageSerializer`.

### Grain Storage Serialization

Grain storage serialization is handled separately from grain call serialization.

#### IGrainStorageSerializer Interface

Starting with Orleans 7.0, there is a general-purpose grain state serializer interface:

```csharp
public interface IGrainStorageSerializer
{
    BinaryData Serialize<T>(T input);
    T Deserialize<T>(BinaryData input);
}
```

All supported storage providers implement a pattern involving setting the `GrainStorageSerializer` property on the provider's options:

- `AzureBlobStorageOptions.GrainStorageSerializer`
- `AzureTableStorageOptions.GrainStorageSerializer`
- `DynamoDBStorageOptions.GrainStorageSerializer`
- `AdoNetGrainStorageOptions.GrainStorageSerializer`

#### Default Serializer

**Grain storage serialization currently defaults to `Newtonsoft.Json`** (not the high-performance Orleans native serializer). This is by design because:
- JSON is human-readable and debuggable.
- JSON is version-tolerant -- you can add/remove fields without breaking existing stored data.
- JSON is interoperable across different systems.

#### Configuring a Custom Storage Serializer

```csharp
siloBuilder.AddAzureBlobGrainStorage(
    "MyGrainStorage",
    (OptionsBuilder<AzureBlobStorageOptions> optionsBuilder) =>
    {
        optionsBuilder.Configure<MyCustomSerializer>(
            (options, serializer) => options.GrainStorageSerializer = serializer);
    });
```

#### ADO.NET Serialization Options (Legacy)

ADO.NET in Orleans 3.x supported three formats:
- Binary (default, most compact, opaque)
- JSON (recommended, human-readable)
- XML

Built-in serializer classes:
- `OrleansStorageDefaultJsonSerializer` / `OrleansStorageDefaultJsonDeserializer`
- `OrleansStorageDefaultXmlSerializer` / `OrleansStorageDefaultXmlDeserializer`
- `OrleansStorageDefaultBinarySerializer` / `OrleansStorageDefaultBinaryDeserializer`

### Grain Call Serialization (Orleans.Serialization)

The native Orleans serialization framework is used for grain-to-grain communication:

```csharp
[GenerateSerializer]
public class Employee
{
    [Id(0)]
    public string Name { get; set; }
}
```

Key properties:
- High-performance binary format.
- Supports polymorphism, object identity, cyclic graphs.
- Version-tolerant: supports adding/removing members, numeric widening/narrowing, type renaming.
- Requires explicit `[GenerateSerializer]` and `[Id(N)]` attributes.
- Supports `[Alias("my-type")]` for type renaming resilience.
- Supports inheritance with per-level IDs:

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
    [Id(0)]  // ID scoped to this level, not clash with Publication's Id(0)
    public string ISBN { get; set; }
}
```

- Supports `record` types with implicit IDs for primary constructor parameters.
- Supports external serialization formats: MessagePack, System.Text.Json, Newtonsoft.Json, Google.Protobuf via surrogates.

---

## Grain State Versioning

### Recommendations

Orleans strongly recommends using **JSON or another version-tolerant serialization format** for grain storage. This is the default.

### Version Tolerance Rules

When evolving grain state types:

**Compound types (class & struct):**
- You CAN add or remove fields at any point in an inheritance hierarchy.
- You CANNOT change the inheritance hierarchy of an object.
- You CANNOT change field types (with numeric exceptions below).
- You CANNOT change field IDs.
- Field IDs must be unique per inheritance level, but can be reused between base and subclass.

**Numeric types:**
- You CAN widen numeric types (e.g., `int` to `long`).
- You CAN narrow numeric types (but may throw at runtime on overflow).
- You CANNOT change the signedness (e.g., `int` to `uint` is invalid).

### ADO.NET Built-in Versioning

The ADO.NET provider has special built-in capability to:
1. Change storage data from one format to another during round-trip (e.g., JSON to binary).
2. Shape the type to be saved or read from storage in arbitrary ways (allowing state version evolution).
3. Make versioning decisions based on grain ID, grain type, or payload data.

### Best Practices

- Use `[Alias("my-type")]` on state types to survive type renaming.
- Use `[Id(N)]` with stable numeric IDs.
- Ensure already-stored data can still be loaded after code changes.
- For Orleans native serialization: start IDs at 0 for each inheritance level.
- Do not change `record` to `class` or vice versa.

---

## Event Sourcing and Journaled Grains

Orleans supports event sourcing through the `JournaledGrain` system. This is an alternative to the standard `IPersistentState` model.

### JournaledGrain<TGrainState, TEventBase>

```csharp
public class JournaledGrain<TGrainState, TEventBase> : Grain
    where TGrainState : class, new()
{
    // Current confirmed state
    TGrainState State { get; }

    // Version = total number of confirmed events
    int Version { get; }

    // Raise a new event (does not wait for storage write)
    protected void RaiseEvent(TEventBase event);

    // Raise multiple events atomically
    protected void RaiseEvents(IEnumerable<TEventBase> events);

    // Wait for all raised events to be confirmed (written to storage)
    protected Task ConfirmEvents();

    // Raise an event conditionally (returns false if conflict)
    protected Task<bool> RaiseConditionalEvent(TEventBase event);

    // Retrieve a segment of the confirmed event sequence
    protected Task<IReadOnlyList<TEventBase>> RetrieveConfirmedEvents(
        int fromVersion, int toVersion);
}
```

Shorthand for POCO events:
```csharp
public class JournaledGrain<TGrainState> : JournaledGrain<TGrainState, object>
```

### State Transitions

The grain state is updated automatically when events are raised. You define how state transitions occur in one of two ways:

**Option A: Apply methods on the state class (recommended)**

The runtime picks the closest match by runtime event type:

```csharp
public class BankAccountState
{
    public decimal Balance { get; set; }

    public void Apply(DepositEvent @event)
    {
        Balance += @event.Amount;
    }

    public void Apply(WithdrawalEvent @event)
    {
        Balance -= @event.Amount;
    }
}
```

**Option B: Override TransitionState on the grain**

```csharp
protected override void TransitionState(
    BankAccountState state, object @event)
{
    switch (@event)
    {
        case DepositEvent deposit:
            state.Balance += deposit.Amount;
            break;
        case WithdrawalEvent withdrawal:
            state.Balance -= withdrawal.Amount;
            break;
    }
}
```

### Key Semantics

- `State` always equals the initial state with all confirmed events applied.
- `Version` always equals the total number of confirmed events.
- `RaiseEvent()` initiates a write but does NOT wait for it. Follow with `await ConfirmEvents()` when durability matters.
- Multiple `RaiseEvent()` calls before `ConfirmEvents()` cause multiple storage writes. Use `RaiseEvents(IEnumerable)` for atomicity.
- Transition methods must be **deterministic** and have **no side effects** other than modifying the state object.
- The initial state is the default constructor of `TGrainState` at version 0.

### Example: Event-Sourced Bank Account

```csharp
// Events
public class DepositEvent
{
    public decimal Amount { get; set; }
    public string Description { get; set; }
}

public class WithdrawalEvent
{
    public decimal Amount { get; set; }
    public string Description { get; set; }
}

// State
public class BankAccountState
{
    public decimal Balance { get; set; }
    public List<string> Transactions { get; set; } = new();

    public void Apply(DepositEvent @event)
    {
        Balance += @event.Amount;
        Transactions.Add($"+{@event.Amount}: {@event.Description}");
    }

    public void Apply(WithdrawalEvent @event)
    {
        Balance -= @event.Amount;
        Transactions.Add($"-{@event.Amount}: {@event.Description}");
    }
}

// Grain
[StorageProvider(ProviderName = "OrleansStorage")]
[LogConsistencyProvider(ProviderName = "LogStorage")]
public class BankAccountGrain :
    JournaledGrain<BankAccountState>, IBankAccountGrain
{
    public Task<decimal> GetBalance() => Task.FromResult(State.Balance);

    public async Task Deposit(decimal amount, string description)
    {
        RaiseEvent(new DepositEvent
        {
            Amount = amount,
            Description = description
        });
        await ConfirmEvents();
    }

    public async Task<bool> Withdraw(decimal amount, string description)
    {
        if (State.Balance < amount)
            return false;

        RaiseEvent(new WithdrawalEvent
        {
            Amount = amount,
            Description = description
        });
        await ConfirmEvents();
        return true;
    }
}
```

### Configuration

```csharp
// Add log-consistency providers
builder.AddLogStorageBasedLogConsistencyProvider("LogStorage");
builder.AddStateStorageBasedLogConsistencyProvider("StateStorage");
builder.AddCustomStorageBasedLogConsistencyProvider("CustomStorage");
```

Grain class attributes:

```csharp
[LogConsistencyProvider(ProviderName = "LogStorage")]
[StorageProvider(ProviderName = "OrleansLocalStorage")]
public class MyJournaledGrain : JournaledGrain<MyState, MyEventBase>, IMyGrain
{
    // ...
}
```

---

## Log-Consistency Providers

Orleans includes three built-in log-consistency providers in the `Microsoft.Orleans.EventSourcing` package.

### 1. StateStorage (Orleans.EventSourcing.StateStorage.LogConsistencyProvider)

- Stores **grain state snapshots** using a standard storage provider.
- The stored object contains the grain state plus metadata (version number and a deduplication tag).
- The **entire grain state is read/written every time** storage is accessed.
- NOT suitable for very large grain states.
- Does NOT support `RetrieveConfirmedEvents()` (events are not persisted, only the state snapshot).
- Best for: grains where you want event-sourcing semantics in code but only need to persist the latest state.

### 2. LogStorage (Orleans.EventSourcing.LogStorage.LogConsistencyProvider)

- Stores the **complete event sequence as a single object** using a standard storage provider.
- The stored object contains a `List<EventType>` and metadata.
- The **entire event sequence is read/written every time** storage is accessed.
- Supports `RetrieveConfirmedEvents()` -- all events are always available and kept in memory.
- **NOT suitable for production use** unless event sequences are guaranteed to remain short.
- Best for: samples, testing, and illustrating event sourcing semantics.

### 3. CustomStorage (Orleans.EventSourcing.CustomStorage.LogConsistencyProvider)

- Allows you to **plug in your own storage interface**.
- The grain must implement `ICustomStorageInterface<TState, TEvent>`:

```csharp
public interface ICustomStorageInterface<StateType, EventType>
{
    /// <summary>
    /// Read state from storage. Returns (version, state).
    /// If nothing stored, return (0, default(StateType)).
    /// </summary>
    Task<KeyValuePair<int, StateType>> ReadStateFromStorage();

    /// <summary>
    /// Apply updates to storage. Return false if expectedVersion
    /// does not match actual version (ETag-like check).
    /// </summary>
    Task<bool> ApplyUpdatesToStorage(
        IReadOnlyList<EventType> updates,
        int expectedVersion);
}
```

- No assumptions about whether stored data consists of state snapshots or events -- you control that.
- Does NOT support `RetrieveConfirmedEvents()` (you implement your own retrieval).
- If `ApplyUpdatesToStorage` returns `false`, the consistency provider retries with refreshed state.
- If `ApplyUpdatesToStorage` throws, events may be duplicated on retry -- you are responsible for handling this.

### Provider Selection Guide

| Feature | StateStorage | LogStorage | CustomStorage |
|---|---|---|---|
| What is stored | State snapshot | Full event log | You decide |
| RetrieveConfirmedEvents | No | Yes | No (DIY) |
| Production suitable | Yes | No (grows unbounded) | Yes |
| External storage control | Via standard provider | Via standard provider | Full control |
| State size concern | Yes (full read/write) | Yes (full read/write) | You control |

---

## Key Design Patterns for Python Implementation

### Pattern 1: Storage Provider Abstraction

The core abstraction is the `IGrainStorage` interface with three operations: read, write, clear. In Python:

```python
from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Optional
from dataclasses import dataclass

T = TypeVar('T')

@dataclass
class GrainState(Generic[T]):
    state: T
    etag: Optional[str] = None
    record_exists: bool = False

class GrainStorage(ABC):
    """Abstract base for all grain storage providers."""

    @abstractmethod
    async def read_state(
        self, state_name: str, grain_id: str, grain_state: GrainState[T]
    ) -> None:
        """Read grain state from storage. Populates grain_state in-place."""
        ...

    @abstractmethod
    async def write_state(
        self, state_name: str, grain_id: str, grain_state: GrainState[T]
    ) -> None:
        """Write grain state to storage. Updates etag on grain_state."""
        ...

    @abstractmethod
    async def clear_state(
        self, state_name: str, grain_id: str, grain_state: GrainState[T]
    ) -> None:
        """Clear grain state from storage."""
        ...
```

### Pattern 2: Persistent State Wrapper

```python
class PersistentState(Generic[T]):
    """Wraps grain state with persistence operations."""

    def __init__(
        self,
        storage: GrainStorage,
        state_name: str,
        grain_id: str,
        state_factory: type[T]
    ):
        self._storage = storage
        self._state_name = state_name
        self._grain_id = grain_id
        self._grain_state = GrainState(state=state_factory())

    @property
    def state(self) -> T:
        return self._grain_state.state

    @state.setter
    def state(self, value: T):
        self._grain_state.state = value

    @property
    def etag(self) -> Optional[str]:
        return self._grain_state.etag

    @property
    def record_exists(self) -> bool:
        return self._grain_state.record_exists

    async def read_state(self) -> None:
        await self._storage.read_state(
            self._state_name, self._grain_id, self._grain_state)

    async def write_state(self) -> None:
        await self._storage.write_state(
            self._state_name, self._grain_id, self._grain_state)

    async def clear_state(self) -> None:
        await self._storage.clear_state(
            self._state_name, self._grain_id, self._grain_state)
```

### Pattern 3: Optimistic Concurrency Exception

```python
class InconsistentStateError(Exception):
    """Raised when an ETag constraint violation is detected."""

    def __init__(
        self,
        message: str,
        stored_etag: Optional[str],
        current_etag: Optional[str],
        storage_exception: Optional[Exception] = None
    ):
        super().__init__(message)
        self.stored_etag = stored_etag
        self.current_etag = current_etag
        self.storage_exception = storage_exception
```

### Pattern 4: Named Storage Provider Registry

```python
class StorageProviderRegistry:
    """Registry of named storage providers."""

    def __init__(self):
        self._providers: dict[str, GrainStorage] = {}

    def register(self, name: str, provider: GrainStorage) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> GrainStorage:
        if name not in self._providers:
            raise KeyError(f"Storage provider '{name}' not registered")
        return self._providers[name]
```

### Pattern 5: Declarative Persistence (Decorator-based)

```python
import functools

def persistent_state(state_name: str, storage_name: str):
    """Decorator to mark a grain attribute as persistent state."""
    def decorator(cls):
        if not hasattr(cls, '_persistent_states'):
            cls._persistent_states = []
        cls._persistent_states.append({
            'state_name': state_name,
            'storage_name': storage_name,
        })
        return cls
    return decorator
```

### Pattern 6: Grain Storage Serializer

```python
from abc import ABC, abstractmethod

class GrainStorageSerializer(ABC):
    """Serializer for grain state persistence."""

    @abstractmethod
    def serialize(self, obj: object) -> bytes:
        ...

    @abstractmethod
    def deserialize(self, data: bytes, target_type: type[T]) -> T:
        ...


class JsonGrainStorageSerializer(GrainStorageSerializer):
    """Default JSON serializer for grain storage."""

    def serialize(self, obj: object) -> bytes:
        import json
        return json.dumps(obj, default=self._default_encoder).encode('utf-8')

    def deserialize(self, data: bytes, target_type: type[T]) -> T:
        import json
        raw = json.loads(data.decode('utf-8'))
        # Reconstruct target_type from dict
        return target_type(**raw) if isinstance(raw, dict) else raw
```

### Pattern 7: Journaled Grain (Event Sourcing)

```python
from abc import ABC
from typing import Generic, TypeVar, List

TState = TypeVar('TState')
TEvent = TypeVar('TEvent')

class JournaledGrain(ABC, Generic[TState, TEvent]):
    """Base class for event-sourced grains."""

    def __init__(self, state_factory: type[TState]):
        self._state: TState = state_factory()
        self._version: int = 0
        self._pending_events: List[TEvent] = []

    @property
    def state(self) -> TState:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    def raise_event(self, event: TEvent) -> None:
        """Raise a new event. Does not persist immediately."""
        self._apply_event(event)
        self._pending_events.append(event)

    def raise_events(self, events: List[TEvent]) -> None:
        """Raise multiple events atomically."""
        for event in events:
            self._apply_event(event)
            self._pending_events.append(event)

    async def confirm_events(self) -> None:
        """Persist all pending events to storage."""
        if self._pending_events:
            await self._write_events(self._pending_events, self._version)
            self._pending_events.clear()

    def _apply_event(self, event: TEvent) -> None:
        """Apply an event to the state using convention-based dispatch."""
        # Look for an 'apply' method on the state that accepts this event type
        method_name = 'apply'
        apply_method = getattr(self._state, method_name, None)
        if apply_method and callable(apply_method):
            apply_method(event)
        self._version += 1

    @abstractmethod
    async def _write_events(
        self, events: List[TEvent], expected_version: int
    ) -> None:
        """Write events to the log-consistency provider."""
        ...
```

### Pattern 8: In-Memory Storage Provider

```python
import copy
import uuid
from typing import Any

class MemoryGrainStorage(GrainStorage):
    """In-memory grain storage for development and testing."""

    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}

    def _make_key(self, state_name: str, grain_id: str) -> str:
        return f"{grain_id}/{state_name}"

    async def read_state(
        self, state_name: str, grain_id: str, grain_state: GrainState
    ) -> None:
        key = self._make_key(state_name, grain_id)
        if key in self._store:
            stored = self._store[key]
            grain_state.state = copy.deepcopy(stored['state'])
            grain_state.etag = stored['etag']
            grain_state.record_exists = True
        else:
            grain_state.etag = None
            grain_state.record_exists = False

    async def write_state(
        self, state_name: str, grain_id: str, grain_state: GrainState
    ) -> None:
        key = self._make_key(state_name, grain_id)
        if key in self._store:
            stored_etag = self._store[key]['etag']
            if grain_state.etag is not None and grain_state.etag != stored_etag:
                raise InconsistentStateError(
                    "ETag mismatch",
                    stored_etag=stored_etag,
                    current_etag=grain_state.etag
                )
        new_etag = str(uuid.uuid4())
        self._store[key] = {
            'state': copy.deepcopy(grain_state.state),
            'etag': new_etag
        }
        grain_state.etag = new_etag
        grain_state.record_exists = True

    async def clear_state(
        self, state_name: str, grain_id: str, grain_state: GrainState
    ) -> None:
        key = self._make_key(state_name, grain_id)
        if key in self._store:
            del self._store[key]
        grain_state.etag = None
        grain_state.record_exists = False
```

---

## References

- [Orleans Grain Persistence (Microsoft Learn)](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-persistence/)
- [Azure Storage Grain Persistence](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-persistence/azure-storage)
- [ADO.NET Grain Persistence](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-persistence/relational-storage)
- [Amazon DynamoDB Grain Persistence](https://learn.microsoft.com/en-us/dotnet/orleans/grains/grain-persistence/dynamodb-storage)
- [Orleans Event Sourcing Overview](https://learn.microsoft.com/en-us/dotnet/orleans/grains/event-sourcing/)
- [JournaledGrain Basics](https://learn.microsoft.com/en-us/dotnet/orleans/grains/event-sourcing/journaledgrain-basics)
- [Log-Consistency Providers](https://learn.microsoft.com/en-us/dotnet/orleans/grains/event-sourcing/log-consistency-providers)
- [Event Sourcing Configuration](https://learn.microsoft.com/en-us/dotnet/orleans/grains/event-sourcing/event-sourcing-configuration)
- [Orleans Serialization](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization)
- [Orleans GitHub Repository](https://github.com/dotnet/orleans)
