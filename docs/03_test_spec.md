# Test Specification — CMN-C2-286 Infomart Order & Invoice Agent

## Strategy

- **Unit** (`tests/unit/`) — one module per node, plus the service layer, the
  manifest and runtime config, and the inner graph.
- **Boundary** (`tests/proof_of_boundary/`) — the compiled outer graph, the
  public HTTP path, import isolation, state safety, and server boot.
  `tests/integration/` is an empty package; the end-to-end coverage lives in the
  boundary suite, which drives the real graph rather than a partial one.

Every operation is a read — order status, received invoices, supplier records —
so there are no write paths to test.

The Infomart read is exercised through the deterministic, network-free transport
(the default) and through injected fake clients. No test performs a live call.

### Conventions the suite depends on

- **Nodes are invoked as `node(state)`**, so the framework's whole pipeline runs
  (trust gate → input gate → `execute()` → output gate). State builders set
  `caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value` for `PreProcessNode`
  (the single external gate) and `TrustLevel.ANONYMOUS.value` elsewhere.
  Two deliberate exceptions call `execute()` directly:
  - `CallInfomartApiNode.execute(state, config=...)`, whose second argument
    `__call__` cannot forward;
  - the instruction-override and caller-contract refusal tests, whose whole
    point is that the template's own guarantee holds with **no** framework
    wrapper in front of it. An assertion that "the framework refused it" only
    holds where that gate is active.
- **Assertions are behavioural.** A refusal is asserted as an error status with
  nothing carried forward, never as a gate's wording — wording is not a contract
  and changes between framework versions.
- **Invoke surface**: `result["output"] / status / trace_id / correlation_id /
  node_history`. Identifiers may be masked, so record evidence is asserted by
  presence rather than by raw repr.
- **Framework behaviours the suite encodes**: `__call__` short-circuits on an
  incoming errored state (`execute()` is skipped, the error passes through); the
  framework input gate rewrites Title-Case bigrams, emails and digit groups in
  `user_input` / `validated_input` to `[MASKED]` before `execute()` sees them, so
  positive payloads are PII-free and the intentional-PII test asserts the
  `[MASKED]` path.
- Domain audit events are muted per module by an autouse fixture patching
  `src.nodes.<module>.emit_trace_event`; the emit-spy tests assert on the event
  payload, never on a whole-call repr.

## Unit tests (`tests/unit/`)

