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
  SIDO_NAME=제주  COLLECT_MINUTES=60  AUTO_COLLECT=1
  FORCE_DEMO=1                        (선택) 키가 있어도 연습 데이터를 쓰고 싶을 때만. 평소엔 넣지 않음
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
# 연습 모드는 (1) 인증키가 없거나 (2) FORCE_DEMO=1 일 때만 켜진다.
# 예전 변수 DEMO_MODE 는 더 이상 쓰지 않는다 (Railway에 남아 있어도 무시됨)
FORCE_DEMO = os.getenv("FORCE_DEMO", "0").strip() == "1"
DEMO = FORCE_DEMO or not DATA_KEY
DEMO_REASON = ("FORCE_DEMO=1" if FORCE_DEMO else "DATA_GO_KR_KEY 없음") if DEMO else None
SOURCE = "demo" if DEMO else "airkorea"
if os.getenv("DEMO_MODE"):
    logging.getLogger("uvicorn.error").warning(
        "DEMO_MODE 변수는 더 이상 쓰지 않습니다. 연습 모드가 필요하면 FORCE_DEMO=1 을 쓰세요.")
AUTO = os.getenv("AUTO_COLLECT", "1") == "1"
MINUTES = int(os.getenv("COLLECT_MINUTES", "60"))


def _parse_origins(raw: str) -> list[str]:
    """쉼표로 나눈 주소 목록. Railway에 실수로 넣은 따옴표·공백·끝의 / 를 정리한다.
    예) "https://a.vercel.app, http://127.0.0.1:5000/"  →  ['https://a.vercel.app', 'http://127.0.0.1:5000']
    """
    out = []
    for o in raw.split(","):
        o = o.strip().strip("\"'").strip().rstrip("/")
        if o and o not in out:
            out.append(o)
    return out


FRONT_ORIGINS = _parse_origins(os.getenv("FRONT_ORIGINS", ""))

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
    return {"service": "서울 미세먼지 백엔드", "mode": SOURCE, "docs": "/docs"}


@app.get("/health")
def health():
    missing = [k for k in ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "DATA_GO_KR_KEY",
                           "ADMIN_TOKEN", "FRONT_ORIGINS"] if not os.getenv(k, "").strip()]
    return {"ok": True, "mode": SOURCE, "demo_reason": DEMO_REASON, "store": store.kind,
            "auto_collect_minutes": MINUTES if AUTO else None, "missing": missing,
            # 허용된 프론트 주소 (CORS 확인용. 주소라서 비밀이 아님)
            "front_origins": FRONT_ORIGINS}


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


@app.get("/air/history")
def air_history(station: str = Query(..., max_length=30),
                limit: int = Query(48, ge=1, le=168, description="최근 몇 건 (시간 단위)")):
    """DB에 시간마다 쌓인 기록 (오래된 순). 예측의 근거가 되는 데이터"""
    hist = store.history(SOURCE, station, limit)
    return [{"time": db.kst(h["data_time"]), "pm10": h["pm10"], "pm25": h["pm25"]} for h in hist]


@app.get("/air/stats")
def air_stats():
    """DB(Supabase)에 지금까지 얼마나 쌓였는지 — 저장이 실제로 일어나는지 확인용"""
    s = store.stats(SOURCE)
    for k in ("first", "last"):
        s[k] = db.kst(s[k]) if s.get(k) else None
    return {"mode": SOURCE, "store": store.kind, **s}


@app.get("/logs")
def logs():
    return store.logs()
