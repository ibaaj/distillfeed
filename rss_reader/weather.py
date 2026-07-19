from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from .config import Config

LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"expires_at": datetime.min.replace(tzinfo=UTC), "data": None, "key": None}


def _condition(code: int, language: str) -> str:
    english = language == "English"
    if code == 0:
        return "Clear" if english else "Ciel clair"
    if code in {1, 2, 3}:
        return "Cloudy" if english else "Nuageux"
    if code in {45, 48}:
        return "Fog" if english else "Brouillard"
    if code in {51, 53, 55, 56, 57}:
        return "Drizzle" if english else "Bruine"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "Rain" if english else "Pluie"
    if code in {71, 73, 75, 77, 85, 86}:
        return "Snow" if english else "Neige"
    if code in {95, 96, 99}:
        return "Thunderstorm" if english else "Orage"
    return "Variable weather" if english else "Temps variable"


def get_weather(config: Config) -> dict[str, Any]:
    options = config.section("weather")
    if not options.get("enabled"):
        return {"enabled": False}
    key = (
        float(options["latitude"]), float(options["longitude"]), str(options["timezone"]),
        str(options["location_name"]), str(options["language"]),
    )
    now = datetime.now(UTC)
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["data"] and _CACHE["expires_at"] > now:
            return dict(_CACHE["data"])
        try:
            with requests.get(
                ENDPOINT,
                params={
                    "latitude": key[0], "longitude": key[1], "timezone": key[2],
                    "current": "temperature_2m,weather_code,precipitation,rain",
                    "hourly": "precipitation_probability", "forecast_hours": 12,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,rain_sum",
                    "forecast_days": 3,
                },
                timeout=8,
            ) as response:
                response.raise_for_status()
                payload = response.json()
            current = payload.get("current", {})
            probabilities = payload.get("hourly", {}).get("precipitation_probability", [])
            rain_probability = max((int(value or 0) for value in probabilities[:6]), default=0)
            weather_code = int(current.get("weather_code", -1))
            daily = payload.get("daily", {})
            days = []
            for index, day in enumerate(daily.get("time", [])[:3]):
                days.append(
                    {
                        "date": day,
                        "condition": _condition(int(daily.get("weather_code", [-1] * 3)[index]), key[4]),
                        "minimum": round(float(daily.get("temperature_2m_min", [0] * 3)[index])),
                        "maximum": round(float(daily.get("temperature_2m_max", [0] * 3)[index])),
                        "rain_probability": int(daily.get("precipitation_probability_max", [0] * 3)[index] or 0),
                        "rain_sum": float(daily.get("rain_sum", [0] * 3)[index] or 0),
                    }
                )
            data = {
                "enabled": True,
                "location": key[3],
                "temperature": round(float(current.get("temperature_2m", 0))),
                "condition": _condition(weather_code, key[4]),
                "rain_probability": rain_probability,
                "rain_now": float(current.get("rain", 0) or 0),
                "observed_at": current.get("time"),
                "days": days,
                "source_url": "https://open-meteo.com/",
                "language": key[4],
            }
            _CACHE.update(
                key=key, data=data,
                expires_at=now + timedelta(minutes=max(1, int(options["refresh_minutes"]))),
            )
            return dict(data)
        except Exception as exc:
            LOGGER.warning("Weather update failed: %s", exc)
            if _CACHE["key"] == key and _CACHE["data"]:
                stale = dict(_CACHE["data"])
                stale["stale"] = True
                return stale
            error = "Weather unavailable" if key[4] == "English" else "Météo indisponible"
            return {"enabled": True, "location": key[3], "error": error, "language": key[4]}
