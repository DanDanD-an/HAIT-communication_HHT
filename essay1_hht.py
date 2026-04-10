import streamlit as st
from streamlit_autorefresh import st_autorefresh
from datetime import datetime
import time
import uuid
import threading
import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────
# 0. 페이지 설정
# ─────────────────────────────────────────
st.set_page_config(page_title="협업 과제 실험 (HHT)", layout="centered")

# ─────────────────────────────────────────
# 1. Google Sheets 연결
# ─────────────────────────────────────────
@st.cache_resource
def connect_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(
        st.secrets["GCP_SERVICE_ACCOUNT"],
        scopes=scope
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(st.secrets["SHEET_KEY"])

    survey_ws       = spreadsheet.worksheet("survey_hht")
    conversation_ws = spreadsheet.worksheet("conversation_hht")
    proposal_ws     = spreadsheet.worksheet("proposal_hht")
    consent_ws      = spreadsheet.worksheet("consent_hht")
    # 두 참가자 간 실시간 채팅을 위한 공유 메시지 시트
    chatroom_ws     = spreadsheet.worksheet("chatroom_hht")

    return survey_ws, conversation_ws, proposal_ws, consent_ws, chatroom_ws

survey_ws, conversation_ws, proposal_ws, consent_ws, chatroom_ws = connect_sheets()

# ─────────────────────────────────────────
# 2. 헤더 자동 삽입
# ─────────────────────────────────────────
@st.cache_resource
def ensure_headers(_survey_ws, _conversation_ws, _proposal_ws, _consent_ws, _chatroom_ws):
    """앱 전체에서 딱 1번만 실행 — autorefresh/다중 세션 무관"""
    def _check(ws, headers):
        for attempt in range(3):
            try:
                if not ws.get("A1"):
                    ws.append_row(headers)
                return
            except Exception:
                if attempt < 2:
                    time.sleep(3)
    _check(_survey_ws, [
        "timestamp", "user_id", "room_id", "condition", "role",
        "mc_partner_type",
        "trust_R1","trust_R2","trust_R3","trust_R4","trust_R5",
        "trust_T1","trust_T2","trust_T3","trust_T4","trust_T5",
        "trust_U1","trust_U2","trust_U3","trust_U4","trust_U5",
        "trust_F1","trust_F2","trust_F3","trust_F4","trust_F5",
        "trust_P1","trust_P2","trust_P3","trust_P4","trust_P5",
        "team1","team2","team3","team4","team5",
        "sat1","sat2","sat3","sat4","sat5","sat6",
        "perf1","perf2","perf3","perf_self",
        "topic_sensitivity","kakao_id",
    ])
    _check(_conversation_ws, ["timestamp","user_id","room_id","role","message"])
    _check(_proposal_ws, ["timestamp","user_id","room_id","condition","role","gdocs_link","proposal_text"])
    _check(_consent_ws, ["consent_timestamp","user_id","agreement"])
    _check(_chatroom_ws, ["timestamp","room_id","user_id","role","message"])

ensure_headers(survey_ws, conversation_ws, proposal_ws, consent_ws, chatroom_ws)

def sheets_append(ws, row):
    """Google Sheets 안전 쓰기 — 429 시 최대 3회 재시도"""
    for attempt in range(4):
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
            return
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                time.sleep(3 * (attempt + 1))
            else:
                return

# ─────────────────────────────────────────
# 3. 역할 레이블
# ─────────────────────────────────────────
PARTNER_ROLE_LABEL = {
    "기획자": "개발자",
    "개발자": "기획자"
}

# ─────────────────────────────────────────
# 4. 세션 초기화
# ─────────────────────────────────────────
def init_session():
    defaults = {
        "user_id":            str(uuid.uuid4())[:8],
        "phase":              "consent",
        "condition":          "HHT",
        "role":               None,
        "room_id":            None,
        "task_start":         None,
        "timer_expired":      False,
        "submitted_proposal": False,
        # 양쪽 모두 입장했는지 여부
        "both_ready":         False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session()

# ─────────────────────────────────────────
# 5. 유틸 함수
# ─────────────────────────────────────────
TASK_DURATION = 30 * 60

def remaining_seconds():
    if st.session_state.task_start is None:
        return TASK_DURATION
    elapsed = time.time() - st.session_state.task_start
    return max(0, TASK_DURATION - elapsed)

def fmt_time(secs):
    m, s = divmod(int(secs), 60)
    return f"{m:02d}:{s:02d}"

def go(phase):
    st.session_state.phase = phase
    st.rerun()

# ─────────────────────────────────────────
# 채팅방 공통 읽기 (TTL 캐시로 API 절약)
# ─────────────────────────────────────────
_CACHE_TTL = 4  # 초 — autorefresh 간격과 맞춤

def _fetch_chatroom_rows():
    """
    chatroom_hht 전체를 읽되, _CACHE_TTL초 안에 중복 호출하면
    캐시된 결과를 반환해 429를 방지한다.
    check_both_ready / poll_messages 모두 이 함수를 공유해
    rerun당 API 호출이 최대 1회로 제한된다.
    """
    now = time.time()
    cached_time = st.session_state.get("_chatroom_cache_time", 0)
    if now - cached_time < _CACHE_TTL and "_chatroom_cache" in st.session_state:
        return st.session_state["_chatroom_cache"]
    try:
        all_rows = chatroom_ws.get_all_values()
        rows = [r for r in all_rows[1:] if len(r) >= 5]
        st.session_state["_chatroom_cache"] = rows
        st.session_state["_chatroom_cache_time"] = now
        return rows
    except Exception:
        return st.session_state.get("_chatroom_cache", [])

def check_both_ready() -> bool:
    """같은 room_id에서 [READY] 메시지가 2개 이상이면 True"""
    rows = _fetch_chatroom_rows()
    count = sum(
        1 for row in rows
        if row[1] == st.session_state.room_id and row[4] == "[READY]"
    )
    return count >= 2

def send_message(message: str):
    """메시지를 백그라운드 스레드로 저장 (UI 블로킹 방지)"""
    ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    room_id = st.session_state.room_id
    user_id = st.session_state.user_id
    role    = st.session_state.role

    def _send():
        sheets_append(chatroom_ws, [ts, room_id, user_id, role, message])
        sheets_append(conversation_ws, [ts, user_id, room_id, role, message])
        st.session_state.pop("_chatroom_cache", None)
        st.session_state.pop("_chatroom_cache_time", None)

    threading.Thread(target=_send, daemon=True).start()

def poll_messages():
    """
    chatroom_hht 시트에서 현재 room_id의 채팅 메시지만 순서대로 반환.
    (캐시된 rows 재사용 -> API 추가 호출 없음)
    """
    rows = _fetch_chatroom_rows()
    result = []
    for row in rows:
        if row[1] != st.session_state.room_id:
            continue
        if row[4] == "[READY]":
            continue
        result.append({
            "user_id": row[2],
            "role":    row[3],
            "message": row[4],
        })
    return result

# ─────────────────────────────────────────
# 7. 동의서 화면
# ─────────────────────────────────────────
if st.session_state.phase == "consent":

    st.title("(온라인) 연구참여 동의서")

    st.markdown("""
■ **연구과제명**: 인간–AI 협업과 인간–인간 협업에서의 커뮤니케이션 특성 비교 연구

■ **IRB 승인번호**: KUIRB-2026-0079-01
""")

    st.divider()

    st.markdown("""
**1.** 본인은 연구참여 설명서를 읽었고, 내용을 충분히 이해하였습니다.

**2.** 본인은 연구 목적을 위해 자발적으로 연구에 참여합니다.

**3.** 본인은 원하지 않을 경우 언제든지 연구 참여를 거절할 수 있으며, 이에 따른 어떠한 불이익도 본인에게 없음을 알고 있습니다.

**4.** 본 연구의 연구진행의 윤리적 측면이나 연구대상자의 권리에 대해 질문이 있는 경우 연락할 수 있는 담당자와 연락처를 알고 있습니다.

> ☞ 본 연구의 책임자는 아래와 같습니다.
> - **주소**: 서울특별시 성북구 안암로 145 고려대학교 미디어관 404호
> - **연구책임자**: 고려대학교 백현미 교수
> - **(연구실 유선)전화번호**: 02-3290-2254
> - **전자우편**: lotus1225@korea.ac.kr

**5.** 본인은 연구에 자발적으로 참여하는 것에 동의합니다.
""")

    st.divider()

    agree = st.radio(
        "연구참여 동의 여부를 선택해 주세요.",
        [" 연구참여에 동의합니다.", " 연구참여에 동의하지 않습니다."],
        index=None
    )

    if st.button("다음 →", disabled=(agree is None)):
        consent_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if agree == " 연구참여에 동의하지 않습니다.":
            sheets_append(consent_ws, [
                consent_timestamp,
                st.session_state.user_id,
                "비동의"
            ])
            st.warning("연구 참여에 동의하지 않으셨습니다. 참여해 주셔서 감사합니다.")
            st.stop()

        sheets_append(consent_ws, [
            consent_timestamp,
            st.session_state.user_id,
            "동의"
        ])
        go("role_assign")

# ─────────────────────────────────────────
# 9. 역할 배정 (URL 파라미터 방식)
# ─────────────────────────────────────────
elif st.session_state.phase == "role_assign":

    if st.session_state.role is None or st.session_state.room_id is None:
        params = st.query_params
        url_role    = params.get("role", "")
        url_room_id = params.get("room_id", "")

        if url_role in ["기획자", "개발자"] and url_room_id:
            st.session_state.role    = url_role
            st.session_state.room_id = url_room_id
        else:
            st.error("❌ 올바른 링크로 접속해 주세요. 연구자에게 문의하세요.")
            st.caption("링크 예시: ?role=기획자&room_id=ROOM001")
            st.stop()

    role         = st.session_state.role
    partner_role = PARTNER_ROLE_LABEL[role]

    st.title("역할 배정 결과")
    st.success(f"귀하의 역할은 **{role}** 입니다.")
    st.info(f"파트너는 **{partner_role}** 역할을 맡았습니다. 같은 room에 접속한 인간 참가자입니다.")

    if st.button("과제 설명서 확인하기 →"):
        go("task_desc")

# ─────────────────────────────────────────
# 10. 과제 설명서 (공통)
# ─────────────────────────────────────────
elif st.session_state.phase == "task_desc":

    st.title("협업 과제 설명서")

    st.markdown("""
우리 회사는 **'MZ 세대를 위한 식단 관리 앱'** 출시를 앞두고 있습니다.
현재 **기능 6개가 후보로 검토**되고 있으나, **총 예산 100포인트의 제약**으로 인해 모든 기능을 넣을 수는 없습니다.
""")
    st.divider()

    role         = st.session_state.role
    partner_role = PARTNER_ROLE_LABEL[role]

    st.subheader("목표")
    st.markdown(f"""
- 기능 후보 6개 중 **예산을 초과하지 않는 최적의 기능 조합 선정**
- **{role} 역할을 맡은 참여자**와 **{partner_role} 역할을 맡은 파트너(인간)**가 정보를 공유하고 합의하여 하나의 최종 앱 기획안 작성
""")
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("과제 규칙")
    st.markdown("""
- 협업 과제는 **30분간** 진행되며, 과제 종료 후 각 팀은 **A4 1쪽 내외의 기획안**을 제출해야 합니다.
- 파트너와 **익명 텍스트 채팅으로만 협업**합니다. (이미지·파일·음성 공유는 허용되지 않습니다.)
- 각 참여자는 기획자 또는 개발자 역할을 맡으며, 역할에 따라 서로 다른 정보를 제공받습니다.
- **외부 AI 도구(ChatGPT, Gemini 등) 사용은 엄격히 금지**됩니다.
""")
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("제출물 (최종 기획안) — A4 1쪽 분량")
    st.markdown("""
최종 기획안에는 아래 내용이 포함되어야 합니다.
1. 주요 타겟층 정의
2. 최종 선정 기능과 선정 사유
3. 기대효과와 한계

카카오톡을 통해 제공되는 템플릿 링크(Google Docs)에 작성해 주시면 됩니다.
""")
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("유의사항")
    st.markdown("""
- 과제 종료 후 기획안, 대화 데이터 및 사후 설문 응답 제출이 확인된 모든 참가자분께 익명 채팅방을 통해 **1만 원**을 지급할 예정입니다.
- 최종 기획안은 외부 평가자에 의해 심사되며, **우수 팀(5팀)에게는 추가 보상(인당 2만 원)**을 지급할 예정입니다.
- 부적절한 언어 사용 시 실험이 즉시 종료되며, 이 경우 보상 지급이 어렵습니다.
""")

    st.divider()
    st.info("📌 다음 페이지에서 **귀하의 역할 카드**를 확인하실 수 있습니다. 역할 카드의 전용 정보는 요약하여 공유할 수 있으나, 표·문장을 그대로 복사·붙여넣기하는 것은 허용되지 않습니다.")

    if st.button("역할 카드 확인하기 →"):
        go("role_card")

# ─────────────────────────────────────────
# 10-2. 역할 카드
# ─────────────────────────────────────────
elif st.session_state.phase == "role_card":

    role         = st.session_state.role
    partner_role = PARTNER_ROLE_LABEL[role]
    st.title(f"역할 카드 — {role}")

    if role == "기획자":
        st.markdown(f"""
당신은 **기획 담당자**로서 사용자의 입장에서 가장 매력적인 앱을 만들어야 합니다.
**당신에게만 제공되는 기획자 전용 정보**를 바탕으로 {partner_role}와 협상하여 역할 목표를 달성하세요.
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("역할 목표")
        st.markdown("""
- 시장 경쟁력과 사용자 만족도를 극대화하는 앱 기획
- **주어진 팀 예산(100포인트) 준수**
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("정보 공유 규칙")
        st.markdown("""
- 기능별 기본 설명과 예산은 모든 참가자에게 동일하게 제공됩니다.
- **아래의 기획자 전용 정보는 대화를 통해 요약하여 공유할 수 있으나, 표·이미지·문장 그대로의 복사·붙여넣기는 허용되지 않습니다.**
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("""
| ID | 기능명 | 설명 | 기획자 전용 정보 | 예산 |
|:---:|:---|:---|:---|:---:|
| A | **AI 카메라 식단 스캔** | 사진 촬영 시 음식 종류와 칼로리를 자동 기록 | 92%의 유저가 이 기능이 없으면 앱을 설치하지 않겠다고 답했습니다. 경쟁력 확보를 위한 핵심 기능입니다. | 60p |
| B | **영양사 1:1 상담** | 전문 영양사와 채팅을 통한 식단 피드백 | 유사 서비스에서 상담 기능 사용자는 평균 체류 시간이 1.6배 길었습니다. 향후 유료 모델로 확장할 가능성도 있습니다. | 30p |
| C | **게임형 챌린지** | 친구와 식단 미션 경쟁 및 보상 포인트 지급 | MZ세대 대상 테스트에서 주간 재방문율이 약 35% 증가한 기능입니다. 친구 초대와 결합될 경우 확산 효과가 큽니다. | 40p |
| D | **심플 텍스트 기록** | 유저가 직접 텍스트로 식단 입력 | 사용자 인터뷰에서 "귀찮다"는 응답이 92%로, 유저의 이탈을 유발할 가능성이 큽니다. 기존 서비스와 차별점이 부족합니다. | 30p |
| E | **커뮤니티 게시판** | 유저 간 식단 공유, 댓글 및 좋아요 소통 기능 | 유사 서비스 분석 결과, 유저 간 소통은 앱 이탈을 방지할 수 있었으나, 새로운 유저 유입에는 큰 효과가 없었습니다. | 20p |
| F | **유전자 데이터 연동** | 외부 기관과 연동해 체질별 맞춤형 식단 추천 | 최신 트렌드이지만, 개인정보 제공에 대한 거부감을 표시한 응답자가 약 35%로 나타나 초기 확산이 제한될 수 있습니다. | 50p |
""")

    else:  # 개발자
        st.markdown(f"""
당신은 **개발 책임자**로서 한정된 예산 내에서 안정적으로 작동하는 앱을 설계해야 합니다.
**당신에게만 제공되는 개발자 전용 정보**를 바탕으로 {partner_role}와 협상하여 역할 목표를 달성하세요.
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("역할 목표")
        st.markdown("""
- 기술적으로 안정적이고 구현 가능한 앱 설계
- **주어진 팀 예산(100포인트) 준수**
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("정보 공유 규칙")
        st.markdown("""
- 기능별 기본 설명과 예산은 모든 참가자에게 동일하게 제공됩니다.
- **아래의 개발자 전용 정보는 대화를 통해 요약하여 공유할 수 있으나, 표·이미지·문장 그대로의 복사·붙여넣기는 허용되지 않습니다.**
""")
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("""
| ID | 기능명 | 설명 | 개발자 전용 정보 | 예산 |
|:---:|:---|:---|:---|:---:|
| A | **AI 카메라 식단 스캔** | 사진 촬영 시 음식 종류와 칼로리를 자동 기록 | 현재 팀 자원 상 일정 수준의 정확도를 확보하기 어렵습니다. 초기 오류가 누적되면 앱 스토어 평점이 1점 하락할 수 있습니다. | 60p |
| B | **영양사 1:1 상담** | 전문 영양사와 채팅을 통한 식단 피드백 | 구현은 쉽지만 상담 인력 관리와 24시간 서버 운영으로 리소스 부담이 기존 대비 약 1.6배 증가할 가능성이 있습니다. | 30p |
| C | **게임형 챌린지** | 친구와 식단 미션 경쟁 및 보상 포인트 지급 | 기존 로직을 활용할 수 있어 추가 서버 부하는 10% 이내로 예상됩니다. 일정 내 안정적 구현이 가능합니다. | 40p |
| D | **심플 텍스트 기록** | 유저가 직접 텍스트로 식단 입력 | 개발 공수가 가장 낮고 데이터 오류 발생률이 1% 미만으로 예상됩니다. 안정적인 데이터 기록을 위한 핵심 기능입니다. | 30p |
| E | **커뮤니티 게시판** | 유저 간 식단 공유, 댓글 및 좋아요 소통 기능 | 일반적인 게시판 형태라 무난하게 개발 가능합니다. 다만 사용자 관리와 운영 정책이 함께 필요합니다. | 20p |
| F | **유전자 데이터 연동** | 외부 기관과 연동해 체질별 맞춤형 식단 추천 | 외부 기관 API를 활용할 수 있어 내부 개발 공수는 전체의 약 10% 수준으로 예상됩니다. 안정적 구현이 가능한 기능입니다. | 50p |
""")

    st.divider()
    st.info("📌 역할 카드를 충분히 숙지하셨으면 아래 버튼을 눌러 파트너와의 채팅을 시작하세요. 과제(채팅) 중에도 역할 카드 확인이 가능합니다.")
    st.warning("⚠️ 외부 AI 도구(ChatGPT, Gemini 등) 사용은 실험 규정상 엄격히 금지됩니다.")

    if st.button("채팅방 입장 →"):
        # 입장 기록을 Sheets에 저장 (타이머는 양쪽 모두 입장 후 시작)
        sheets_append(chatroom_ws, [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            st.session_state.room_id,
            st.session_state.user_id,
            st.session_state.role,
            "[READY]"
        ])
        go("task")

# ─────────────────────────────────────────
# 11. 협업 과제 (인간-인간 채팅)
# ─────────────────────────────────────────
elif st.session_state.phase == "task":

    # 5초마다 자동 리렌더링 → 상대방 입장 확인 + 메시지 polling + 타이머 갱신
    st_autorefresh(interval=4_000, key="task_autorefresh")

    # 양쪽 모두 입장했는지 확인 (한 번 True가 되면 다시 체크 안 함)
    if not st.session_state.both_ready:
        if check_both_ready():
            st.session_state.both_ready = True
            st.session_state.task_start = time.time()
        else:
            st.title("⏳ 파트너 입장 대기 중...")
            st.info("파트너가 채팅방에 입장하면 자동으로 과제가 시작됩니다.")
            st.stop()

    role         = st.session_state.role
    partner_role = PARTNER_ROLE_LABEL[role]
    rem          = remaining_seconds()

    # ── 타이머
    timer_col, _ = st.columns([1, 3])
    with timer_col:
        if rem > 5 * 60:
            st.metric("⏱ 남은 시간", fmt_time(rem))
        elif rem > 0:
            st.warning(f"⚠️ 남은 시간: {fmt_time(rem)}")
        else:
            st.error("⏰ 30분이 종료되었습니다. 아래 버튼을 눌러 기획안을 제출해 주세요.")

    st.markdown(f"**역할**: {role} | **파트너 역할**: {partner_role} (인간)  |  **Room**: `{st.session_state.room_id}`")

    # ── 역할 카드 (접기)
    with st.expander("📋 내 역할 카드 확인하기"):
        if role == "기획자":
            st.markdown(f"""
당신은 **기획 담당자**로서 사용자의 입장에서 가장 매력적인 앱을 만들어야 합니다.
**당신에게만 제공되는 기획자 전용 정보**를 바탕으로 {partner_role}와 협상하여 역할 목표를 달성하세요.
""")
            st.markdown("""
- 시장 경쟁력과 사용자 만족도를 극대화하는 앱 기획 / **주어진 팀 예산(100포인트) 준수**
- 전용 정보는 대화를 통해 요약 공유 가능 (표·문장 그대로 복붙 금지)
""")
            st.markdown("""
| ID | 기능명 | 설명 | 기획자 전용 정보 | 예산 |
|:---:|:---|:---|:---|:---:|
| A | **AI 카메라 식단 스캔** | 사진 촬영 시 음식 종류와 칼로리를 자동 기록 | 92%의 유저가 이 기능이 없으면 앱을 설치하지 않겠다고 답했습니다. 경쟁력 확보를 위한 핵심 기능입니다. | 60p |
| B | **영양사 1:1 상담** | 전문 영양사와 채팅을 통한 식단 피드백 | 유사 서비스에서 상담 기능 사용자는 평균 체류 시간이 1.6배 길었습니다. 향후 유료 모델로 확장할 가능성도 있습니다. | 30p |
| C | **게임형 챌린지** | 친구와 식단 미션 경쟁 및 보상 포인트 지급 | MZ세대 대상 테스트에서 주간 재방문율이 약 35% 증가한 기능입니다. 친구 초대와 결합될 경우 확산 효과가 큽니다. | 40p |
| D | **심플 텍스트 기록** | 유저가 직접 텍스트로 식단 입력 | 사용자 인터뷰에서 "귀찮다"는 응답이 92%로, 유저의 이탈을 유발할 가능성이 큽니다. 기존 서비스와 차별점이 부족합니다. | 30p |
| E | **커뮤니티 게시판** | 유저 간 식단 공유, 댓글 및 좋아요 소통 기능 | 유사 서비스 분석 결과, 유저 간 소통은 앱 이탈을 방지할 수 있었으나, 새로운 유저 유입에는 큰 효과가 없었습니다. | 20p |
| F | **유전자 데이터 연동** | 외부 기관과 연동해 체질별 맞춤형 식단 추천 | 최신 트렌드이지만, 개인정보 제공에 대한 거부감을 표시한 응답자가 약 35%로 나타나 초기 확산이 제한될 수 있습니다. | 50p |
""")
        else:
            st.markdown(f"""
당신은 **개발 책임자**로서 한정된 예산 내에서 안정적으로 작동하는 앱을 설계해야 합니다.
**당신에게만 제공되는 개발자 전용 정보**를 바탕으로 {partner_role}와 협상하여 역할 목표를 달성하세요.
""")
            st.markdown("""
- 기술적으로 안정적이고 구현 가능한 앱 설계 / **주어진 팀 예산(100포인트) 준수**
- 전용 정보는 대화를 통해 요약 공유 가능 (표·문장 그대로 복붙 금지)
""")
            st.markdown("""
| ID | 기능명 | 설명 | 개발자 전용 정보 | 예산 |
|:---:|:---|:---|:---|:---:|
| A | **AI 카메라 식단 스캔** | 사진 촬영 시 음식 종류와 칼로리를 자동 기록 | 현재 팀 자원 상 일정 수준의 정확도를 확보하기 어렵습니다. 초기 오류가 누적되면 앱 스토어 평점이 1점 하락할 수 있습니다. | 60p |
| B | **영양사 1:1 상담** | 전문 영양사와 채팅을 통한 식단 피드백 | 구현은 쉽지만 상담 인력 관리와 24시간 서버 운영으로 리소스 부담이 기존 대비 약 1.6배 증가할 가능성이 있습니다. | 30p |
| C | **게임형 챌린지** | 친구와 식단 미션 경쟁 및 보상 포인트 지급 | 기존 로직을 활용할 수 있어 추가 서버 부하는 10% 이내로 예상됩니다. 일정 내 안정적 구현이 가능합니다. | 40p |
| D | **심플 텍스트 기록** | 유저가 직접 텍스트로 식단 입력 | 개발 공수가 가장 낮고 데이터 오류 발생률이 1% 미만으로 예상됩니다. 안정적인 데이터 기록을 위한 핵심 기능입니다. | 30p |
| E | **커뮤니티 게시판** | 유저 간 식단 공유, 댓글 및 좋아요 소통 기능 | 일반적인 게시판 형태라 무난하게 개발 가능합니다. 다만 사용자 관리와 운영 정책이 함께 필요합니다. | 20p |
| F | **유전자 데이터 연동** | 외부 기관과 연동해 체질별 맞춤형 식단 추천 | 외부 기관 API를 활용할 수 있어 내부 개발 공수는 전체의 약 10% 수준으로 예상됩니다. 안정적 구현이 가능한 기능입니다. | 50p |
""")

    st.divider()

    # ── 메시지 polling (autorefresh 시마다 시트에서 전체 재구성)
    messages = poll_messages()

    # API 응답이 늦거나 비어있으면 이전 결과 유지 (깜빡임 방지)
    if not messages and "last_messages" in st.session_state:
        messages = st.session_state["last_messages"]
    elif messages:
        st.session_state["last_messages"] = messages

    # ── 채팅 메시지 표시
    for entry in messages:
        is_me = (entry["user_id"] == st.session_state.user_id)
        if is_me:
            with st.chat_message("user", avatar="🧑"):
                st.write(f"**나 ({role})**: {entry['message']}")
        else:
            with st.chat_message("assistant", avatar="🤝"):
                st.write(f"**파트너 ({entry['role']})**: {entry['message']}")

    # ── 메시지 입력
    user_input = st.chat_input("메시지를 입력하세요...")

    if user_input:
        # 백그라운드로 Sheets 저장 (chatroom + conversation 동시 처리)
        send_message(user_input)
        st.rerun()

    st.divider()
    st.info("📌 해당 페이지를 벗어나면 채팅 내용의 확인이 불가합니다. 카카오톡을 통해 제공된 구글독스 링크에 기획안을 완성한 상태에서 제출 버튼을 눌러주세요.")
    st.warning("⚠️ 채팅은 실시간 동기화 방식으로, 메시지가 바로 표시되지 않을 수 있습니다. 메시지 전송 후 잠시 기다려주세요.")
    if st.button("✅ 기획안 완성 → 제출 페이지로"):
        go("proposal")

# ─────────────────────────────────────────
# 12. 기획안 제출
# ─────────────────────────────────────────
elif st.session_state.phase == "proposal":

    st.title("기획안 제출")
    st.write("협업 중 Google Docs에 작성한 최종 기획안의 링크를 아래에 붙여넣어 주세요.")

    st.markdown("""
**기획안 구성 요소** (A4 1쪽 분량):
1. 주요 타겟층 정의
2. 최종 선정 기능과 선정 사유 (예산 총액 기재)
3. 기대효과와 한계

> 💡 Google Docs 공유 방법: **공유 → 링크 복사**해 붙여넣어 주세요.
>    (*링크가 있는 모든 사용자 → 편집자로 설정되어 있는지 확인해 주세요.)
""")

    gdocs_link = st.text_input(
        "Google Docs 링크 *",
        placeholder="https://docs.google.com/document/d/..."
    )

    if st.button("기획안 제출 →"):
        if not gdocs_link.strip() or not gdocs_link.strip().startswith("https://"):
            st.error("⚠️ 유효한 Google Docs 링크를 입력해 주세요.")
            st.stop()

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        sheets_append(proposal_ws, [
            timestamp,
            st.session_state.user_id,
            st.session_state.room_id,
            st.session_state.condition,
            st.session_state.role,
            gdocs_link.strip(),
            ""
        ])

        st.session_state.submitted_proposal = True
        go("survey")

# ─────────────────────────────────────────
# 13. 사후 설문
# ─────────────────────────────────────────
elif st.session_state.phase == "survey":

    st.title("사후 설문")
    st.write("협업 경험에 관한 설문입니다. 솔직하게 응답해 주세요. (약 10분 소요)")

    st.divider()
    st.subheader("📌 보상 지급 정보")
    kakao_id = st.text_input(
        "**카카오톡 아이디를 입력해 주세요.** (보상 지급에 사용됩니다)",
        placeholder="카카오톡 아이디 입력"
    )

    st.markdown("""
    <style>
    div[data-testid="stRadio"] label,
    div[data-testid="stRadio"] > label {
        font-size: 1.08rem !important;
        font-weight: 700 !important;
    }
    div[data-testid="stRadio"] > div label {
        font-size: 1.0rem !important;
        font-weight: 400 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    scale5 = ["전혀 그렇지 않다", "그렇지 않다", "보통이다", "그렇다", "매우 그렇다"]

    # ── 조작점검
    st.subheader("1. 조작 점검")
    mc_partner = st.radio(
        "방금 함께 과제를 수행한 파트너는 무엇이었습니까?",
        ["인간 파트너", "AI 파트너"],
        index=None
    )

    # ─────────────────────────────────────────
    # ── 신뢰 (HAIT, Madsen & Gregor 2000 기반 25문항)
    #    HHT 조건에서는 "AI 파트너" → "파트너(인간)"로 표현 변경
    # ─────────────────────────────────────────
    st.divider()
    st.subheader("2. 파트너 신뢰")
    st.caption("다음 문항은 협업 과제에서 경험한 인간 파트너에 대한 신뢰를 묻는 문항입니다.")

    st.markdown("<br>", unsafe_allow_html=True)
    trust_R1 = st.radio("**파트너는 내가 의사결정을 내리는 데 필요한 의견을 제공했다.**", scale5, index=None, key="trust_R1")
    trust_R2 = st.radio("**파트너는 믿을 수 있는 수준으로 역할을 수행했다.**", scale5, index=None, key="trust_R2")
    trust_R3 = st.radio("**파트너는 동일한 상황에서 일관된 방식으로 반응했다.**", scale5, index=None, key="trust_R3")
    trust_R4 = st.radio("**나는 파트너가 제 역할을 제대로 해낼 것이라고 믿었다.**", scale5, index=None, key="trust_R4")
    trust_R5 = st.radio("**파트너는 문제를 일관된 방식으로 분석했다.**", scale5, index=None, key="trust_R5")

    st.markdown("<br>", unsafe_allow_html=True)
    trust_T1 = st.radio("**파트너는 의사결정에 있어 적절한 방법을 사용했다.**", scale5, index=None, key="trust_T1")
    trust_T2 = st.radio("**파트너는 이 유형의 과제에 대해 충분한 지식을 갖추고 있었다.**", scale5, index=None, key="trust_T2")
    trust_T3 = st.radio("**파트너가 제시하는 의견은 매우 유능한 사람이 제시하는 것만큼 훌륭했다.**", scale5, index=None, key="trust_T3")
    trust_T4 = st.radio("**파트너는 내가 제공한 정보를 정확하게 활용했다.**", scale5, index=None, key="trust_T4")
    trust_T5 = st.radio("**파트너는 가용한 모든 지식과 정보를 활용하여 해결책을 제시했다.**", scale5, index=None, key="trust_T5")

    st.markdown("<br>", unsafe_allow_html=True)
    trust_U1 = st.radio("**나는 파트너가 어떻게 행동하는지 이해하기 때문에, 다음에 어떻게 반응할지 예측할 수 있었다.**", scale5, index=None, key="trust_U1")
    trust_U2 = st.radio("**나는 파트너가 내 의사결정 과정에서 어떻게 도움을 줄지 이해하고 있었다.**", scale5, index=None, key="trust_U2")
    trust_U3 = st.radio("**파트너가 정확히 어떻게 생각하는지는 몰라도, 의사결정에 어떻게 활용하면 되는지는 알았다.**", scale5, index=None, key="trust_U3")
    trust_U4 = st.radio("**파트너가 무엇을 하고 있는지 파악하기 쉬웠다.**", scale5, index=None, key="trust_U4")
    trust_U5 = st.radio("**파트너에게서 내가 필요한 의견을 얻으려면 어떻게 해야 하는지 알고 있었다.**", scale5, index=None, key="trust_U5")

    st.markdown("<br>", unsafe_allow_html=True)
    trust_F1 = st.radio("**파트너의 의견이 확실히 옳은지 모르더라도 나는 그것을 신뢰했다.**", scale5, index=None, key="trust_F1")
    trust_F2 = st.radio("**의사결정이 불확실할 때, 나는 내 판단보다 파트너의 의견을 더 신뢰했다.**", scale5, index=None, key="trust_F2")
    trust_F3 = st.radio("**결정에 확신이 서지 않을 때, 나는 파트너가 최선의 해결책을 제시할 것이라 믿었다.**", scale5, index=None, key="trust_F3")
    trust_F4 = st.radio("**파트너가 예상치 못한 의견을 제시하더라도, 그것이 옳다고 믿었다.**", scale5, index=None, key="trust_F4")
    trust_F5 = st.radio("**근거가 없어도, 파트너가 어려운 문제를 해결할 수 있다고 확신했다.**", scale5, index=None, key="trust_F5")

    st.markdown("<br>", unsafe_allow_html=True)
    trust_P1 = st.radio("**만약 파트너를 더 이상 함께할 수 없게 된다면 상실감을 느낄 것이다.**", scale5, index=None, key="trust_P1")
    trust_P2 = st.radio("**나는 파트너와 협업하는 것에 유대감을 느꼈다.**", scale5, index=None, key="trust_P2")
    trust_P3 = st.radio("**파트너는 내 의사결정 방식에 잘 맞았다.**", scale5, index=None, key="trust_P3")
    trust_P4 = st.radio("**나는 파트너와 함께 의사결정을 내리는 것이 좋았다.**", scale5, index=None, key="trust_P4")
    trust_P5 = st.radio("**나는 파트너와 함께 의사결정을 내리는 것을 개인적으로 선호한다.**", scale5, index=None, key="trust_P5")

    # ─────────────────────────────────────────
    # ── 팀 인식 (Team Perception, 5문항)
    # ─────────────────────────────────────────
    st.divider()
    st.subheader("3. 팀 인식")
    st.caption("다음 문항은 파트너와의 협업에서 팀으로서의 경험을 묻는 문항입니다.")
    team1 = st.radio("**나는 파트너와 하나의 팀의 일원이라고 느꼈다.**", scale5, index=None, key="team1")
    team2 = st.radio("**나는 파트너를 협업 파트너로 인식했다.**", scale5, index=None, key="team2")
    team3 = st.radio("**나는 파트너와 함께 협력하며 과제를 수행했다고 느꼈다.**", scale5, index=None, key="team3")
    team4 = st.radio("**나는 파트너와 함께 일했다는 느낌을 받았다.**", scale5, index=None, key="team4")
    team5 = st.radio("**파트너와 나는 따로가 아니라 하나의 팀으로 움직였다.**", scale5, index=None, key="team5")

    # ─────────────────────────────────────────
    # ── 만족도 (6문항)
    # ─────────────────────────────────────────
    st.divider()
    st.subheader("4. 협업 만족도")
    sat1 = st.radio("**전반적으로 이번 협업에 만족한다.**", scale5, index=None, key="sat1")
    sat2 = st.radio("**나는 파트너의 기여에 만족한다.**", scale5, index=None, key="sat2")
    sat3 = st.radio("**우리의 협업 과정에는 개선될 수 있는 부분이 있다고 느꼈다.**", scale5, index=None, key="sat3")
    sat4 = st.radio("**이번 협업 경험은 긍정적이었다.**", scale5, index=None, key="sat4")
    sat5 = st.radio("**지난 협업 경험과 비교했을 때, 이번 협업은 전반적으로 만족스러웠다.**", scale5, index=None, key="sat5")
    sat6 = st.radio("**나의 파트너도 이번 협업을 긍정적으로 느낄 것이다.**", scale5, index=None, key="sat6")

    # ─────────────────────────────────────────
    # ── 협업 성과 (주관)
    # ─────────────────────────────────────────
    st.divider()
    st.subheader("5. 협업 성과")
    perf1 = st.radio("**우리 팀은 매우 생산적이었다.**", scale5, index=None, key="perf1")
    perf2 = st.radio("**우리 팀은 양질의 업무를 수행했다.**", scale5, index=None, key="perf2")
    perf3 = st.radio("**우리 팀은 과제 목표를 달성했다.**", scale5, index=None, key="perf3")
    perf_self = st.slider(
        "**전반적으로 이번 협업의 결과물(기획안)의 완성도를 0~100점으로 평가해 주십시오.**",
        min_value=0, max_value=100, value=50, step=1
    )

    # ─────────────────────────────────────────
    # ── 토픽 민감도
    # ─────────────────────────────────────────
    st.divider()
    st.subheader("6. 토픽 민감도")
    topic_sensitivity = st.radio(
        "**귀하는 식단 관리나 다이어트에 얼마나 관심이 있으십니까?**",
        ["전혀 관심 없다", "별로 관심 없다", "보통이다", "약간 관심 있다", "매우 관심 있다"],
        index=None, key="topic_sensitivity"
    )

    # ─────────────────────────────────────────
    # ── 제출
    # ─────────────────────────────────────────
    st.divider()
    if st.button("설문 제출 →"):

        required = [
            mc_partner,
            # 신뢰 25문항
            trust_R1, trust_R2, trust_R3, trust_R4, trust_R5,
            trust_T1, trust_T2, trust_T3, trust_T4, trust_T5,
            trust_U1, trust_U2, trust_U3, trust_U4, trust_U5,
            trust_F1, trust_F2, trust_F3, trust_F4, trust_F5,
            trust_P1, trust_P2, trust_P3, trust_P4, trust_P5,
            # 팀 인식 5문항
            team1, team2, team3, team4, team5,
            # 만족도
            sat1, sat2, sat3, sat4, sat5, sat6,
            # 성과
            perf1, perf2, perf3,
            # 토픽 민감도
            topic_sensitivity,
        ]
        if any(v is None for v in required) or not kakao_id.strip():
            st.error("⚠️ 응답하지 않은 항목이 있습니다. 카카오톡 아이디와 모든 항목을 입력해야 제출할 수 있습니다.")
            st.stop()

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        sheets_append(survey_ws, [
            timestamp,
            st.session_state.user_id,
            st.session_state.room_id,
            st.session_state.condition,
            st.session_state.role,
            # 조작점검
            mc_partner,
            # 신뢰 – Reliability
            trust_R1, trust_R2, trust_R3, trust_R4, trust_R5,
            # 신뢰 – Technical Competence
            trust_T1, trust_T2, trust_T3, trust_T4, trust_T5,
            # 신뢰 – Understandability
            trust_U1, trust_U2, trust_U3, trust_U4, trust_U5,
            # 신뢰 – Faith
            trust_F1, trust_F2, trust_F3, trust_F4, trust_F5,
            # 신뢰 – Personal Attachment
            trust_P1, trust_P2, trust_P3, trust_P4, trust_P5,
            # 팀 인식
            team1, team2, team3, team4, team5,
            # 만족도
            sat1, sat2, sat3, sat4, sat5, sat6,
            # 성과
            perf1, perf2, perf3, perf_self,
            # 토픽 민감도
            topic_sensitivity,
            # 카카오톡 아이디
            kakao_id.strip(),
        ])

        go("done")

# ─────────────────────────────────────────
# 14. 완료 화면
# ─────────────────────────────────────────
elif st.session_state.phase == "done":

    st.title("🎉 실험 완료")
    st.success("설문까지 모두 완료하셨습니다. 참여해주셔서 감사드립니다! 🙇‍♀️")
    st.markdown(f"""
**참여자 ID**: `{st.session_state.user_id}`
💵 보상 지급을 위해 위의 참여자 ID를 연구자 카카오톡으로 제출해 주세요.

참여 보상은 연구팀에서 데이터 확인 후, 카카오톡(개인톡)을 통해 지급해 드릴 예정입니다.
문의사항은 아래 이메일 또는 카카오톡으로 연락해 주세요.

📧 연구자: 노단 (고려대학교 박사과정) | dandandan1002@gmail.com | 카카오톡 ID: dandan_dan
""")
