from noema.agent_runtime import _trench_forward_enrichment_limit
from noema.trench_config import TrenchCollectorConfig


def test_forward_sampler_runs_one_rpc_enrichment_but_never_exceeds_owner_limit():
    assert _trench_forward_enrichment_limit(TrenchCollectorConfig(enrichment_limit=0)) == 0
    assert _trench_forward_enrichment_limit(TrenchCollectorConfig(enrichment_limit=1)) == 1
    assert _trench_forward_enrichment_limit(TrenchCollectorConfig(enrichment_limit=5)) == 1
