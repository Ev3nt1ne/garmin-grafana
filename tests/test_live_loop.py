"""The live loop (__main__) must not drop a date before its last hours are fetched (upstream issue #185).

Each scenario runs the module's real __main__ block against a fake Garmin account whose watch uploads
at given instants; time.sleep(UPDATE_INTERVAL_SECONDS) moves to the next upload.
"""
import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeGarminBase, InfluxRecorder, SimClock, stats_json

UTC = dt.timezone.utc
INTERVAL = 4242
ROME = ZoneInfo("Europe/Rome")


class ScenarioDone(BaseException):
    pass


def rome_offset(instant):
    return instant.astimezone(ROME).utcoffset()


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


class LoopGarmin(FakeGarminBase):
    """A watch in a zone given by offset_at(instant), uploading at the given instants, with an HR sample every 15 min."""

    def __init__(self, offset_at, uploads, clock):
        super().__init__()
        self.offset_at, self.uploads, self.clock, self.index = offset_at, uploads, clock, 0
        self.clock.now = uploads[0] + dt.timedelta(minutes=2)

    @property
    def upload(self):
        return self.uploads[self.index]

    def advance(self):
        self.index += 1
        if self.index == len(self.uploads):
            raise ScenarioDone()
        self.clock.now = self.upload + dt.timedelta(minutes=2)

    def day_bounds(self, date_str):
        def local_midnight_utc(day):
            guess = (day - self.offset_at(day.replace(tzinfo=UTC))).replace(tzinfo=UTC)
            return (day - self.offset_at(guess)).replace(tzinfo=UTC)
        day = dt.datetime.strptime(date_str, "%Y-%m-%d")
        return local_midnight_utc(day), local_midnight_utc(day + dt.timedelta(days=1))

    def get_device_last_used(self):
        return {"lastUsedDeviceName": "FakeWatch", "userDeviceId": None, "imageUrl": None,
                "lastUsedDeviceUploadTime": int(self.upload.timestamp() * 1000)}

    def get_last_activity(self):
        start = self.uploads[0] - dt.timedelta(hours=3)
        local = start + self.offset_at(start)
        return {"startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"), "startTimeLocal": local.strftime("%Y-%m-%d %H:%M:%S")}

    def get_stats(self, date_str):
        self._log("get_stats", date_str, self.index)
        start, end = self.day_bounds(date_str)
        if start > self.upload:
            return super().get_stats(date_str)
        start_off = round(self.offset_at(start).total_seconds() / 60)
        if self.upload >= end:
            return stats_json(date_str, start_off, round(self.offset_at(end - dt.timedelta(seconds=1)).total_seconds() / 60))
        return stats_json(date_str, start_off, round(self.offset_at(self.upload).total_seconds() / 60), complete=False, last_sync_utc=self.upload)

    def get_heart_rates(self, date_str):
        self._log("get_heart_rates", date_str, self.index)
        start, end = self.day_bounds(date_str)
        values, t = [], start
        while t < min(end, self.upload):
            values.append([int(t.timestamp() * 1000), 60])
            t += dt.timedelta(minutes=15)
        return {"heartRateValues": values}


def run_loop(load_fetch, monkeypatch, garmin, influx, env):
    def fake_sleep(seconds):
        if seconds == INTERVAL:
            garmin.advance()
    monkeypatch.setattr(time, "sleep", fake_sleep)
    with pytest.raises(ScenarioDone):
        load_fetch(env={"UPDATE_INTERVAL_SECONDS": str(INTERVAL), "FETCH_SELECTION": "daily_avg,heartrate", **env},
                   garmin=garmin, clock=garmin.clock, influx=influx, as_main=True)


def hr_dates_per_upload(garmin):
    per = [[] for _ in garmin.uploads]
    for name, date_str, index in (c for c in garmin.calls if c[0] == "get_heart_rates"):
        per[index].append(date_str)
    return per


def assert_last_hour_of_day_written(garmin, influx, date_str):
    start, end = garmin.day_bounds(date_str)
    last_sample = (end - dt.timedelta(minutes=15)).isoformat()
    written = {p["time"] for p in influx.measurement("HeartRateIntraday")}
    complete_fetches = [i for name, d, i in (c for c in garmin.calls if c[0] == "get_heart_rates") if d == date_str and garmin.uploads[i] >= end]
    assert complete_fetches, f"{date_str} was never fetched after its end ({end.isoformat()}); fetches per upload: {hr_dates_per_upload(garmin)}"
    assert last_sample in written, f"last HR sample of {date_str} ({last_sample}) never written"


def test_dst_end_after_start_up_keeps_the_day_until_complete(load_fetch, monkeypatch):
    # Started in summer time (UTC+2); DST ends on 2026-10-25; on 26 Oct the watch uploads at 23:30 local (UTC+1), then at 07:00.
    uploads = [utc(2026, 10, 24, 9), utc(2026, 10, 25, 10), utc(2026, 10, 26, 10), utc(2026, 10, 26, 22, 30), utc(2026, 10, 27, 6), utc(2026, 10, 27, 12)]
    garmin, influx = LoopGarmin(rome_offset, uploads, SimClock(None)), InfluxRecorder(last_hr_time="2026-10-24T08:00:00Z")
    run_loop(load_fetch, monkeypatch, garmin, influx, {"USER_TIMEZONE": "Europe/Rome"})
    assert_last_hour_of_day_written(garmin, influx, "2026-10-26")


def test_westward_travel_keeps_the_day_until_complete(load_fetch, monkeypatch):
    # A trip at UTC+3 (offset learnt from the last activity at start-up); flight home to UTC+1 on 3 Nov;
    # upload at 23:30 local (22:30 UTC) on 3 Nov, then at 07:00 on 4 Nov.
    def trip_offset(instant):
        return dt.timedelta(hours=3) if instant < utc(2026, 11, 3, 15) else dt.timedelta(hours=1)
    uploads = [utc(2026, 11, 2, 7), utc(2026, 11, 3, 9), utc(2026, 11, 3, 22, 30), utc(2026, 11, 4, 6), utc(2026, 11, 4, 12)]
    garmin, influx = LoopGarmin(trip_offset, uploads, SimClock(None)), InfluxRecorder(last_hr_time="2026-11-02T06:00:00Z")
    run_loop(load_fetch, monkeypatch, garmin, influx, {})
    assert_last_hour_of_day_written(garmin, influx, "2026-11-03")


def test_steady_zone_fetches_the_same_dates_as_before(load_fetch, monkeypatch):
    # No travel, no DST change: the window (and so the number of Garmin calls) is the original one.
    uploads = [utc(2026, 1, 10, 8), utc(2026, 1, 10, 22, 30), utc(2026, 1, 10, 23, 30), utc(2026, 1, 11, 7), utc(2026, 1, 11, 7)]
    garmin, influx = LoopGarmin(rome_offset, uploads, SimClock(None)), InfluxRecorder(last_hr_time="2026-01-10T07:00:00Z")
    run_loop(load_fetch, monkeypatch, garmin, influx, {})
    assert hr_dates_per_upload(garmin) == [["2026-01-10"], ["2026-01-10"], ["2026-01-11", "2026-01-10"], ["2026-01-11"], []]
    assert_last_hour_of_day_written(garmin, influx, "2026-01-10")
