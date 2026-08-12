import os
import time
import pickle
import sqlite3
import requests
import json
import smtplib
import re
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from google import genai
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# 💡 한국 시간대(KST = UTC+9) 고정 설정 (UTC 시간 왜곡 완전 해결)
KST = timezone(timedelta(hours=9))

# 1. 환경변수 로드
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MAIL_FROM = os.getenv("MAIL_FROM_ADDRESS")
MAIL_PASS = os.getenv("MAIL_PASSWORD") 
MAIL_TO_LIST = [
    os.getenv("MAIL_TO"),
    os.getenv("MAIL_TO2")
]
# 값이 없는(None) 이메일은 리스트에서 자동 제거
MAIL_TO_LIST = [mail for mail in MAIL_TO_LIST if mail]

# 2. 캐시 DB 세팅 (파일형 SQLite - 날짜별 중복 발송 방지)
conn = sqlite3.connect('sleep_cache.db')
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS sleep_cache (date_str TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
conn.commit()

def is_cached(date_str):
    cursor.execute('SELECT 1 FROM sleep_cache WHERE date_str = ?', (date_str,))
    return cursor.fetchone() is not None

def set_cache(date_str):
    cursor.execute('INSERT OR IGNORE INTO sleep_cache (date_str) VALUES (?)', (date_str,))
    conn.commit()

def lg(msg):
    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] ⚡ {msg}")

# 3. Google Fit API 연동 및 수면 데이터 수집 로직
SCOPES = ['https://www.googleapis.com/auth/fitness.sleep.read']

def get_fit_credentials():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
            
    return creds

def time_to_rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat()

def fetch_sleep_json():
    try:
        creds = get_fit_credentials()
        
        now = datetime.now(KST)
        # 최근 2일(48시간) 전부터 넉넉하게 모든 수면 데이터를 조회
        search_start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        headers = {"Authorization": f"Bearer {creds.token}"}
        response = requests.get(
            "https://www.googleapis.com/fitness/v1/users/me/sessions",
            headers=headers,
            params={
                "startTime": time_to_rfc3339(search_start),
                "endTime": time_to_rfc3339(now),
                "activityType": 72  # 72 = 수면(Sleep)
            }
        )
        
        if response.status_code != 200:
            lg(f"Fit API 호출 실패: {response.status_code} - {response.text}")
            return None
            
        data = response.json()
        
        # 💡 여기가 수정된 부분입니다! 가져온 데이터를 일단 'raw_sessions'에 담습니다.
        raw_sessions = data.get("session", [])
        
        if not raw_sessions:
            return None

        # 💡 48시간 데이터 중 '최근 24시간 이내' 데이터만 필터링
        cutoff_time = now - timedelta(hours=24)
        sessions = []
        for s in raw_sessions:
            s_end = datetime.fromtimestamp(int(s["endTimeMillis"]) / 1000, tz=KST)
            # 수면 종료 시간이 최근 24시간 이내인 것만 '진짜 오늘 수면'으로 인정
            if s_end > cutoff_time:
                sessions.append(s)

        # 필터링 후 오늘 수면이 없다면 종료
        if not sessions:
            return None

        # 💡 전체 수면 세션 합산 로직 (KST 시간대로 완벽 파싱)
        total_minutes = 0
        first_start_ms = int(sessions[0]["startTimeMillis"])
        last_end_ms = int(sessions[-1]["endTimeMillis"])
        
        session_segments = []
        for s in sessions:
            s_start = int(s["startTimeMillis"])
            s_end = int(s["endTimeMillis"])
            duration = int((s_end - s_start) / (1000 * 60))
            total_minutes += duration
            
            # UTC -> KST 한국 표준시로 명확히 변환
            dt_start = datetime.fromtimestamp(s_start / 1000, tz=KST)
            dt_end = datetime.fromtimestamp(s_end / 1000, tz=KST)
            
            session_segments.append({
                "start": dt_start.strftime('%p %I:%M'),
                "end": dt_end.strftime('%p %I:%M'),
                "minutes": duration
            })
        
        hours = total_minutes // 60
        minutes = total_minutes % 60
        
        # 최초 입면 및 최종 기상 시간도 KST 기준으로 포맷팅
        start_dt = datetime.fromtimestamp(first_start_ms / 1000, tz=KST).strftime('%Y-%m-%d %p %I:%M')
        end_dt = datetime.fromtimestamp(last_end_ms / 1000, tz=KST).strftime('%Y-%m-%d %p %I:%M')
        
        return {
            "date": now.strftime('%Y-%m-%d'),
            "sleep_start": start_dt,                  # 어젯밤 최초 입면 시간 (KST)
            "sleep_end": end_dt,                      # 오늘 아침 최종 기상 시간 (KST)
            "total_sleep_minutes": total_minutes,     # 깬 시간 제외한 순수 수면 시간 총합
            "duration_formatted": f"{hours}시간 {minutes}분",
            "sleep_segment_count": len(sessions),     # 잠이 끊긴 횟수
            "segments_detail": session_segments       # 수면 조각별 세부 내역
        }
    except Exception as e:
        lg(f"수면 데이터 수집 에러: {e}")
        return None

