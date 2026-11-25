"""An in-memory stand-in for the parts of core's HTTP API that learn's
retrospective reads.

Its lineage rule is a COPY of core's, not a check of it. Core's
``Store._populate_lineage`` records, for each id in a verdict's
``parent_ids``, the parent and every ancestor the parent already has, with no
check that the id is a verdict; ``descendants_of`` then returns the verdicts
recorded under an ancestor id, nearest first. This class does the same. If
core changes, this copy does not notice: core pins the behaviour itself in
``test_descendants_of_an_assessment_id_returns_the_chain_that_names_it``
(opensrm-brms), and the three-tier harness is to assert the real seam
(opensrm-mtxr). When opensrm-7t3q's typed relations land, this file,
``fetch_incident_chain`` and those two change together.
"""

from __future__ import annotations

from nthlayer_common.api_client import APIResult


def ok(data=None, status_code: int = 200) -> APIResult:
    return APIResult(ok=True, status_code=status_code, data=data if data is not None else {})


def failed(status_code: int = 503) -> APIResult:
    return APIResult(ok=False, status_code=status_code, error="unavailable")


class FakeCore:
    """Verdicts with lineage, assessments and cases, behind the client's
    method names. ``fail`` names methods that answer 503 until removed."""

    def __init__(self) -> None:
        self.verdicts: dict[str, dict] = {}
        # descendant id -> {ancestor id: hop distance}
        self._ancestors: dict[str, dict[str, int]] = {}
        self.assessments: list[dict] = []
        self.cases: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.fail: set[str] = set()
        self.manifests: list[dict] = []

    # -- writes ---------------------------------------------------------

    def put_verdict(self, verdict: dict) -> dict:
        vid = verdict["id"]
        self.verdicts[vid] = verdict
        mine = self._ancestors.setdefault(vid, {})
        for parent in verdict.get("parent_ids") or []:
            mine[parent] = min(mine.get(parent, 1), 1)
            for ancestor, hops in self._ancestors.get(parent, {}).items():
                mine[ancestor] = min(mine.get(ancestor, hops + 1), hops + 1)
        return verdict

    def put_case(self, case_id: str, underlying_verdict: str, state: str = "pending") -> None:
        """A case as respond creates it (``_create_case_for_incident``) and
        core stores it: ``state`` is core's, pending until leased or resolved."""
        self.cases[case_id] = {
            "id": case_id, "kind": "incident", "underlying_verdict": underlying_verdict,
            "state": state,
        }

    # -- the client surface learn uses ------------------------------------

    async def get_assessments(self, kind=None, **_kwargs):
        if "get_assessments" in self.fail:
            return failed()
        return ok([a for a in self.assessments if kind is None or a.get("kind") == kind])

    async def get_verdict(self, verdict_id):
        if "get_verdict" in self.fail:
            return failed()
        if verdict_id not in self.verdicts:
            return APIResult(ok=False, status_code=404, error="not_found")
        return ok(self.verdicts[verdict_id])

    async def get_descendants(self, verdict_id):
        if "get_descendants" in self.fail:
            return failed()
        found = [
            (ancestors[verdict_id], vid)
            for vid, ancestors in self._ancestors.items()
            if verdict_id in ancestors
        ]
        return ok([self.verdicts[vid] for _hops, vid in sorted(found)])

    async def get_cases(self, *, state=None, priority=None, service=None, limit=100):
        if "get_cases" in self.fail:
            return failed()
        cases = [c for c in self.cases.values() if state is None or c["state"] == state]
        return ok(cases[:limit])

    async def get_manifests(self):
        return ok(list(self.manifests))

    async def submit_assessment(self, envelope):
        if "submit_assessment" in self.fail:
            return failed()
        self.submitted.append(envelope.get("data", envelope))
        return ok(status_code=201)

    @property
    def retrospectives(self) -> list[dict]:
        return [a for a in self.submitted if a.get("kind") == "retrospective"]
