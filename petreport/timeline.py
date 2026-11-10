"""Presentation coordinates for local calendar days, including clock changes."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def day_timeline(records, local_date, timezone):
    zone = ZoneInfo(timezone)
    start = datetime.combine(local_date, time.min, zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, zone)
    elapsed = end.timestamp() - start.timestamp()

    def x(stamp):
        fraction = (stamp.timestamp() - start.timestamp()) / elapsed
        return round(32 + max(0, min(1, fraction)) * 936, 2)

    bounds = [start.replace(hour=hour) for hour in (0, 6, 12, 18)] + [end]
    periods = [
        {
            "name": name,
            "hour": f"{i * 6:02}:00",
            "end_hour": f"{(i + 1) * 6:02}:00",
            "night": i in (0, 3),
            "x": x(bounds[i]),
            "width": x(bounds[i + 1]) - x(bounds[i]),
            "center": (x(bounds[i]) + x(bounds[i + 1])) / 2,
            "visits": [],
        }
        for i, name in enumerate(("Night", "Morning", "Afternoon", "Evening"))
    ]
    visits = []
    previous = None
    for index, record in enumerate(
        sorted(records, key=lambda v: (datetime.fromisoformat(v["start"]), v["id"])), 1
    ):
        visit = dict(record)
        stamp = datetime.fromisoformat(visit["start"]).astimezone(zone)
        clock = stamp.strftime("%I:%M %p").lstrip("0")
        if elapsed != 86400:
            clock += " " + stamp.tzname()
        visit.update(clock=clock, moment_index=index, x=x(stamp), lane="right" if index % 2 == 0 else "left")
        end_stamp = datetime.fromisoformat(visit["end"]) if visit.get("end") else stamp
        visit["end_x"] = max(visit["x"], x(end_stamp))
        gap = max(0, int((stamp.timestamp() - previous.timestamp()) / 60)) if previous else 0
        visit["gap_label"] = ""
        if gap >= 45:
            hours, minutes = divmod(gap, 60)
            visit["gap_label"] = (f"{hours}h " if hours else "") + (f"{minutes}m" if minutes else "")
            visit["gap_size"] = "long" if gap >= 360 else "medium" if gap >= 120 else "short"
        previous = stamp
        period = 0 if stamp < start else min(3, stamp.hour // 6)
        periods[period]["visits"].append(visit)
        visits.append(visit)
    return {
        "periods": periods,
        "visits": visits,
        "hours": elapsed / 3600,
        "next_date": end.date(),
        "ticks": [{"x": x(stamp), "label": f"{i * 6:02}:00"} for i, stamp in enumerate(bounds)],
    }
