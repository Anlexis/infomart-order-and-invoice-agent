# Design Specification — CMN-C2-286 Infomart Order & Invoice Agent

## Position in the framework

| Aspect | Value |
|---|---|
| Agent class | `InfomartOrderInvoiceAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 — a fixed multi-step pipeline for one job to be done |
| Pattern | Tool-calling: classify intent → extract the order/invoice target → build an Infomart API request → answer → confirm. No retrieval, no autonomous loop. |

Three-layer separation:

- **State** — a flat TypedDict, `State(AgentState)`. Graph checkpoints are
  msgpack-serialised, so model objects and nested containers are not storable;
  dict and list payloads travel as JSON strings.
- **Node** — direct framework inheritance; a node overrides `execute(self, state) -> dict`
  and nothing else.
- **Graph** — composition. The outer graph registers its three domain slots and
  does not override `add_edges()`; the inner graph owns its own linear topology.

## Architecture

### Outer graph (`src/graph/graph.py`)

| Node | Responsibility | Reads | Writes | Required trust |
|------|---------------|-------|--------|----------------|
| initialize | framework setup (schema, session, trust) | user_input | session / trust fields | framework default |
| pre_process | validate the caller contract; screen both caller channels; serialise the request for the inner graph | user_input, input_context | validated_input, target_hint, caller_fields | **VERIFIED_EXTERNAL** — the single external gate |
| main | run the inner Infomart workflow subgraph | validated_input, caller_fields | result, intent, target_id, record_id, record_ref, record_label, record_count, amount_total, confirmation, infomart_payload | caller context forwarded unchanged |
| post_process | shape, gate and release `formatted_output` | inner-result fields | formatted_output | ANONYMOUS |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default |

### Inner workflow (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` for a fully custom linear topology. **Every
inner node declares `required_trust_level = TrustLevel.ANONYMOUS`** — the
caller's `InvocationContext` is forwarded into the subgraph unchanged, so the
single external trust gate stays on the backbone `pre_process`. Declaring an
inner node INTERNAL would deny a legitimate external caller before the read runs.

| Inner node | Step | Responsibility | Writes |
|---|---|---|---|
| validate_input | 1 | empty / non-request guard; instruction-override refusal; deterministic flag-and-redact of email and token-shaped strings before logging | validated_input, target_hint, redaction_flags |
| classify_intent | 2 | deterministic keyword classification → lookup_order / list_invoices / lookup_supplier; low confidence falls back to lookup_order, the narrowest read | intent |
| infer_infomart_fields | 3 | extract the order number / supplier code, the record label and any "Key: value" filters; assemble the request parameters; an unresolved code is left empty, never invented | target_id, record_label, infomart_payload |
| call_infomart_api | 4 | answer from the caller's own records when supplied, otherwise read orders / received invoices / suppliers through `InfomartClient`; compute the billed aggregate | record_id, record_ref, target_id, record_label, record_count, amount_total |
| confirm | 5 | render the confirmation the caller reads back | confirmation, result |

### Data flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              |  error / retry  ^
Inner (inside main):
        START -> validate_input -> classify_intent -> infer_infomart_fields
              -> call_infomart_api -> confirm -> END