| TC-ID | File | Focus | Expected |
|-------|------|-------|----------|
| U-01 | test_trust_gate.py | the single external gate; every inner node ANONYMOUS | an under-trusted caller is denied with an error dict, execute-only keys absent; VERIFIED_EXTERNAL passes; trust declarations pinned per node |
| U-02 | test_pre_process_node.py | the caller contract: identifier bounds, finite+bounded amounts, inert labels, entry caps, undeclared keys dropped, refusals that name the field; the instruction-override screen on both channels | out-of-contract input fails closed naming the field, never echoing the value; an unrecognised field name is masked; attack forms refused, ordinary trading prose unaffected |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; the framework `[MASKED]` path; token flag-and-redact; the instruction-override screen | email masked before `execute()`; `secret_*` redacted with `redaction_flags=["token"]`; control tokens and directive phrases refused; retail-style prose passes |
| U-04 | test_classify_intent_node.py | intent = lookup_order / list_invoices / lookup_supplier | correct intent per keyword, most-specific vocabulary first; no signal defaults to lookup_order with a non-fatal note; empty input errors |
| U-05 | test_infer_infomart_fields_node.py | code resolution precedence (text > hint > caller record > filter line); the quoted label; "Key: value" filters; the assembled parameters | payload per intent; an unresolved code stays empty; a label or filter term outside the display alphabet is dropped, not escaped; filters capped |
| U-06 | test_call_infomart_api_node.py | answers from the caller's records; the integration path; the runtime timeout; credential posture | a caller invoice list yields a real count and a gridded aggregate; absent records fall back to the integration; a live transport without a token refuses; a malformed `timeout_s` fails closed; the audit event names the source |
| U-07 | test_confirm_node.py | the confirmation per intent; the aggregate line; identifier rendering | the right verb; `total=` only when there is one; a bare identifier is never emitted — it renders behind `#`; missing evidence errors |
| U-08 | test_post_process_node.py | the whole output boundary | the success shape; a blocked response clears every output-bearing field; a credential nested in the payload is caught, and so is the top-level control; the grid snaps every enumerated leak form and leaves every structural token byte-identical; the field classification is asserted, not assumed |
| U-09 | test_infomart_client.py | the read client | URLs, headers, params; a non-2xx raises with the joined errors; the deterministic stub shapes; the configured timeout reaches the transport; a non-finite or out-of-range timeout is refused |
| U-10 | test_config.py | the manifest and the runtime config | identity and entry point at the manifest root, no nested `agent:` block, no declared compile-time secret; the runtime file carries the integration section and the parameters; a declared runtime value provably reaches the framework (an out-of-contract `max_retry` fails at compile) |
| U-11 | test_domain_workflow_graph.py | the inner graph | identity; the integration section and the runtime timeout injected as JSON; the caller bridge read back inside `invoke()`; the route callable annotated with the graph's own State; the output contract; a direct inner invoke |
| U-12 | test_framework_compliance_tc06_tc07.py | framework compliance | the default input and output gates cannot be replaced by a subclass |

## Boundary tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | File | Expected |
|-------|----------|------|----------|
| PB-2/PB-5 | State serialization | test_state_safety.py | no model objects and no credential-shaped fields in `state.py` |
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-internal imports |
| PB-6 | Backbone invoke order + external trust | test_pb_invoke_order.py | the payload is byte-equal to `deploy/invoke_payload.json`; an external caller runs the exact five-node backbone in order and gets record evidence; an under-trusted caller is denied at `pre_process`; blank input errors rather than crashing |
| PB-7 | Human-in-the-loop interrupt *(conditional)* | test_pb7_hitl_interrupt_propagation.py | auto-waived — this template declares no human-review step; the stub bodies are real assertion failures, so enabling one without implementing this fails loudly |
| PB (contract) | The public path, end to end | test_pb_caller_contract_e2e.py | through the real HTTP entry with Bearer auth: caller records reach the inner graph and produce a real answer; the invoice aggregate is computed and gridded; validation and instruction-override refusals surface as errors with no output; a credential on the structured channel is refused at the adapter naming the field; an oversized channel is refused; the response carries the schema note and no credential-shaped string; a purely numeric order number is not rewritten; both arms of the backbone's conditional edge are reached |
| PB (containment) | Error-path containment at the output boundary | test_error_envelope_no_record_evidence.py | the existing-ERROR branch of `post_process` returns a **truthy** record-free envelope (`reason` / `confirmation` / `error`) — a falsy value would re-open the framework's `formatted_output or result` projection — carrying no order number, `infomart://` reference, supplier name, label or amount; the delta CLEARS every output-bearing field (`result`, `confirmation`, `record_label`, `amount_total`, `infomart_payload`, `intent`, `record_id`, `record_ref`, `target_id`); the reasons `call_infomart_api` writes carry closed-set labels only (record type, HTTP status, exception type — never the record id, the upstream body or the request URL); a success-path control proves the containment did not empty the clean path |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` constructs, compiles and provisions without raising; `/invoke` and `/health` are exposed |

> Audit emission is covered inside the unit suite by the emit-spy tests. A live
> external call is exercised at first invoke against a real tenant, not here —
> the default transport is the documented network-free one.

## Execution summary

- Runner: the repository's own suite under the framework wheel the pipeline installs.
- Total: 276 — 274 passed, 2 skipped (the human-review boundary, auto-waived).
