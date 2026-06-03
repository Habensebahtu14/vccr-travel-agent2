import os
import sys
import requests
from unittest.mock import Mock, patch

# Zorg dat de repository root in sys.path staat zodat imports werken tijdens tests
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vccr_test.src.traffic_helper import get_traffic_info


def test_get_traffic_info_found():
    payload = {"roads": [{"road": "A1", "segments": [], "jams": [], "roadworks": []}]}
    mock_resp = Mock()
    mock_resp.raise_for_status = Mock()
    mock_resp.json.return_value = payload

    with patch("vccr_test.src.traffic_helper.requests.get", return_value=mock_resp):
        res = get_traffic_info("a1")
        assert isinstance(res, dict)
        assert res.get("road") == "A1"


def test_get_traffic_info_not_found():
    payload = {"roads": [{"road": "A2"}]}
    mock_resp = Mock()
    mock_resp.raise_for_status = Mock()
    mock_resp.json.return_value = payload

    with patch("vccr_test.src.traffic_helper.requests.get", return_value=mock_resp):
        res = get_traffic_info("A1")
        assert res == {"message": "No traffic incidents found for A1."}


def test_get_traffic_info_request_exception():
    with patch("vccr_test.src.traffic_helper.requests.get", side_effect=requests.RequestException("boom")):
        res = get_traffic_info("A1")
        assert isinstance(res, dict)
        assert res.get("error") == "Failed to fetch traffic data"
        assert "boom" in res.get("details")
