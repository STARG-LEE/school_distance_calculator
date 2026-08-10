import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import time
import io

st.set_page_config(
    page_title="학생 통학시간 계산기",
    page_icon="🚌",
    layout="wide"
)

# ============================================================
# 카카오 API 엔드포인트
# ============================================================

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_FUTURE_URL = "https://apis-navi.kakaomobility.com/v1/future/directions"
KAKAO_ORIGINS_URL = "https://apis-navi.kakaomobility.com/v1/origins/directions"

MAX_ORIGINS = 30  # 다중 출발지 길찾기 1회 최대 출발지 수


# ============================================================
# 공통 유틸
# ============================================================

def _request(method: str, url: str, headers: dict, retries: int = 3, **kwargs) -> Optional[dict]:
    """카카오 API 호출. 429/5xx는 백오프 후 재시도."""
    for attempt in range(retries):
        try:
            response = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        except requests.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
            continue

        if response.status_code == 200:
            return response.json()

        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
            continue

        # 401/403 등은 재시도해도 소용없으므로 즉시 중단
        st.error(f"카카오 API 오류 {response.status_code}: {response.text[:200]}")
        return None

    return None


# ============================================================
# 1단계 — 주소를 좌표로 변환 (카카오 로컬 API)
# ============================================================

def geocode(address: str, api_key: str, cache: dict) -> Optional[dict]:
    """주소 문자열을 좌표로 변환. 주소검색 실패 시 키워드(장소명) 검색으로 재시도."""
    address = str(address).strip()
    if not address:
        return None
    if address in cache:
        return cache[address]

    headers = {"Authorization": f"KakaoAK {api_key}"}
    result = None

    data = _request("GET", KAKAO_ADDRESS_URL, headers, params={"query": address, "size": 1})
    if data and data.get("documents"):
        doc = data["documents"][0]
        result = {
            "x": float(doc["x"]),
            "y": float(doc["y"]),
            "matched": doc.get("address_name", address),
            "method": "주소검색"
        }
    else:
        # 도로명/지번이 아닌 건물명·아파트명 등은 키워드 검색으로 보정
        data = _request("GET", KAKAO_KEYWORD_URL, headers, params={"query": address, "size": 1})
        if data and data.get("documents"):
            doc = data["documents"][0]
            result = {
                "x": float(doc["x"]),
                "y": float(doc["y"]),
                "matched": doc.get("address_name") or doc.get("place_name", address),
                "method": "장소검색"
            }

    cache[address] = result
    return result


def geocode_students(students: list[dict], api_key: str, progress_bar=None) -> list[dict]:
    """학생 명단 전체를 좌표로 변환. 실패한 학생도 coord=None으로 남겨 누락되지 않게 한다."""
    cache: dict = {}
    resolved = []

    for idx, student in enumerate(students):
        coord = geocode(student["주소"], api_key, cache)
        resolved.append({**student, "coord": coord})
        if progress_bar:
            progress_bar.progress((idx + 1) / len(students))

    return resolved


# ============================================================
# 2단계 — 통학 경로 계산 (카카오모빌리티 길찾기 API)
# ============================================================

def _row(student: dict, coord: Optional[dict], distance_m=None, duration_s=None, error: str = "") -> dict:
    """결과 한 줄. 실패해도 반드시 한 줄을 만들어 학생이 사라지지 않게 한다."""
    return {
        "이름": student["이름"],
        "주소": student["주소"],
        "인식된 주소": coord["matched"] if coord else "",
        "거리": f"{distance_m / 1000:.1f} km" if distance_m is not None else f"오류: {error}",
        "거리(m)": distance_m,
        "소요시간": f"{duration_s // 60}분" if duration_s is not None else f"오류: {error}",
        "소요시간(분)": duration_s // 60 if duration_s is not None else None,
    }


def calculate_future(
    students: list[dict],
    destination: dict,
    api_key: str,
    departure_time: str,
    priority: str,
    progress_bar=None
) -> pd.DataFrame:
    """출발시각 지정 모드 — 학생 1명당 1회 호출(미래 운행 정보 길찾기)."""
    headers = {"Authorization": f"KakaoAK {api_key}", "Content-Type": "application/json"}
    results = []

    for idx, student in enumerate(students):
        coord = student["coord"]
        if not coord:
            results.append(_row(student, None, error="주소 좌표 변환 실패"))
        else:
            params = {
                "origin": f"{coord['x']},{coord['y']}",
                "destination": f"{destination['x']},{destination['y']}",
                "departure_time": departure_time,
                "priority": priority,
            }
            data = _request("GET", KAKAO_FUTURE_URL, headers, params=params)
            routes = (data or {}).get("routes") or [{}]
            route = routes[0]

            if route.get("result_code") == 0:
                summary = route["summary"]
                results.append(_row(student, coord, summary["distance"], summary["duration"]))
            else:
                msg = route.get("result_msg") or "응답 없음"
                results.append(_row(student, coord, error=msg))

        if progress_bar:
            progress_bar.progress((idx + 1) / len(students))

    return pd.DataFrame(results)


