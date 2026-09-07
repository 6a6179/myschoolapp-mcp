"""Regression check for term routing; no school session or network needed."""

import os
from unittest.mock import Mock, patch

import pytest

from myschoolapp_mcp import server


def test_duration_routing():
    terms = [
        {"OfferingType": 3, "DurationId": 303, "CurrentInd": 1},
        {"OfferingType": 1, "DurationId": 100, "CurrentInd": 0},
        {"OfferingType": 1, "DurationId": 101, "CurrentInd": 1},
        {"OfferingType": 2, "DurationId": 202, "CurrentInd": 1},
        {"OfferingType": 4, "DurationId": 404, "CurrentInd": 1},
        {"OfferingType": 9, "DurationId": 909, "CurrentInd": 1},
        {"OfferingType": 11, "DurationId": 0, "CurrentInd": 1},
    ]

    def request(method, path, **kwargs):
        assert method == "GET"
        body = terms if path.endswith("StudentGroupTermList/") else []
        return {"status": 200, "body": body}

    client = Mock()
    client.request.side_effect = request
    cases = [
        (server.classes, {}, "durationList", 101),
        (server.gradebook, {}, "durationList", 101),
        (server.group_membership, {"kind": "advisory"}, "durationId", 303),
        (server.group_membership, {"kind": "athletic"}, "durationList", 909),
        (server.group_membership, {"kind": "dorm"}, "durationId", 404),
        (server.group_membership, {"kind": "activity"}, "durationId", 202),
        (server.group_membership, {"kind": "community"}, "durationId", 0),
    ]
    with (
        patch.object(server, "_get_client", return_value=client),
        patch.dict(os.environ, {"MSA_STUDENT_ID": "test-student"}),
    ):
        for tool, args, duration_param, expected in cases:
            for override in (None, 0, 999):
                client.request.reset_mock()
                kwargs = args if override is None else {**args, "duration_id": override}
                assert tool(**kwargs)["status"] == 200
                params = client.request.call_args.kwargs["params"]
                assert params[duration_param] == (override or expected)
                auto_resolved = not override and args.get("kind") != "community"
                assert client.request.call_count == (2 if auto_resolved else 1)

        client.request.side_effect = None
        for response in [
            {"status": 200, "body": []},
            {"status": 200, "body": terms[:2]},
            {"status": 200, "body": "not a term list"},
            {"status": 403, "error": True, "body": terms},
        ]:
            client.request.return_value = response
            with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
                server._resolve_duration_id()

        client.request.reset_mock()
        client.request.return_value = {"status": 200, "body": terms[:3]}
        with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
            server.group_membership("athletic")
        assert client.request.call_count == 1
