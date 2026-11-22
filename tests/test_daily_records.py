"""Stamps and time-zone fields of date-level records (DAILY_RECORD_TIME, utcOffsetMinutes, calendarDate)."""
import datetime as dt
import subprocess
from zoneinfo import ZoneInfo

import pytest

from conftest import REPO, FakeGarminBase, InfluxRecorder, SimClock, stats_json

UTC = dt.timezone.utc
VIEWERS = ["Europe/London", "Europe/Rome", "America/New_York"]
DATE_LEVEL = ["DailyStats", "FitnessAge", "VO2_Max", "RacePredictions", "BodyComposition", "LactateThreshold",
              "HillScore", "EnduranceScore", "Hydration", "LifestyleJournal"]
DATE_LEVEL_SELECTION = "daily_avg,fitness_age,vo2,race_prediction,body_composition,lactate_threshold,hill_score,endurance_score,hydration,lifestyle"
NEW_FIELDS = ("utcOffsetMinutes", "calendarDate")


def ms(instant):
    return int(instant.timestamp() * 1000)


class DayGarmin(FakeGarminBase):
    """One synthetic day per date: stats with the given offsets, and a value for every date-level endpoint."""

    def __init__(self, offsets):
        super().__init__()
        self.offsets = offsets  # {date: (start_offset_min, end_offset_min)}

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if callable(attr) and (name.startswith("get_") or name == "connectapi"):
            calls = super().__getattribute__("calls")

            def logged(*args, **kwargs):
                calls.append((name, args, tuple(sorted(kwargs.items()))))
                return attr(*args, **kwargs)
            return logged
        return attr

    def get_stats(self, date_str):
        start, end = self.offsets.get(date_str, (None, None))
        if start is None:
            return super().get_stats(date_str)
        return stats_json(date_str, start, end)

    def get_fitnessage_data(self, date_str):
        return {"chronologicalAge": 40, "fitnessAge": 35, "achievableFitnessAge": 33}

    def get_max_metrics(self, date_str):
        return [{"generic": {"vo2MaxPreciseValue": 50.1}, "cycling": None}]

    def get_race_predictions(self, startdate=None, enddate=None, _type=None):
        return [{"time5K": 1500, "time10K": 3200, "timeHalfMarathon": 7000, "timeMarathon": 15000}]

    def get_weigh_ins(self, start, end):
        return {"dailyWeightSummaries": [{"allWeightMetrics": [
            {"timestampGMT": None, "weight": 70000.0, "bmi": 22.0, "sourceType": "MANUAL"},
            {"timestampGMT": ms(dt.datetime.strptime(start, "%Y-%m-%d").replace(hour=6, tzinfo=UTC)), "weight": 70100.0, "sourceType": "INDEX_SCALE"},
        ]}]}

    def connectapi(self, endpoint, **kwargs):
        return [{"value": 170 if "HeartRate" in endpoint else 3.4}]

    def get_hill_score(self, date_str):
        return {"overallScore": 60, "strengthScore": 50}

    def get_endurance_score(self, date_str):
        return {"overallScore": 7000}

    def get_hydration_data(self, date_str):
        return {"valueInML": 1500.0, "goalInML": 2000.0}

    def get_lifestyle_logging_data(self, date_str):
        return {"dailyLogsReport": [{"name": "Alcohol", "category": "LIFESTYLE", "logStatus": "YES", "details": [{"amount": 2}]}]}

    # instants (not date-level), only for the legacy comparison
    def get_sleep_data(self, date_str):
        end = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=6, tzinfo=UTC)
        return {"dailySleepDTO": {"calendarDate": date_str, "sleepTimeSeconds": 25000,
                                  "sleepStartTimestampGMT": ms(end) - 25_000_000, "sleepEndTimestampGMT": ms(end),
                                  "sleepStartTimestampLocal": ms(end) - 25_000_000 + 3_600_000, "sleepEndTimestampLocal": ms(end) + 3_600_000,
                                  "sleepScores": {"overall": {"value": 80}}},
                "sleepLevels": [{"startGMT": f"{date_str}T01:00:00.0", "endGMT": f"{date_str}T02:00:00.0", "activityLevel": 1}],
                "avgOvernightHrv": 45}

    def get_heart_rates(self, date_str):
        base = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=8, tzinfo=UTC)
        return {"heartRateValues": [[ms(base), 61], [ms(base) + 120_000, 63]]}

    def get_training_readiness(self, date_str):
        return [{"score": 70, "timestamp": f"{date_str}T07:00:00.0"}]


