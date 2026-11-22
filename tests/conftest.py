"""Offline harness for garmin_fetch.py.

garmin_fetch.py opens an InfluxDB client and writes a DemoPoint at import time, and logs in to Garmin
under __main__. The loader below replaces both clients with in-memory fakes before the module runs,
blocks every socket, and swaps the module's `datetime` class for one driven by a simulated clock.

GF_MODULE_PATH chooses the garmin_fetch.py under test (default: this checkout), so the same tests
can be run against the unmodified upstream file.
"""
import datetime as _dt
import importlib.util
import os
import socket
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(os.environ.get("GF_MODULE_PATH", REPO / "src" / "garmin_grafana" / "garmin_fetch.py"))
UTC = _dt.timezone.utc
GARMIN_TS = "%Y-%m-%dT%H:%M:%S.%f"


# ---------------------------------------------------------------- no network, ever
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# ---------------------------------------------------------------- simulated clock
class SimClock:
    def __init__(self, now_utc):
        self.now = now_utc


def fake_datetime_class(clock):
    class FakeDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            t = clock.now.astimezone(tz) if tz is not None else clock.now.astimezone().replace(tzinfo=None)
            return cls.combine(t.date(), t.timetz()) if tz is not None else cls.combine(t.date(), t.time())

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            return cls.now(UTC).replace(tzinfo=None)
    return FakeDatetime


# ---------------------------------------------------------------- fake InfluxDB
class InfluxRecorder:
    def __init__(self, last_hr_time="2020-01-01T00:00:00Z"):
        self.points = []
        self.last_hr_time = last_hr_time

    def client_class(self):
        recorder = self

        class FakeInfluxClient:
            def __init__(self, *args, **kwargs):
                pass

            def switch_database(self, name):
                pass

            def write_points(self, points):
                recorder.points.extend(points)

            def write(self, record):
                recorder.points.extend(record)

            def query(self, *args, **kwargs):
                return types.SimpleNamespace(get_points=lambda: [{"time": recorder.last_hr_time}])

            def delete_series(self, **kwargs):
                pass
        return FakeInfluxClient

    def measurement(self, name):
        return [p for p in self.points if p["measurement"] != "DemoPoint" and p["measurement"] == name]


# ---------------------------------------------------------------- fake Garmin
class FakeGarminBase:
    """Every endpoint garmin_fetch calls, answering 'no data'. Subclasses override what a test needs."""
    display_name = "fake-user"

    def __init__(self):
        self.calls = []

    def login(self, *args, **kwargs):
        pass

    def _log(self, name, *args):
        self.calls.append((name,) + args)

    def get_stats(self, date_str):
        self._log("get_stats", date_str)
        return {"calendarDate": date_str, "wellnessStartTimeGmt": None, "includesWellnessData": False}

    def get_device_last_used(self):
        return {"lastUsedDeviceName": "FakeWatch", "userDeviceId": None, "imageUrl": None, "lastUsedDeviceUploadTime": 0}

    def get_last_activity(self):
        return {"startTimeLocal": "2020-01-01 12:00:00", "startTimeGMT": "2020-01-01 12:00:00"}

    def get_sleep_data(self, date_str):
        return {"dailySleepDTO": {"sleepEndTimestampGMT": None}}

    def get_steps_data(self, date_str):
        return []

    def get_heart_rates(self, date_str):
        self._log("get_heart_rates", date_str)
        return {}

    def get_stress_data(self, date_str):
        return {}

    def get_respiration_data(self, date_str):
        return {}

    def get_hrv_data(self, date_str):
        return {}

    def get_fitnessage_data(self, date_str):
        return {}

    def get_max_metrics(self, date_str):
        return []

    def get_race_predictions(self, startdate=None, enddate=None, _type=None):
        return []

    def get_weigh_ins(self, start, end):
        return {"dailyWeightSummaries": []}

    def get_activities_by_date(self, start, end):
        return []

    def get_lifestyle_logging_data(self, date_str):
        return {}

    def connectapi(self, endpoint, **kwargs):
        return []

    def get_training_status(self, date_str):
        return {}

    def get_training_readiness(self, date_str):
        return []

    def get_hill_score(self, date_str):
        return {}

    def get_endurance_score(self, date_str):
        return {}

    def get_blood_pressure(self, start, end):
        return {}

    def get_hydration_data(self, date_str):
        return {}

    def get_device_solar_data(self, device_id, date_str):
        return {}


