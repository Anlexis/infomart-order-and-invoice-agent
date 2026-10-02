# CMN-C2-286 — Infomart Order & Invoice Agent

> **Category**: Cat 2 (a fixed multi-step pipeline for one job to be done)
> **Industry**: CMN (industry-agnostic)

## Overview

Answers a plain-language question about orders and invoices on a food-service B2B trading
platform.

Give it a sentence like *"look up the status of order o-2001"* or *"list the received invoices for
supplier s-77"* and it works out which of three read operations you meant, pulls the order number,
supplier code and any filter terms out of the wording, calls the matching Infomart BtoB Platform
REST API endpoint, and hands back a confirmation carrying the record reference.

Three operations are supported, all of them reads: looking up an order's status, listing received
invoices, and retrieving a trading-partner record. The tool surface is read-only by design — orders
and invoices are commercial records, and a wrong write is unrecoverable where a wrong read is not.
The agent never invents an identifier: an unresolved order number or supplier code is reported as
an error rather than guessed at, because reading the wrong trading partner's records is the failure
mode worth designing against.

Intent classification and field extraction are deterministic (keyword and pattern based), so the
pipeline runs and is testable without a language model. Out of the box it ships with a network-free
transport that returns the documented Infomart response shapes, which makes the template runnable
end to end before you connect a real tenant; a caller may also pass order, invoice or supplier
records it already holds on the request's structured channel, and the pipeline reports on those —
including a real total over the caller's own invoices.

Everything the agent reports passes an output boundary that withholds the response if a
credential-shaped string appears anywhere in it, and that rounds every billed figure to the nearest
1,000 — the agent reports aggregates, never an individual invoice's exact amount.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design specification and test specification
```

`docs/02_design.md` describes the graph, the state contract, the caller-data contract and the
output boundary; `docs/03_test_spec.md` maps every test to the behaviour it pins.

## Customising

1. Adjust `config/config.yaml` for your own environment and policies.
2. Point it at your own Infomart tenant and inject a live transport in
   `src/services/infomart_client.py`.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