CASES = [  # date, offset at the day's start, at its end (minutes)
    pytest.param("2026-01-15", 60, 60, id="UTC+1"),
    pytest.param("2026-07-15", 120, 120, id="UTC+2-summer"),
    pytest.param("2026-11-02", 180, 180, id="UTC+3-trip"),
    pytest.param("2026-01-20", -300, -300, id="UTC-5"),
    pytest.param("2026-03-29", 60, 120, id="EU-spring-DST"),
    pytest.param("2026-10-25", 120, 60, id="EU-autumn-DST"),
    pytest.param("2026-11-03", 180, 60, id="travel-UTC+3-to-UTC+1"),
    pytest.param("2025-12-20", 60, -300, id="travel-Rome-to-New-York"),
]


@pytest.mark.parametrize("date_str,start_off,end_off", CASES)
def test_local_noon_stamps_fall_on_their_date_for_every_viewer(load_fetch, date_str, start_off, end_off):
    influx = InfluxRecorder()
    gf = load_fetch(env={"DAILY_RECORD_TIME": "local_noon", "FETCH_SELECTION": DATE_LEVEL_SELECTION},
                    garmin=DayGarmin({date_str: (start_off, end_off)}), influx=influx)
    gf.daily_fetch_write(date_str)

    rows = [p for p in influx.points if p["measurement"] in DATE_LEVEL and "calendarDate" in p["fields"]]
    assert {p["measurement"] for p in rows} == set(DATE_LEVEL)
    expected = (dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12) - dt.timedelta(minutes=end_off)).replace(tzinfo=UTC)
    for p in rows:
        stamp = dt.datetime.fromisoformat(p["time"])
        assert stamp == expected, p["measurement"]
        assert p["fields"]["calendarDate"] == date_str
        assert p["fields"]["utcOffsetMinutes"] == end_off
        for viewer in VIEWERS:
            assert stamp.astimezone(ZoneInfo(viewer)).date().isoformat() == date_str, (p["measurement"], viewer)


def test_offset_falls_back_to_user_timezone_at_local_noon(load_fetch):
    # No wellness data that day: USER_TIMEZONE evaluated at 12:00 of the date, DST-aware (CEST on the spring DST day).
    gf = load_fetch(env={"DAILY_RECORD_TIME": "local_noon", "USER_TIMEZONE": "Europe/Rome"})
    day = gf.get_day_context("2026-03-29", {"wellnessStartTimeGmt": None})
    assert day["utc_offset"] == dt.timedelta(hours=2)
    assert gf.daily_record_time(day, "legacy") == "2026-03-29T10:00:00+00:00"
    assert gf.get_day_context("2026-03-28")["utc_offset"] == dt.timedelta(hours=1)


def test_offset_falls_back_to_utc(load_fetch):
    gf = load_fetch(env={"DAILY_RECORD_TIME": "local_noon"})
    day = gf.get_day_context("2026-03-29", {})
    assert day["utc_offset"] == dt.timedelta(0) and day["day_end_utc"] is None


@pytest.fixture
def upstream_module(tmp_path):
    """garmin_fetch.py exactly as at upstream commit 8375155 (read from the local git history)."""
    path = tmp_path / "garmin_fetch_8375155.py"
    path.write_bytes(subprocess.run(["git", "-C", str(REPO), "show", "8375155:src/garmin_grafana/garmin_fetch.py"],
                                    check=True, capture_output=True).stdout)
    return path


ALL_SELECTION = "daily_avg,sleep,steps,heartrate,stress,breathing,hrv,fitness_age,vo2,activity,race_prediction,body_composition,lifestyle,lactate_threshold,training_status,training_readiness,hill_score,endurance_score,blood_pressure,hydration"


