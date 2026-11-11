"""
db.py — ④ 저장: Supabase(진짜 DB)

SUPABASE_URL / SUPABASE_SECRET_KEY 가 있으면 Supabase에 저장하고,
없으면 서버 메모리에 임시 저장한다(재시작하면 사라짐).
그래서 변수를 넣기 전 첫 배포도 죽지 않고 /health 에서 무엇이 빠졌는지 알려 준다.
"""
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def kst(ts: str) -> str:
    """DB 시각(UTC 등) → '2026-11-10 14:00' (한국 시간)"""
    return datetime.fromisoformat(ts).astimezone(KST).strftime("%Y-%m-%d %H:%M")


class SupabaseStore:
    kind = "supabase"

    def __init__(self, url, key):
        from supabase import create_client
        self.sb = create_client(url, key)

    def save_readings(self, rows):
        if not rows:
            return 0
        # 같은 측정소·같은 시각이면 새로 넣지 않고 덮어쓴다(중복 방지)
        res = (self.sb.table("air_readings")
               .upsert(rows, on_conflict="source,station,data_time").execute())
        return len(res.data or [])

    def save_log(self, log):
        self.sb.table("collect_logs").insert(log).execute()

    def recent(self, source, limit=300):
        res = (self.sb.table("air_readings").select("*").eq("source", source)
               .order("data_time", desc=True).limit(limit).execute())
        return res.data

    def history(self, source, station, limit=72):
        res = (self.sb.table("air_readings").select("data_time,pm10,pm25")
               .eq("source", source).eq("station", station)
               .order("data_time", desc=True).limit(limit).execute())
        return list(reversed(res.data))

    def logs(self, limit=20):
        res = (self.sb.table("collect_logs").select("*")
               .order("ran_at", desc=True).limit(limit).execute())
        return res.data

    def stats(self, source):
        """누적 행 수 · 수집 횟수 · 가장 오래된/최근 측정 시각"""
        def edge(desc):
            d = (self.sb.table("air_readings").select("data_time").eq("source", source)
                 .order("data_time", desc=desc).limit(1).execute()).data
            return d[0]["data_time"] if d else None

        rows = (self.sb.table("air_readings").select("id", count="exact")
                .eq("source", source).limit(1).execute()).count or 0
        runs = (self.sb.table("collect_logs").select("id", count="exact")
                .eq("source", source).limit(1).execute()).count or 0
        return {"rows": rows, "runs": runs, "first": edge(False), "last": edge(True)}


class MemoryStore:
    kind = "memory(임시)"

    def __init__(self):
        self.rows = {}
        self._logs = []

    def save_readings(self, rows):
        for r in rows:
            self.rows[(r["source"], r["station"], r["data_time"])] = r
        return len(rows)

    def save_log(self, log):
        self._logs.append(log)

    def recent(self, source, limit=300):
        rs = [r for r in self.rows.values() if r["source"] == source]
        return sorted(rs, key=lambda r: r["data_time"], reverse=True)[:limit]

    def history(self, source, station, limit=72):
        rs = [r for r in self.rows.values() if r["source"] == source and r["station"] == station]
        return sorted(rs, key=lambda r: r["data_time"])[-limit:]

    def logs(self, limit=20):
        return list(reversed(self._logs))[:limit]

    def stats(self, source):
        times = sorted(r["data_time"] for r in self.rows.values() if r["source"] == source)
        runs = sum(1 for l in self._logs if l.get("source") == source)
        return {"rows": len(times), "runs": runs,
                "first": times[0] if times else None, "last": times[-1] if times else None}


def get_store():
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if url and key:
        return SupabaseStore(url, key)
    return MemoryStore()
