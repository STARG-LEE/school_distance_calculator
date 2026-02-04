import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import io

st.set_page_config(
    page_title="학생 통학시간 계산기",
    page_icon="🚌",
    layout="wide"
)

# ============================================================
# 핵심 함수들
# ============================================================

def get_departure_timestamp(hour: int, minute: int = 0, date: Optional[str] = None) -> int:
    """출발 시간을 Unix timestamp로 변환"""
    if date:
        dt = datetime.strptime(f"{date} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
    else:
        now = datetime.now()
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
    return int(dt.timestamp())


def calculate_commute_times(
    students: list[dict],
    school_address: str,
    api_key: str,
    mode: str = "transit",
    departure_hour: Optional[int] = None,
    departure_minute: int = 0,
    departure_date: Optional[str] = None,
    progress_bar=None
) -> pd.DataFrame:
    """학생들의 학교까지 통학 시간을 계산"""
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    results = []
    batch_size = 25
    total_batches = (len(students) + batch_size - 1) // batch_size
    
    for batch_idx, i in enumerate(range(0, len(students), batch_size)):
        batch = students[i:i + batch_size]
        addresses = [s["주소"] for s in batch]
        
        params = {
            "origins": "|".join(addresses),
            "destinations": school_address,
            "mode": mode,
            "language": "ko",
            "key": api_key
        }
        
        if departure_hour is not None:
            params["departure_time"] = get_departure_timestamp(
                departure_hour, departure_minute, departure_date
            )
        
        response = requests.get(url, params=params)
        data = response.json()
        
        if data["status"] != "OK":
            st.error(f"API 오류: {data['status']} - {data.get('error_message', '')}")
            continue
        
        for j, row in enumerate(data["rows"]):
            student = batch[j]
            element = row["elements"][0]
            
            if element["status"] == "OK":
                results.append({
                    "이름": student["이름"],
                    "주소": student["주소"],
                    "거리": element["distance"]["text"],
                    "거리(m)": element["distance"]["value"],
                    "소요시간": element["duration"]["text"],
                    "소요시간(분)": element["duration"]["value"] // 60
                })
            else:
                error_status = element["status"]
                results.append({
                    "이름": student["이름"],
                    "주소": student["주소"],
                    "거리": f"오류: {error_status}",
                    "거리(m)": None,
                    "소요시간": f"오류: {error_status}",
                    "소요시간(분)": None
                })
        
        if progress_bar:
            progress_bar.progress((batch_idx + 1) / total_batches)
    
    return pd.DataFrame(results)


# ============================================================
# Streamlit UI
# ============================================================

st.title("🚌 학생 통학시간 계산기")
st.markdown("학생 명단(이름, 주소)을 업로드하면 학교까지 대중교통 소요시간을 계산합니다.")

# 고정 설정
SCHOOL_ADDRESS = "경기도 포천시 해룡로 120"

# API 키 (Streamlit secrets에서 불러오기)
try:
    api_key = st.secrets["GOOGLE_MAPS_API_KEY"]
except KeyError:
    api_key = None

# 사이드바 설정
with st.sidebar:
    st.header("⚙️ 설정")
    
    if not api_key:
        st.error("API 키가 설정되지 않았습니다. Streamlit secrets에 GOOGLE_MAPS_API_KEY를 추가해주세요.")
    else:
        st.success("✅ API 키 로드 완료")
    
    st.info(f"🏫 학교: {SCHOOL_ADDRESS}")
    
    st.subheader("출발 시간 설정")
    use_departure_time = st.checkbox("특정 출발 시간 지정", value=True)
    
    if use_departure_time:
        col1, col2 = st.columns(2)
        with col1:
            departure_hour = st.number_input("시", min_value=0, max_value=23, value=8)
        with col2:
            departure_minute = st.number_input("분", min_value=0, max_value=59, value=0)
        
        departure_date = st.date_input(
            "출발 날짜",
            value=datetime.now().date() + timedelta(days=1),
            help="과거 날짜는 지정할 수 없습니다"
        )
    else:
        departure_hour = None
        departure_minute = 0
        departure_date = None

# 메인 영역
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("📁 파일 업로드")
    
    uploaded_file = st.file_uploader(
        "학생 명단 파일 (xlsx 또는 csv)",
        type=["xlsx", "csv"],
        help="컬럼명: '이름', '주소'"
    )
    
    if uploaded_file:
        try:
            if uploaded_file.name.endswith('.csv'):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
            
            # 컬럼 확인
            if "이름" not in df.columns or "주소" not in df.columns:
                st.error("파일에 '이름'과 '주소' 컬럼이 필요합니다.")
            else:
                st.success(f"✅ {len(df)}명의 학생 데이터 로드 완료")
                st.dataframe(df, use_container_width=True, height=200)
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")

with col2:
    st.subheader("📋 파일 형식 안내")
    st.markdown("""
    엑셀/CSV 파일은 아래 형식으로 준비해주세요:
    
    | 이름 | 주소 |
    |------|------|
    | 김철수 | 서울시 강남구 역삼동 |
    | 이영희 | 경기도 성남시 분당구 |
    """)
    
    # 템플릿 다운로드
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

# 계산 버튼
if st.button("🚀 통학시간 계산하기", type="primary", use_container_width=True):
    # 유효성 검사
    if not api_key:
        st.error("API 키가 설정되지 않았습니다.")
    elif not uploaded_file:
        st.error("학생 명단 파일을 업로드해주세요.")
    else:
        students = df.to_dict("records")
        
        st.info(f"🔄 {len(students)}명의 통학시간을 계산 중...")
        progress_bar = st.progress(0)
        
        result_df = calculate_commute_times(
            students=students,
            school_address=SCHOOL_ADDRESS,
            api_key=api_key,
            mode="transit",
            departure_hour=departure_hour if use_departure_time else None,
            departure_minute=departure_minute,
            departure_date=str(departure_date) if use_departure_time and departure_date else None,
            progress_bar=progress_bar
        )
        
        if len(result_df) > 0:
            st.success("✅ 계산 완료!")
            
            # 결과 테이블
            st.subheader("📊 계산 결과")
            st.dataframe(result_df, use_container_width=True)
            
            # 통계
            valid_times = result_df["소요시간(분)"].dropna()
            if len(valid_times) > 0:
                st.subheader("📈 통계")
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("평균", f"{valid_times.mean():.0f}분")
                col2.metric("최소", f"{valid_times.min():.0f}분")
                col3.metric("최대", f"{valid_times.max():.0f}분")
                col4.metric("계산 성공", f"{len(valid_times)}/{len(result_df)}명")
            
            # 다운로드 버튼
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