def calculate_realtime(
    students: list[dict],
    destination: dict,
    api_key: str,
    priority: str,
    progress_bar=None
) -> pd.DataFrame:
    """현재 교통 기준 모드 — 30명씩 묶어 호출(다중 출발지 길찾기)."""
    headers = {"Authorization": f"KakaoAK {api_key}", "Content-Type": "application/json"}
    # 명단 순서를 그대로 유지하기 위해 자리를 미리 잡아두고 채운다
    results: list[Optional[dict]] = [None] * len(students)

    # 좌표 변환에 실패한 학생은 요청에서 빼되, 결과에는 오류로 남긴다
    routable = [(idx, s) for idx, s in enumerate(students) if s["coord"]]
    for idx, student in enumerate(students):
        if not student["coord"]:
            results[idx] = _row(student, None, error="주소 좌표 변환 실패")

    total_batches = max(1, (len(routable) + MAX_ORIGINS - 1) // MAX_ORIGINS)

    for batch_idx, i in enumerate(range(0, len(routable), MAX_ORIGINS)):
        batch = routable[i:i + MAX_ORIGINS]
        body = {
            "origins": [
                {"x": s["coord"]["x"], "y": s["coord"]["y"], "key": str(idx)}
                for idx, s in batch
            ],
            "destination": {"x": destination["x"], "y": destination["y"]},
            "radius": 10000,
            "priority": priority,
        }

        data = _request("POST", KAKAO_ORIGINS_URL, headers, json=body)
        by_key = {r.get("key"): r for r in (data or {}).get("routes", [])}

        for idx, s in batch:
            route = by_key.get(str(idx))
            if route is None:
                # 배치 전체가 실패해도 학생별로 오류 행을 남긴다
                results[idx] = _row(s, s["coord"], error="요청 실패")
            elif route.get("result_code") == 0:
                summary = route["summary"]
                results[idx] = _row(s, s["coord"], summary["distance"], summary["duration"])
            else:
                msg = route.get("result_msg") or "경로를 찾을 수 없음"
                results[idx] = _row(s, s["coord"], error=msg)

        if progress_bar:
            progress_bar.progress((batch_idx + 1) / total_batches)

    return pd.DataFrame(results)


# ============================================================
# Streamlit UI
# ============================================================

st.title("🚌 학생 통학시간 계산기")
st.markdown("학생 명단(이름, 주소)을 업로드하면 학교까지 소요시간을 계산합니다. **카카오 길찾기 API 기반**입니다.")

SCHOOL_ADDRESS = "경기도 포천시 해룡로 120"

try:
    api_key = st.secrets["KAKAO_REST_API_KEY"]
except KeyError:
    api_key = None

with st.sidebar:
    st.header("⚙️ 설정")

    if not api_key:
        st.error("API 키가 없습니다. Streamlit secrets에 `KAKAO_REST_API_KEY`를 추가해주세요.")
        st.caption("카카오 개발자센터에서 발급한 **REST API 키**입니다.")
    else:
        st.success("✅ 카카오 API 키 로드 완료")

    st.info(f"🏫 학교: {SCHOOL_ADDRESS}")

    st.subheader("계산 기준")
    mode = st.radio(
        "모드",
        ["출발시각 지정", "현재 교통 기준"],
        help="출발시각 지정은 학생 1명당 1회 호출하고, 현재 교통 기준은 30명씩 묶어 호출합니다."
    )

    if mode == "출발시각 지정":
        col1, col2 = st.columns(2)
        with col1:
            departure_hour = st.number_input("시", min_value=0, max_value=23, value=8)
        with col2:
            departure_minute = st.number_input("분", min_value=0, max_value=59, value=0)

        departure_date = st.date_input(
            "출발 날짜",
            value=datetime.now().date() + timedelta(days=1),
            min_value=datetime.now().date(),
            help="카카오 미래 운행 정보는 현재 시각 이후만 조회할 수 있습니다."
        )
    else:
        departure_hour = departure_minute = departure_date = None

    priority = st.selectbox(
        "경로 우선순위",
        ["RECOMMEND", "TIME", "DISTANCE"],
        format_func=lambda v: {"RECOMMEND": "추천", "TIME": "최단시간", "DISTANCE": "최단거리"}[v]
    )

    st.warning("카카오는 대중교통 경로 API를 제공하지 않아 **자동차 기준**으로 계산됩니다.", icon="🚗")

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("📁 파일 업로드")

    uploaded_file = st.file_uploader(
        "학생 명단 파일 (xlsx 또는 csv)",
        type=["xlsx", "csv"],
        help="컬럼명: '이름', '주소'"
    )

    df = None
    if uploaded_file:
        try:
            if uploaded_file.name.endswith('.csv'):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)

            if "이름" not in df.columns or "주소" not in df.columns:
                st.error("파일에 '이름'과 '주소' 컬럼이 필요합니다.")
                df = None
            else:
                st.success(f"✅ {len(df)}명의 학생 데이터 로드 완료")
                st.dataframe(df, use_container_width=True, height=200)
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")
            df = None