# 💡 AI 응답에서 ```html ... ``` 껍데기와 불필요한 서론/결론 텍스트를 제거하는 클리닝 함수
def clean_html_response(text):
    if not text:
        return ""
    # ```html 과 ``` 사이의 실제 HTML 본문만 정규식으로 추출
    match = re.search(r"```(?:html)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()

# 4. AI 수면 분석 로직 (인사말/중복 타이틀 금지 프롬프트 적용)
def analyze_sleep_with_ai(sleep_data):
    date = sleep_data.get('date')
    sleep_str = json.dumps(sleep_data, ensure_ascii=False, indent=2)
    
    prompt = f"""
    당신은 세계 최고 수준의 수면 의학 전문가이자 개인 수면 전담 코치입니다.
    사용자의 갤럭시워치에서 측정되어 Google Fit을 통해 수집된 오늘({date})의 수면 데이터를 분석하고,
    오늘의 컨디션 예측과 개선 방안을 HTML 본문으로 작성해 주세요.

    [중요: 출력 규칙 - 절대 위반 금지!]
    1. "안녕하세요", "분석해 드립니다" 같은 인사말이나 서론/결론 텍스트를 절대 쓰지 마세요.
    2. 보고서 제목(예: "오늘의 수면 분석 리포트")은 이메일 템플릿에 이미 있으므로 본문에 다시 쓰지 마세요.
    3. 마크다운 코드블록(```html)을 씌우지 말고, 순수하게 HTML 태그(<p>, <h3>, <ul>, <li>, <b> 등)로만 구성된 본문 내용만 출력하세요.
    4. 친근하면서도 전문성 있고 따뜻한 어조로 작성해 주세요.

    [수면 JSON 데이터 (한국 표준시 KST 기준)]
    {sleep_str}
    """
    
    fallback_models = [
        'gemma-4-26b-a4b-it',
        'gemini-2.0-flash',
        'gemini-3.0-flash',
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-1.5-flash'
    ]
    
    client = genai.Client(api_key=API_KEY)

    for model_name in fallback_models:
        try:
            lg(f"🤖 [{model_name}] 모델로 수면 분석 시도 중...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            lg(f"✅ [{model_name}] 수면 분석 성공!")
            
            # 후처리를 통해 깨끗한 순수 HTML 본문만 반환
            return clean_html_response(response.text)
            
        except Exception as e:
            error_msg = str(e).lower()
            if any(k in error_msg for k in ["429", "quota", "exhausted", "404", "not_found", "not found"]):
                lg(f"⚠️ [{model_name}] 이용 불가 또는 할당량 초과! 2초 대기 후 다음 모델로 교체합니다.")
                time.sleep(2)
                continue
            else:
                lg(f"⚠️ [{model_name}] 분석 실패: {e}")
                time.sleep(2)
                continue

    lg("🚨 모든 모델의 할당량이 고갈되었거나 사용할 수 있는 모델이 없습니다.")
    return None

# 5. 이메일 발송 로직
def send_email(subject, html_body):
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(MAIL_FROM, MAIL_PASS)

        for recipient in MAIL_TO_LIST:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = MAIL_FROM
            msg['To'] = recipient

            part = MIMEText(html_body, 'html')
            msg.attach(part)
            
            server.sendmail(MAIL_FROM, recipient, msg.as_string())
            lg(f"📧 메일 발송 완료: {recipient}")

        server.quit()
        return True
    except Exception as e:
        lg(f"메일 발송 에러: {e}")
        return False

# 6. 메인 실행 로직
def main():
    lg("수면 분석 파이프라인 시작...")
    
    sleep_data = fetch_sleep_json()
    if not sleep_data:
        lg("어제/오늘의 수면 데이터가 없습니다. 워치 동기화 여부를 확인해 주세요.")
        return
        
    date_str = sleep_data['date']
    
    # 당일 중복 발송 방지
    if is_cached(date_str):
        lg(f"CACHED! [{date_str}] 오늘의 수면 분석 리포트가 이미 발송되었습니다.")
        return

    summary = analyze_sleep_with_ai(sleep_data)
    
    if summary:
        subject = f"🌙 [수면 리포트] {date_str} 분석 결과 및 오늘 컨디션 예측"
        
        # 중복 제목/인사말 없이 본문만 흰색 카드 컨테이너 안에 쏙 들어가는 깔끔한 디자인
        html_body = f"""
        <div style="font-family: 'Apple SD Gothic Neo', Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 10px;">
            <h2 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 10px; margin-top: 0;">🌙 오늘의 수면 분석 리포트 ({date_str})</h2>
            <div style="background-color: #ffffff; padding: 10px 5px; color: #333333;">
                {summary}
            </div>
            <hr style="border: none; border-top: 1px solid #eeeeee; margin: 20px 0;">
            <p style="font-size: 12px; color: #888888; text-align: center;">
                Galaxy Watch 8 × Gemini AI Automated Sleep Coach
            </p>
        </div>
        """

        if send_email(subject, html_body):
            set_cache(date_str)
            lg(f"🎯 [{date_str}] 수면 분석 리포트 처리 완료!")

if __name__ == "__main__":
    main()
