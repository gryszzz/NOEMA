from noema.research_guard import ResearchEvidence, audit_research


def test_research_guard_rejects_in_sample_edge() -> None:
    verdict = audit_research(
        ResearchEvidence(
            resolved=500,
            estimated_edge=0.05,
            edge_standard_error=0.01,
            strategies_tested=10,
            out_of_sample=False,
            lookahead_free=True,
        )
    )
    assert verdict.credible is False
    assert "not out-of-sample" in verdict.reasons


def test_multiple_testing_can_destroy_edge() -> None:
    verdict = audit_research(
        ResearchEvidence(
            resolved=1000,
            estimated_edge=0.02,
            edge_standard_error=0.008,
            strategies_tested=100,
            out_of_sample=True,
            lookahead_free=True,
        )
    )
    assert verdict.conservative_edge < verdict.z_score
    assert verdict.credible is False