with col2:
    st.subheader("📋 파일 형식 안내")
    st.markdown("""
    엑셀/CSV 파일은 아래 형식으로 준비해주세요:

    | 이름 | 주소 |
    |------|------|
    | 김철수 | 서울시 강남구 역삼동 |
    | 이영희 | 경기도 성남시 분당구 |
    """)

    template_df = pd.DataFrame({
        "이름": ["김철수", "이영희", "박민수"],
        "주소": ["서울시 강남구 역삼동", "경기도 성남시 분당구", "인천시 연수구 송도동"]
    })

    buffer = io.BytesIO()
    template_df.to_excel(buffer, index=False)
    buffer.seek(0)

    st.download_button(
        label="📥 템플릿 다운로드",
        data=buffer,
        file_name="학생명단_템플릿.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

st.divider()

if st.button("🚀 통학시간 계산하기", type="primary", use_container_width=True):
    if not api_key:
        st.error("API 키가 설정되지 않았습니다.")
    elif df is None:
        st.error("학생 명단 파일을 업로드해주세요.")
    else:
        students = df.to_dict("records")

        # 학교 주소도 같은 방식으로 좌표 변환
        destination = geocode(SCHOOL_ADDRESS, api_key, {})
        if not destination:
            st.error(f"학교 주소를 좌표로 변환하지 못했습니다: {SCHOOL_ADDRESS}")
            st.stop()

        st.info(f"📍 {len(students)}명의 주소를 좌표로 변환 중...")
        geo_progress = st.progress(0)
        resolved = geocode_students(students, api_key, geo_progress)

        failed_geo = [s["이름"] for s in resolved if not s["coord"]]
        if failed_geo:
            st.warning(f"주소를 찾지 못한 학생 {len(failed_geo)}명: {', '.join(map(str, failed_geo))}")

        st.info(f"🔄 {len(students)}명의 통학시간을 계산 중...")
        calc_progress = st.progress(0)

        if mode == "출발시각 지정":
            departure_time = f"{departure_date:%Y%m%d}{departure_hour:02d}{departure_minute:02d}"
            result_df = calculate_future(
                resolved, destination, api_key, departure_time, priority, calc_progress
            )
        else:
            result_df = calculate_realtime(
                resolved, destination, api_key, priority, calc_progress
            )

        if len(result_df) > 0:
            st.success("✅ 계산 완료!")

            st.subheader("📊 계산 결과")
            st.dataframe(result_df, use_container_width=True)

            valid_times = result_df["소요시간(분)"].dropna()
            if len(valid_times) > 0:
                st.subheader("📈 통계")
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("평균", f"{valid_times.mean():.0f}분")
                col2.metric("최소", f"{valid_times.min():.0f}분")
                col3.metric("최대", f"{valid_times.max():.0f}분")
                col4.metric("계산 성공", f"{len(valid_times)}/{len(result_df)}명")

            st.subheader("💾 결과 다운로드")
            col1, col2 = st.columns(2)

            with col1:
                buffer = io.BytesIO()
                result_df.to_excel(buffer, index=False)
                buffer.seek(0)
                st.download_button(
                    label="📥 Excel 다운로드",
                    data=buffer,
                    file_name="통학시간_결과.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

            with col2:
                csv = result_df.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    label="📥 CSV 다운로드",
                    data=csv,
                    file_name="통학시간_결과.csv",
                    mime="text/csv"
                )
