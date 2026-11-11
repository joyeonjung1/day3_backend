"""
main.py — 제주 미세먼지 백엔드 (FastAPI)

   공공데이터 API ──①받기──▶ FastAPI ──②가공──③예측──▶ ④ Supabase 저장
                                   ▲
   Vercel(프론트) ── 결과만 요청 ───┘    (키는 전부 서버 안에만)

환경변수
  로컬:    backend/.env 파일에서 읽음 (python-dotenv)
  Railway: Variables 탭에 넣은 값이 환경변수로 들어옴 (.env는 올리지 않음)

  SUPABASE_URL, SUPABASE_SECRET_KEY   Supabase 연결 (없으면 메모리 임시 저장)
  DATA_GO_KR_KEY                      공공데이터포털 인증키 (없으면 연습 데이터)
  ADMIN_TOKEN                         /collect 를 아무나 못 누르게 하는 암호
  FRONT_ORIGINS                       프론트 주소 (쉼표로 여러 개, 끝에 / 없이)
  SIDO_NAME=제주  COLLECT_MINUTES=60  AUTO_COLLECT=1  DEMO_MODE=0
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path

from dotenv import load_dotenv

# main.py 바로 옆의 .env를 읽는다 (어느 폴더에서 실행해도 같은 파일).
# 로컬에서만 의미 있음. Railway에는 .env가 없으므로 아무 일도 안 함
load_dotenv(Path(__file__).with_name(".env"))

from fastapi import FastAPI, Header, HTTPException, Query  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

import collect  # noqa: E402
import db  # noqa: E402
import predict  # noqa: E402

log = logging.getLogger("uvicorn.error")

DATA_KEY = os.getenv("DATA_GO_KR_KEY", "").strip()
SIDO = os.getenv("SIDO_NAME", "제주").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEMO = os.getenv("DEMO_MODE", "0") == "1" or not DATA_KEY
SOURCE = "demo" if DEMO else "airkorea"
AUTO = os.getenv("AUTO_COLLECT", "1") == "1"
MINUTES = int(os.getenv("COLLECT_MINUTES", "60"))
FRONT_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONT_ORIGINS", "").split(",") if o.strip()]

store = db.get_store()


# ─────────────── 수집 한 번 = 받기 → 가공 → 저장 → 기록 ───────────────
def run_collect(hours: int = 1) -> dict:
    items = collect.fake_raw(hours) if DEMO else collect.fetch_raw(SIDO, DATA_KEY)
    rows, stats = collect.clean(items, SIDO, SOURCE)
    saved = store.save_readings(rows)
    entry = {"ran_at": datetime.now(timezone.utc).isoformat(), "source": SOURCE,
             **stats, "saved": saved}
    store.save_log(entry)
    log.info("collect %s", entry)
    return entry


async def auto_loop():
    """서버가 켜져 있는 동안 COLLECT_MINUTES마다 자동 수집 (브라우저가 꺼져 있어도!)"""
    # 연습 모드에서 DB가 비어 있으면 예측을 바로 볼 수 있게 72시간치를 한 번 채운다
    try:
        if DEMO and not store.recent(SOURCE, 1):
            await asyncio.to_thread(run_collect, 72)
    except Exception:
        log.exception("연습 데이터 채우기 실패")
    while True:
        try:
            await asyncio.to_thread(run_collect)
        except Exception:
            log.exception("자동 수집 실패")
        await asyncio.sleep(MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_loop()) if AUTO else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="제주 미세먼지 백엔드", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=FRONT_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


@app.get("/")
def home():
    return {"service": "제주 미세먼지 백엔드", "mode": SOURCE, "docs": "/docs"}


@app.get("/health")
def health():
    missing = [k for k in ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "DATA_GO_KR_KEY",
                           "ADMIN_TOKEN", "FRONT_ORIGINS"] if not os.getenv(k, "").strip()]
    return {"ok": True, "mode": SOURCE, "store": store.kind,
            "auto_collect_minutes": MINUTES if AUTO else None, "missing": missing}


@app.post("/collect")
def collect_now(x_admin_token: str = Header(default=""),
                hours: int = Query(1, ge=1, le=168, description="연습 모드에서만: 몇 시간치 만들지")):
    """지금 바로 한 번 수집 (Swagger /docs 에서 Try it out)"""
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN 환경변수를 먼저 넣으세요")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "x-admin-token 이 틀렸습니다")
    try:
        return run_collect(hours if DEMO else 1)
    except Exception as e:
        raise HTTPException(502, str(e))


def _latest_rows():
    rows = store.recent(SOURCE)
    seen, latest = set(), []
    for r in rows:  # 시각 내림차순 → 측정소별 첫 행이 최신
        if r["station"] not in seen:
            seen.add(r["station"])
            latest.append(r)
    return latest


@app.get("/air/latest")
def air_latest():
    rows = _latest_rows()
    rows.sort(key=lambda r: r["pm10"] or 0, reverse=True)
    return [{"station": r["station"], "time": db.kst(r["data_time"]),
             "pm10": r["pm10"], "pm10_grade": r["pm10_grade"],
             "pm25": r["pm25"], "pm25_grade": r["pm25_grade"]} for r in rows]


@app.get("/air/summary")
def air_summary():
    rows = _latest_rows()
    pm10s = [r["pm10"] for r in rows if r["pm10"] is not None]
    pm25s = [r["pm25"] for r in rows if r["pm25"] is not None]
    worst = max(rows, key=lambda r: r["pm10"] or 0) if rows else None
    logs = store.logs(1)
    avg10 = round(sum(pm10s) / len(pm10s)) if pm10s else None
    return {
        "mode": SOURCE,
        "stations": len(rows),
        "avg_pm10": avg10,
        "avg_pm25": round(sum(pm25s) / len(pm25s)) if pm25s else None,
        "avg_pm10_grade": collect.grade_pm10(avg10),
        "worst_station": worst and {"station": worst["station"], "pm10": worst["pm10"]},
        "last_collect": logs[0] if logs else None,
    }


@app.get("/air/predict")
def air_predict(station: str = Query(..., max_length=30)):
    hist = store.history(SOURCE, station)
    if not hist:
        raise HTTPException(404, f"'{station}' 측정소 데이터가 없습니다")
    return {"station": station, **predict.predict_next(hist)}


@app.get("/logs")
def logs():
    return store.logs()
