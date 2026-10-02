# CMN-C2-286 - Unit tests: the manifest and the runtime config.
#
# config/agent.yaml is the flat registry manifest (static identity + the
# compile-time gates); config/config.yaml carries the runtime parameters the
# graph is constructed with. The two files are read by different consumers, so
# both are pinned here - including the fact that nothing declares an `agent:`
# block any more, which is the shape a stale reader would look for.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-286"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["enabled"] is True


def test_manifest_entry_point():
    # One dotted import path, read at the manifest ROOT.
    assert _manifest()["class"] == "src.graph.graph.InfomartOrderInvoiceAgent"


def test_manifest_keys_are_flat():
    """No nested `agent:` block: a reader looking for one would find nothing."""
    data = _manifest()
    assert "agent" not in data
    assert "config" not in data


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_compile_time_secret():
    """The integration token is optional, so it is NOT declared as required.

    A declared secret is a compile-time gate: the platform refuses to start the
    agent when it is not provisioned. This template reads the token with
    ctx.secrets.get() and degrades to the network-free transport when it is
    absent, so declaring it would turn a working default into a start-up
    failure.
    """
    data = _manifest()
    assert data["requires"]["secrets"] == []
    assert data["requires"]["extras"] == []


def test_runtime_config_integration_section():
    # Forwarded to the inner graph by InfomartWorkflowGraphNode._parent_config().
    assert _runtime()["infomart"]["base_url"] == "https://api.infomart.co.jp/v1"


def test_runtime_config_parameters():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], int)


def test_load_runtime_config_reads_the_live_file():
    """The loader returns what the file declares, not a hard-coded default."""
    from src.graph.graph import load_runtime_config

    runtime = load_runtime_config()
    assert runtime["max_retry"] == _runtime()["max_retry"]
    assert runtime["timeout_s"] == _runtime()["timeout_s"]
    assert runtime["infomart"] == _runtime()["infomart"]


def test_declared_runtime_values_reach_the_framework():
    """A declared value that nothing consumes is the failure worth pinning.

    The framework validates max_retry when the graph compiles, so constructing
    the agent with an out-of-contract value must fail. If the runtime config
    were not passed to the graph, this would compile happily on a default the
    template never declared.
    """
    from framework.errors import ConfigError

    from src.graph.graph import InfomartOrderInvoiceAgent, load_runtime_config

    runtime = load_runtime_config()
    runtime["max_retry"] = 99  # above the framework ceiling
    with pytest.raises(ConfigError):
        InfomartOrderInvoiceAgent(config=runtime).compile()


def test_declared_runtime_values_compile_as_declared():
    from src.graph.graph import InfomartOrderInvoiceAgent, load_runtime_config

    agent = InfomartOrderInvoiceAgent(config=load_runtime_config())
    agent.compile()
    assert agent.config["max_retry"] == _runtime()["max_retry"]