def stats_json(date_str, start_offset_min, end_offset_min, complete=True, last_sync_utc=None):
    """A get_stats response shaped like Garmin's: local midnight bounds and their GMT instants.

    A completed day ends at next local midnight; a day in progress ends at the last sync
    (both observed in public recorded responses)."""
    day = _dt.datetime.strptime(date_str, "%Y-%m-%d")
    start_gmt = day - _dt.timedelta(minutes=start_offset_min)
    if complete:
        end_local = day + _dt.timedelta(days=1)
        end_gmt = end_local - _dt.timedelta(minutes=end_offset_min)
    else:
        end_gmt = last_sync_utc.replace(tzinfo=None)
        end_local = end_gmt + _dt.timedelta(minutes=end_offset_min)
    return {
        "calendarDate": date_str,
        "includesWellnessData": True,
        "wellnessStartTimeGmt": start_gmt.strftime(GARMIN_TS)[:-5],
        "wellnessStartTimeLocal": day.strftime(GARMIN_TS)[:-5],
        "wellnessEndTimeGmt": end_gmt.strftime(GARMIN_TS)[:-5],
        "wellnessEndTimeLocal": end_local.strftime(GARMIN_TS)[:-5],
        "totalSteps": 1234,
        "restingHeartRate": 55,
    }


# ---------------------------------------------------------------- loader
BASE_ENV = {
    "INFLUXDB_HOST": "influx.invalid",
    "INFLUXDB_PORT": "1",
    "INFLUXDB_VERSION": "1",
    "RATE_LIMIT_CALLS_SECONDS": "0",
    "LOG_LEVEL": "WARNING",
}
CLEARED_ENV = ["GARMINCONNECT_EMAIL", "GARMINCONNECT_BASE64_PASSWORD", "USER_TIMEZONE", "DAILY_RECORD_TIME",
               "MANUAL_START_DATE", "MANUAL_END_DATE", "FETCH_SELECTION", "TAG_MEASUREMENTS_WITH_USER_EMAIL",
               "REQUEST_INTRADAY_DATA_REFRESH", "UPDATE_INTERVAL_SECONDS", "IGNORE_ERRORS", "GARMIN_DEVICENAME"]


@pytest.fixture
def load_fetch(monkeypatch, tmp_path):
    """load_fetch(env, garmin, clock, influx, as_main=False, module_path=None) -> module (or None under __main__)."""
    def _load(env=None, garmin=None, clock=None, influx=None, as_main=False, module_path=None):
        import influxdb
        import influxdb_client_3
        import garminconnect

        monkeypatch.chdir(tmp_path)  # no override-default-vars.env here
        for key in CLEARED_ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in {**BASE_ENV, "TOKEN_DIR": str(tmp_path / "tokens"), **(env or {})}.items():
            monkeypatch.setenv(key, value)
        influx = influx or InfluxRecorder()
        clock = clock or SimClock(_dt.datetime(2026, 11, 22, 12, 0, tzinfo=UTC))
        garmin = garmin or FakeGarminBase()
        monkeypatch.setattr(influxdb, "InfluxDBClient", influx.client_class())
        monkeypatch.setattr(influxdb_client_3, "InfluxDBClient3", influx.client_class())
        monkeypatch.setattr(garminconnect, "Garmin", lambda *a, **k: garmin)

        shim = types.ModuleType("datetime")
        shim.__dict__.update(_dt.__dict__)
        shim.datetime = fake_datetime_class(clock)
        real_datetime_module = sys.modules["datetime"]
        name = "__main__" if as_main else "garmin_fetch_under_test"
        spec = importlib.util.spec_from_file_location(name, module_path or MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["datetime"] = shim
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules["datetime"] = real_datetime_module
        module.garmin_obj = garmin
        return module
    return _load


@pytest.fixture
def process_tz():
    """Set the process time zone (TZ + tzset) for the test, restored afterwards."""
    old = os.environ.get("TZ")

    def _set(tz_name):
        os.environ["TZ"] = tz_name
        time.tzset()
    yield _set
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()
