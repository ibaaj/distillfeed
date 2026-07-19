from datetime import UTC, datetime

from rss_reader.weather import _CACHE, get_weather


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "current": {"temperature_2m": 19.4, "weather_code": 61, "rain": 0.2, "time": "2026-07-12T12:00"},
            "hourly": {"precipitation_probability": [20, 45, 70, 30, 10, 5]},
            "daily": {
                "time": ["2026-07-12", "2026-07-13", "2026-07-14"],
                "weather_code": [61, 2, 0], "temperature_2m_min": [14, 13, 15],
                "temperature_2m_max": [20, 22, 25], "precipitation_probability_max": [80, 30, 5],
                "rain_sum": [4.2, 0.3, 0],
            },
        }


def test_weather_returns_current_paris_rain(configured, monkeypatch):
    _CACHE.update(expires_at=datetime.min.replace(tzinfo=UTC), data=None, key=None)
    monkeypatch.setattr("rss_reader.weather.requests.get", lambda *args, **kwargs: FakeResponse())
    result = get_weather(configured)
    assert result["location"] == "Paris"
    assert result["temperature"] == 19
    assert result["language"] == "English"
    assert result["condition"] == "Rain"
    assert result["rain_probability"] == 70
    assert len(result["days"]) == 3
    assert result["days"][0]["rain_probability"] == 80


def test_weather_can_be_french(configured, monkeypatch):
    _CACHE.update(expires_at=datetime.min.replace(tzinfo=UTC), data=None, key=None)
    configured.data["weather"]["language"] = "French"
    monkeypatch.setattr("rss_reader.weather.requests.get", lambda *args, **kwargs: FakeResponse())
    result = get_weather(configured)
    assert result["language"] == "French"
    assert result["condition"] == "Pluie"
    assert result["days"][1]["condition"] == "Nuageux"