```

The request text travels as a JSON string: `pre_process` serialises
`{"text", "target_hint"}` into `validated_input`, the graph node hands that to
the subgraph, and `validate_input` parses it back.

The **validated caller records travel separately**, over the caller-context
bridge in `src/graph/context_bridge.py`. Two facts force that design:

1. the framework invokes a subgraph as `subgraph.invoke(user_input, session_id, ctx)`
   and does **not** forward `input_context`, so an inner read of
   `state["input_context"]` would always see `{}`;
2. `validated_input` is masked at every node boundary, so caller values riding
   inside it could be rewritten between hops.

`InfomartWorkflowGraphNode.extract_input()` stashes the validated contract on a
`ContextVar` (per thread/task, so concurrent invocations cannot see each
other's), and the inner graph's `_extra_initial_state()` seeds it back into the
inner state. Only validated fields ever cross.

### State (`src/schemas/state.py`)

Every domain field is `NotRequired[...]` — absent until its producer writes it.
Dict and list payloads are stored as JSON strings via the module helpers
`to_json` / `from_json`.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| target_hint | NotRequired[str] | caller-supplied order number / supplier code; never inferred | pre_process |
| target_id | NotRequired[str] | the resolved order number / supplier code | infer_infomart_fields |
| caller_fields | NotRequired[Optional[str]] | JSON — the validated caller contract, handed across the graph boundary | pre_process |
| redaction_flags | NotRequired[Optional[str]] | JSON list of the pattern categories redacted before logging | validate_input |
| record_label | NotRequired[str] | display label (supplier trading name, order status) | infer_infomart_fields / call_infomart_api |
| infomart_payload | NotRequired[Optional[str]] | JSON — the assembled request parameters | infer_infomart_fields |
| infomart_config | NotRequired[Optional[str]] | JSON — the runtime `infomart:` section, injected by the inner graph | inner graph |
| record_id | NotRequired[str] | order / invoice / supplier identifier | call_infomart_api |
| record_ref | NotRequired[str] | reference (`infomart://orders/<no>`) | call_infomart_api |
| record_count | NotRequired[int] | how many records a list read covered — structural, never monetary | call_infomart_api |
| amount_total | NotRequired[str] | the billed aggregate, already rendered on the external grid | call_infomart_api |
| confirmation | NotRequired[str] | the human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `input_context` and `formatted_output`
are inherited from `AgentState` and are not re-declared.

State constraints, all satisfied: flat TypedDict only; no credentials in state
(the integration token is read through `ctx.secrets`); the invocation context is
read via `InvocationContext.from_state(state)`, never stored.

## Runtime configuration

Two files, two consumers:

| File | Content | Read by |
|---|---|---|
| `config/agent.yaml` | the flat registry manifest: identity, entry point, entry trust, and the compile-time `requires` gates | the platform registry |
| `config/config.yaml` | runtime parameters: `max_retry`, `timeout_s`, and the `infomart:` integration section | the graph, as `config=` |

Nodes take **no constructor arguments** — configuration never rides on a node
instance. `src/graph/graph.py::load_runtime_config()` reads `config/config.yaml`;
the standalone entry point constructs the agent with it, exactly as the registry
does. `InfomartWorkflowGraphNode._parent_config()` forwards the `infomart`
section and the runtime values to the inner graph under `config["configurable"]`,
and `_extra_initial_state()` injects the integration settings into state as the
JSON `infomart_config` field, where `CallInfomartApiNode` reads them. The request
timeout is passed to the transport on **every** call, so the declared value is
the one a live HTTP client applies rather than a setting nothing consumes.

`requires.secrets` is deliberately empty. A declared secret is a compile-time
gate — the platform refuses to start the agent when it is not provisioned. This
template reads the token with `ctx.secrets.get()` and degrades to the
network-free transport when it is absent, so declaring it would turn a working
default into a start-up failure.

## Caller-data contract

`POST /invoke` accepts `input_context` alongside `input`. Every field is
optional; each one is validated in `PreProcessNode`, which owns the contract.

| Field | Shape | Bound |
|---|---|---|
| `order_no` / `supplier_code` / `target_hint` | string | identifier alphabet `[A-Za-z0-9][A-Za-z0-9_-]{0,31}` |
| `order` | `{order_no, supplier_code, status, amount}` | identifiers as above; `status` a bounded display label; `amount` finite and within range |
| `invoices` | list of `{invoice_no, supplier_code, status, amount}` | as above, at most 200 entries |
| `supplier` | `{supplier_code, name}` | identifier + bounded display label |

Rules applied to all of them:

- **Types are not coerced.** `str(float("nan"))` is `"nan"`, which passes an
  identifier shape check, so a number where a string is required is refused
  rather than stringified.
- **Every number is finite and bounded.** NaN and ±Infinity survive `float()`
  and arrive intact through raw JSON, and every comparison against NaN is
  False — a silent fail-open on exactly the figures this agent totals. They are
  refused, along with out-of-range magnitudes. Failure is closed, naming the
  field.
- **Every string that renders into the response is locked to an inert
  alphabet.** Free text there would be caller-controlled output. That also makes
  the channel structurally immune to a second hazard: the first backbone node
  returns `input_context` verbatim in its own result, and the framework scans
  every result value for credential patterns, so a credential anywhere in the
  channel would fail the run at node one. The entry point screens for that
  explicitly and refuses with a message naming the field.