@pytest.mark.parametrize("date_str,start_off,end_off", CASES[:4])
def test_legacy_points_are_the_old_points_plus_two_fields(load_fetch, upstream_module, process_tz, date_str, start_off, end_off):
    process_tz("UTC")  # LactateThreshold used the container's TZ before; the comparison holds for TZ=UTC, the Docker default
    old_influx, new_influx = InfluxRecorder(), InfluxRecorder()
    old_garmin, new_garmin = DayGarmin({date_str: (start_off, end_off)}), DayGarmin({date_str: (start_off, end_off)})
    env = {"FETCH_SELECTION": ALL_SELECTION}
    old = load_fetch(env=env, garmin=old_garmin, influx=old_influx, module_path=upstream_module)
    old.daily_fetch_write(date_str)
    new = load_fetch(env=env, garmin=new_garmin, influx=new_influx)
    new.daily_fetch_write(date_str)

    stripped = []
    for p in new_influx.points:
        fields = {k: v for k, v in p["fields"].items() if k not in NEW_FIELDS}
        stripped.append({**p, "fields": fields})
    assert stripped == old_influx.points
    assert new_garmin.calls == old_garmin.calls  # same Garmin requests, same order
    tagged = {p["measurement"] for p in new_influx.points if "calendarDate" in p["fields"]}
    assert tagged == set(DATE_LEVEL) | {"SleepSummary"}


@pytest.mark.parametrize("mode", ["legacy", "local_noon"])
def test_lactate_threshold_does_not_depend_on_process_tz(load_fetch, process_tz, mode):
    stamps = {}
    for tz_name in ["America/New_York", "Asia/Tokyo"]:
        process_tz(tz_name)
        gf = load_fetch(env={"DAILY_RECORD_TIME": mode, "LACTATE_THRESHOLD_SPORTS": "RUNNING"}, garmin=DayGarmin({}))
        points = gf.get_lactate_threshold("2026-01-15")  # no day context given: offset from USER_TIMEZONE or UTC
        stamps[tz_name] = {p["time"] for p in points}
    assert stamps["America/New_York"] == stamps["Asia/Tokyo"]
    expected = "2026-01-15T00:00:00+00:00" if mode == "legacy" else "2026-01-15T12:00:00+00:00"
    assert stamps["Asia/Tokyo"] == {expected}


def test_sleep_summary_gets_calendar_date_and_offset(load_fetch):
    gf = load_fetch(garmin=FakeGarminBase())
    end_gmt = dt.datetime(2026, 11, 3, 5, 30, tzinfo=UTC)
    gf.garmin_obj.get_sleep_data = lambda date_str: {"dailySleepDTO": {
        "calendarDate": "2026-11-03", "sleepTimeSeconds": 27000,
        "sleepStartTimestampGMT": ms(end_gmt) - 27_000_000, "sleepEndTimestampGMT": ms(end_gmt),
        "sleepStartTimestampLocal": ms(end_gmt) - 27_000_000 + 10_800_000, "sleepEndTimestampLocal": ms(end_gmt) + 10_800_000}}
    summary = [p for p in gf.get_sleep_data("2026-11-03") if p["measurement"] == "SleepSummary"]
    assert len(summary) == 1
    assert summary[0]["time"] == "2026-11-03T05:30:00+00:00"  # stamp unchanged: an instant
    assert summary[0]["fields"]["calendarDate"] == "2026-11-03"
    assert summary[0]["fields"]["utcOffsetMinutes"] == 180


def test_daily_stats_written_once_the_watch_day_has_begun(load_fetch, process_tz):
    # Container in New York (18:30 on Jan 15), watch in Tokyo where Jan 16 began at 15:00 UTC.
    process_tz("America/New_York")
    clock = SimClock(dt.datetime(2026, 1, 15, 23, 30, tzinfo=UTC))
    gf = load_fetch(clock=clock)
    tokyo_day = stats_json("2026-01-16", 540, 540, complete=False, last_sync_utc=dt.datetime(2026, 1, 15, 23, 0, tzinfo=UTC))
    gf.garmin_obj.get_stats = lambda date_str: tokyo_day
    assert len(gf.get_daily_stats("2026-01-16")) == 1
    # A day that has not begun in the watch's zone is not written.
    ny_day = stats_json("2026-01-16", -300, -300, complete=False, last_sync_utc=dt.datetime(2026, 1, 15, 23, 0, tzinfo=UTC))
    gf.garmin_obj.get_stats = lambda date_str: ny_day
    assert len(gf.get_daily_stats("2026-01-16")) == 0
