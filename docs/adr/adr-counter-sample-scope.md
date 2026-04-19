# Counter-sample scope

- Status: accepted
- Date: 2026-04-19
- Decision driver: keep the sample app small enough to be read end-to-end in one sitting

## Context

`src/counter_app` demonstrates pyleans. Reviewers use it to
understand what the library delivers. That only works if the
sample stays small enough to read in one sitting — once it grows
production-sample features (config files, metrics, auth, Docker
compose) the sample starts competing with the framework docs for
the reader's attention, and neither wins.

## Decision

The sample is a **demonstration**, not a **template**. The
following are explicitly out of scope:

- **No config-file support.** CLI flags are enough to run the
  demo. A config file would add another format to document and
  another code path to test without clarifying any cluster
  behaviour.
- **No metrics export or dashboard.** The framework's logging is
  the observability layer the sample demonstrates. Metrics come
  from the operator's infrastructure, not the sample.
- **No authentication on the gateway.** TLS on the built-in
  transport is a Phase 4 concern (see
  [`adr-cluster-transport`](adr-cluster-transport.md)). Adding
  auth to the sample today would demonstrate a moving target.
- **No Docker Compose / Kubernetes recipe.** Deployment wrappers
  belong in a separate `examples/` tree post-PoC. Their lifecycle
  does not match the framework's.
- **No grain types beyond `CounterGrain` and `AnswerGrain`.** Two
  types are enough to demonstrate activation routing plus
  request-reply; more types add surface area that distracts from
  the cluster semantics the sample is here to show.

## Consequences

- A reviewer can understand the sample by reading
  `src/counter_app/` + `src/counter_client/` plus the README — no
  detour into configuration-loader code, no Kubernetes YAML, no
  TLS certificate handling.
- Users who want a production-shaped starter will need to
  assemble one themselves or wait for the `examples/` tree.
  Explicitly acceptable: the sample's audience is framework
  evaluators, not production operators.
- When Phase 4 ships TLS and metrics, this ADR is the gate that
  forces us to update the sample — avoiding silent feature drift.