- **A refusal names the field, never the value**, and an unrecognised field
  *name* is masked rather than echoed.
- **Absent fields are simply absent** — the workflow falls back to what it can
  read out of the request text and to the built-in transport.
- **Size caps**: 256 KB on the serialised channel at the adapter; 200 invoice
  entries; per-field length limits.

## Instruction-override screen

Directives aimed at the model — role reassignment, system-prompt manipulation,
chat-template control tokens (`<|im_start|>`, `[INST]`, `<<SYS>>`) — are refused,
fail closed, on **both** caller channels: the request text, and every decoded
string in `input_context`, keys included, at any depth. The walk runs on the
parsed mapping, so escaping cannot smuggle a phrase past it.

The screen is the template's own (`src/services/security.py`), enforced inside
`execute()`. Delegating it to the platform input gate would leave the template
failing open wherever that gate is absent or configured off, and would leave the
structured channel — which the platform gate does not cover — unscreened
entirely.

Two passes run, and neither is redundant. The **raw** pass catches control
tokens, which share the shape of HTML markup: the sanitizer would otherwise
delete `<|im_start|>` and forward the bare directive after it as ordinary text,
turning a detectable token attack into an undetectable one. The **stripped**
pass catches a directive spliced with markup (`ig<b>nore all rules`) that only
re-assembles into a phrase once the markup is gone.

Every alternative is anchored on a full directive phrase or a control token, so
ordinary trading prose passes: "ignore the cancelled line", "forget the previous
delivery date", "acting as the purchasing manager". A screen that refuses real
work is the failure mode a user actually meets.

## External output schema

`post_process` is the external boundary. Three layers run in order:

1. **Credential scan** — API keys, bearer tokens and JWT-shaped strings anywhere
   in the caller-facing output withhold the response entirely. It runs *before*
   the numeric layer: the precision grammar treats any standalone three-letter
   uppercase word as a currency marker, so snapping first could rewrite the
   digits of a labelled sequence and hide it from this scan.
2. **Precision grid** — billed figures are reported as aggregates rounded to the
   nearest 1,000; individual invoice amounts are never reported. The pipeline
   renders on that grid and this gate independently enforces it, with an audit
   event on every correction.
3. **Re-scan** — no rewrite performed inside the gate may produce an unscanned
   surface.

The gate walks the whole output structure, not just its top-level strings:
`infomart_payload` is a nested mapping and `error` is a list.

**Monetary values are identified by form and by currency context, never by
magnitude**: comma-grouped numbers and unformatted runs of five or more digits,
plus any short number sitting next to a currency marker — a three-letter
uppercase code or a symbol, before or after the value, attached or separated by
any horizontal whitespace, signed or unsigned. A decimal fraction is absorbed
into the same token, so a ratio is never rewritten and an amount never snaps
with its fraction dangling.

**Identifiers are protected structurally.** This agent's output is dense with
them, and a B2B order number is very often purely numeric — it has no letters to
protect it. Two mechanisms hold:

- the renderer never emits a bare identifier. Every one appears behind a `#` or
  inside an `infomart://` reference;
- the grammar is wrapped in single-character guards over this template's own
  render alphabet (`A-Za-z0-9_-`, plus `/` `:` `#`), so a monetary token can
  neither begin nor end inside an identifier.

Where the two are still lexically identical — `JPY-9999` (a signed amount) and
`SKF-6205` (a part number) have the same shape — the gate consults the closed
ISO 4217 code list for that one decision. Everywhere else any three-letter
uppercase word counts as a marker, because there a false snap fails safe.

**Every error return clears every output-bearing state field.** Returning an
error is not enough: `AgentBaseGraph.get_output()` projects
`formatted_output or result` with **no status check**, so an error return that
merely reported the failure would ship the un-gated answer inside the error
envelope. Both error returns — a gate violation, and a pre-existing
inner-workflow error arriving from the subgraph — overwrite `result`,
`confirmation`, `record_label`, `amount_total`, `infomart_payload`, `intent`, and
the record evidence `record_id`, `record_ref`, `target_id`.

