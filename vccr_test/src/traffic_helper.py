"""ANWB traffic helper utilities.

Contains a single function `get_traffic_info` which fetches and returns
traffic incident data for a given road from the ANWB API.
"""
from typing import Dict

import requests

__all__ = ["get_traffic_info"]


def get_traffic_info(road_number: str) -> Dict:
    """
    Fetch traffic incidents for a specific road from the ANWB API.

    Args:
        road_number: Road identifier (e.g. "A1" or "a1"). Comparison is
                     case-insensitive and whitespace is ignored.

    Returns:
        dict: The matching road dictionary from the API payload if found
              (contains nested `segments`, `jams`, `roadworks`, etc.).
              If no matching road is found, returns
              {"message": "No traffic incidents found for <road>."}.
              On network or parse failures, returns
              {"error": "Failed to fetch traffic data", "details": "..."}.

    Notes:
        - Uses a standard `User-Agent` header to avoid simple blocks by the API.
        - This function is suitable to be exposed as an external tool for
          LLM agents (clear docstring and simple, serializable return values).
    """
    url = "https://api.anwb.nl/routing/v1/incidents/incidents-desktop"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    timeout_seconds = 10

    try:
        resp = requests.get(url, headers=headers, timeout=timeout_seconds)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": "Failed to fetch traffic data", "details": str(e)}

    try:
        payload = resp.json()
    except ValueError as e:
        return {"error": "Failed to parse JSON response", "details": str(e)}

    roads = payload.get("roads", []) if isinstance(payload, dict) else []
    target = road_number.strip().lower()

    for road in roads:
        if not isinstance(road, dict):
            continue
        road_key = road.get("road")
        if road_key and str(road_key).strip().lower() == target:
            return road

    return {"message": f"No traffic incidents found for {road_number}."}
