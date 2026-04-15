# Hexagonal Architecture (Ports and Adapters)

## Origin and History

Hexagonal Architecture was conceived by **Alistair Cockburn** in 2005. He originally published it under the name **"Ports and Adapters"** pattern.

Cockburn developed the pattern in response to a recurring problem he observed: **business logic becoming entangled with user interface code and infrastructure concerns**. He noticed that applications frequently suffered from infiltration of business logic into the UI layer (or vice versa), making them difficult to test, difficult to adapt to new interfaces, and fragile in the face of changing infrastructure.

The hexagonal shape in the diagrams is not significant in itself -- it was chosen simply to provide enough "sides" to represent multiple ports, breaking away from the traditional layered (top-to-bottom) diagrams that implied a fixed number of layers.

## The Core Idea

The fundamental intent, as Cockburn stated it:

> Allow an application to equally be driven by users, programs, automated test or batch scripts, and to be developed and tested in isolation from its eventual run-time devices and databases.

The architecture draws a hard boundary around the application's business logic (the "inside") and treats everything external (the "outside") -- whether it is a user interface, a database, a message queue, or an external service -- as interchangeable peripherals connected through well-defined interfaces.

The two key abstractions are:

- **Ports**: Technology-agnostic interfaces that define how the application communicates with the outside world. A port is a contract -- it says *what* interactions are possible without specifying *how* they happen.
- **Adapters**: Concrete implementations that translate between the port's interface and a specific external technology. Each port can have multiple adapters.

## How It Separates Business Logic from Infrastructure

Traditional layered architectures (presentation -> business logic -> data access) create an implicit dependency direction: the business layer depends on the data access layer. Hexagonal Architecture inverts this relationship using the **Dependency Inversion Principle**:

1. The **application core** defines the ports (interfaces) it needs.
2. The **infrastructure** provides adapters that implement those ports.
3. Dependencies always point **inward** -- infrastructure depends on the core, never the reverse.

This means the business logic has zero knowledge of whether it is being driven by a REST API, a CLI, a test harness, or a scheduled job. It also has no knowledge of whether data is stored in PostgreSQL, MongoDB, an in-memory dictionary, or a flat file.

## Key Components

### 1. Domain / Application Core

The innermost part of the architecture. It contains:

- **Domain model**: Entities, value objects, aggregates -- the business concepts and rules.
- **Application services** (use cases): Orchestrate domain objects to fulfill specific business operations. They depend only on port interfaces, never on concrete infrastructure.
- **Domain services**: Encapsulate domain logic that does not naturally belong to a single entity.

The core has **no dependencies on frameworks, databases, or external systems**. It is pure business logic.

### 2. Ports

Ports are interfaces (or abstract classes) that sit at the boundary of the core. They define how the outside world can interact with the application and what the application needs from the outside world. They represent dependencies the application requires. 

### 3. Adapters

Adapters are concrete implementations that connect ports to the external world. They translate external input into calls on the application coreand implement the interfaces defined by driven ports using specific technologies. Examples: a SQL repository implementation, an HTTP client for an external API, an SMTP email sender, or an in-memory fake for testing.

### Structural Summary

```
[Driving Adapter] --> [Driving Port] --> [Application Core] --> [Driven Port] --> [Driven Adapter]
   (REST API)         (interface)        (business logic)      (interface)       (PostgreSQL repo)
   (CLI)                                                                         (In-memory fake)
   (Test harness)                                                                (HTTP client)
```

## Practical Example

Consider an order management system:

```
Driving Adapters:
  - FastAPI REST controller
  - CLI tool for batch processing
  - Unit test calling the service directly

Driving Port (interface):
  - OrderService.place_order(items, customer_id) -> OrderConfirmation

Application Core:
  - Order entity (validates business rules)
  - OrderService implementation (orchestrates the workflow)

Driven Ports (interfaces):
  - OrderRepository.save(order) -> None
  - OrderRepository.find_by_id(id) -> Order
  - PaymentGateway.charge(amount, method) -> PaymentResult
  - NotificationSender.send(recipient, message) -> None

Driven Adapters:
  - PostgresOrderRepository (implements OrderRepository)
  - StripePaymentGateway (implements PaymentGateway)
  - SmtpNotificationSender (implements NotificationSender)
  - InMemoryOrderRepository (for tests)
  - FakePaymentGateway (for tests)
```

## Benefits

1. **Testability**: The core can be tested without any infrastructure. Driven adapters are replaced with in-memory fakes or mocks. Driving adapters are bypassed entirely -- tests call the application services directly through the driving ports.

2. **Technology independence**: Swapping a database, switching from REST to gRPC, or replacing an email provider requires only writing a new adapter. The core remains untouched.

3. **Deferred decisions**: Infrastructure choices (which database? which message broker?) can be deferred. Development can start with simple in-memory adapters and real infrastructure can be plugged in later.

4. **Symmetry**: The architecture treats all external actors uniformly. A user clicking a button and an automated test are both just driving adapters. A real database and a test double are both just driven adapters. This symmetry simplifies reasoning about the system.

5. **Focus on business value**: Developers working on the core deal only with business concepts. The domain model is not polluted with persistence annotations, serialization logic, or framework dependencies.

6. **Multiple entry points**: The same application logic can be exposed through multiple interfaces (web API, CLI, event consumer) without duplication.

## Trade-offs

1. **Increased indirection**: Every interaction crosses at least one port boundary. This adds interfaces, adapter classes, and mapping code. For small or simple applications, this overhead may not be justified.

2. **Mapping overhead**: Data must often be translated between representations -- domain objects, persistence models, API request/response objects. This mapping code can become tedious, though it also provides valuable decoupling and other methods also do simliar mappings, e.g. ORMapper.

### Related Patterns

- **Domain-Driven Design (Eric Evans, 2003)**: DDD's emphasis on a rich domain model and bounded contexts pairs naturally with Hexagonal Architecture. The hexagonal core is an excellent home for a DDD domain model.
- **Dependency Injection**: The mechanism by which adapters are wired to ports at runtime. DI containers or manual constructor injection are the typical implementation strategies.
- **Repository Pattern**: A specific instance of a driven port/adapter pair focused on data persistence.
- **Anti-Corruption Layer (DDD)**: Conceptually similar to an adapter -- it translates between an external system's model and the application's domain model.

## Summary

Hexagonal Architecture is a structural pattern that places business logic at the center of the application and pushes all infrastructure concerns to the periphery, connected through explicit port interfaces and interchangeable adapter implementations. Its core value proposition is **protecting domain logic from technological change** while enabling **superior testability** and **flexibility**. The pattern is most valuable in applications with meaningful business logic and multiple integration points, where the upfront cost of defining ports and adapters pays off through long-term maintainability and adaptability.