**No error envelope names a record.** `record_id` / `record_ref` are this agent's
lookup evidence — the gate *refuses* a SUCCESS that lacks them — so returning
them under an ERROR status would tell a caller being informed of failure that a
record was nonetheless resolved, and which order or supplier it was. Both error
envelopes are built by one `_error_envelope()` helper:

```
{"reason": "<closed-set code>", "confirmation": "", "error": [<node-authored reasons>]}
```

`reason` is one of `infomart_workflow_failed` / `output_withheld_by_gate` and is
a constant, which keeps the mapping **truthy** — a falsy `formatted_output` would
re-open the `or result` projection this containment exists to prevent. The
`post_process_error_contained` audit event carries the reason code and the error
**count** only.

**Error reasons are closed-set labels.** `error_log` rides the caller-facing
envelope under `error`, so a reason that interpolates the record or upstream text
puts the same disclosure back by another key. `call_infomart_api` reports the
record **type** for a not-found (never the order number or supplier code), the
**HTTP status** for an API error (a live tenant's error body is unbounded
third-party text that can quote the record it refused), and the **exception
type** for a transport failure (a transport error string can carry the request
URL and the order number).

## Audit events

Every node emits one domain event on its success path, carrying presence signals
only — never request text, record content, or credentials. Framework lifecycle
events are emitted by the framework and are not duplicated.

| Node | Event |
|------|-------|
| pre_process | `pre_process_complete` (or `pre_process_validation_failed` on a refusal) |
| validate_input | `validate_input_complete` (or `validate_input_refused`) |
| classify_intent | `classify_intent_complete` |
| infer_infomart_fields | `infer_infomart_fields_complete` |
| call_infomart_api | `call_infomart_api_complete` |
| confirm | `confirm_complete` |
| post_process | `post_process_complete` (or `post_process_blocked`) |

## Language model

The pipeline is fully deterministic. Intent classification is a keyword
heuristic and field inference is regex and line-structure extraction, so the
template runs and is testable without a model. No model client is constructed
anywhere and no prompt is read, so there is no dead configuration. Model-backed
synthesis — richer classification, free-text-to-filter mapping, natural-language
record summaries — is additive and needs no change to the graph shape.

## Integration limitation

`src/services/infomart_client.py` ships a **deterministic, network-free stub**
as its default transport. It returns the documented Infomart response shapes so
the pipeline is runnable and testable without a live tenant or an HTTP client
package; it does not perform a live call, and it says so. To go live, inject a
real transport at construction: the method contracts and parameter shapes follow
the Infomart BtoB Platform API surface, so no business logic changes. Every
operation is a read — the client exposes no write methods.

When the caller supplies its own order, invoice or supplier records on the
structured channel, the pipeline reports on those instead, and the aggregate is
real arithmetic over real figures.

## Import isolation

- The template imports `framework/` and `shared/` only.
- `src/services/infomart_client.py` and `src/services/security.py` import
  nothing from the framework — pure stdlib, which is what keeps the service
  layer independently testable.

## Design decisions

| Decision | Alternative | Chosen | Rationale |
|----------|-------------|--------|-----------|
| Base class | AutonomousBaseGraph | AgentBaseGraph | a fixed multi-step pipeline, not an autonomous loop |
| Composition | flat single main node | graph node + inner subgraph | five domain steps belong in their own graph |
| Caller records | request text only | validated structured channel + text | the answer is computed from the caller's real figures, not only from a built-in transport |
| Caller-record transport | inside `validated_input` | the context bridge | `validated_input` is masked at every node boundary; the bridge is not |
| Language model | model client from the start | deterministic, model optional | the template runs and tests with no model and reads no dead prompt config |
| Integration | live HTTP call | injectable transport, network-free default | never fake a live call; document the limitation; going live is an injection |
| Node configuration | constructor-argument injection | no-arg nodes + runtime config forwarding | nodes are no-arg by contract; config stays in one place |
| Lookup target | infer the code freely from prose | explicit code only, unresolved left empty | never read the wrong partner's records |
| Default intent | list_invoices | lookup_order | low confidence should fall back to the narrowest single-record read |
| Operation scope | read + write | read only | a commerce write path needs its own review |
| Declared secrets | declare `INFOMART_TOKEN` | declare none | a declared secret is a compile-time gate; this template degrades gracefully without one |
