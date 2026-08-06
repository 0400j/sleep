import os
import time
import pickle
import sqlite3
import requests
import json
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from google import genai
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

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
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
    return dt.astimezone().isoformat()

def fetch_sleep_json():
    try:
        creds = get_fit_credentials()
        
        now = datetime.now().astimezone()
        yesterday_evening = (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        
        headers = {"Authorization": f"Bearer {creds.token}"}
        response = requests.get(
            "https://www.googleapis.com/fitness/v1/users/me/sessions",
            headers=headers,
            params={
                "startTime": time_to_rfc3339(yesterday_evening),
                "endTime": time_to_rfc3339(now),
                "activityType": 72  # 72 = 수면(Sleep)
            }
        )
        
        if response.status_code != 200:
            lg(f"Fit API 호출 실패: {response.status_code} - {response.text}")
            return None
            
        data = response.json()
        sessions = data.get("session", [])
        
        if not sessions:
            return None

        # 💡 전체 수면 세션 합산 로직 (분절 수면 완벽 처리)
        total_minutes = 0
        first_start_ms = int(sessions[0]["startTimeMillis"])
        last_end_ms = int(sessions[-1]["endTimeMillis"])
        
        session_segments = []
        for s in sessions:
            s_start = int(s["startTimeMillis"])
            s_end = int(s["endTimeMillis"])
            duration = int((s_end - s_start) / (1000 * 60))
            total_minutes += duration
            
            session_segments.append({
                "start": datetime.fromtimestamp(s_start / 1000).strftime('%H:%M'),
                "end": datetime.fromtimestamp(s_end / 1000).strftime('%H:%M'),
                "minutes": duration
            })
        
        hours = total_minutes // 60
        minutes = total_minutes % 60
        
        start_dt = datetime.fromtimestamp(first_start_ms / 1000).strftime('%Y-%m-%d %H:%M')
        end_dt = datetime.fromtimestamp(last_end_ms / 1000).strftime('%Y-%m-%d %H:%M')
        
        return {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "sleep_start": start_dt,                  # 어젯밤 최초 입면 시간
            "sleep_end": end_dt,                      # 오늘 아침 최종 기상 시간
            "total_sleep_minutes": total_minutes,     # 깬 시간 제외한 순수 수면 시간 총합
            "duration_formatted": f"{hours}시간 {minutes}분",
            "sleep_segment_count": len(sessions),     # 잠이 끊긴 횟수 (1이면 통잠, 2 이상이면 분절 수면)
            "segments_detail": session_segments       # 수면 조각별 세부 내역
        }
    except Exception as e:
        lg(f"수면 데이터 수집 에러: {e}")
        return None
        
# 4. AI 수면 분석 로직 (scraping.py의 fallback 돌려막기 구조 100% 적용)
def analyze_sleep_with_ai(sleep_data):
    date = sleep_data.get('date')
    sleep_str = json.dumps(sleep_data, ensure_ascii=False, indent=2)
    
    prompt = f"""
    당신은 세계 최고 수준의 수면 의학 전문가이자 개인 수면 전담 코치입니다.
    사용자의 갤럭시워치에서 측정되어 Google Fit을 통해 수집된 오늘({date})의 수면 데이터를 분석하고,
    오늘의 컨디션 예측과 개선 방안을 이메일로 읽기 쉽게 보고서 형식으로 써주세요.

    [요청 사항]
    1. 총 수면 시간 및 입면/기상 시간 분석: 적정 수면 시간과 비교해 어땠는지 알기 쉽게 설명해 주세요.
    2. 오늘의 컨디션 예측: 오늘의 수면 결과가 낮 시간의 업무 집중도나 체력에 미칠 영향을 예측해 주세요.
    3. 수면 코칭 액션 아이템: 오늘 밤 더 깊고 질 좋은 수면을 위해 실천할 수 있는 구체적인 행동 1~2가지를 제시해 주세요.
    4. 친근하면서도 전문성 있고 따뜻한 어조로 작성해 주세요.
    5. HTML 이메일 본문에 바로 들어갈 수 있도록 <p>, <h3>, <ul>, <li>, <b> 태그 등을 적절히 활용해 예쁘게 작성해 주세요.

    [수면 JSON 데이터]
    {sleep_str}
    """
    
    # 💡 돌려막기(Fallback)할 모델 리스트 (현재 확실히 열려있는 2.0-flash를 1순위로 추가)
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
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # API 에러 메시지에 429, Quota, Exhausted, 404, Not Found 등의 단어가 있으면 다음 모델로 자동 넘김!
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

# 5. 이메일 발송 로직 (scraping.py의 smtplib.SMTP_SSL 구조 100% 동일)
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
        
        html_body = f"""
        <div style="font-family: 'Apple SD Gothic Neo', Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 10px;">
            <h2 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 10px;">🌙 오늘의 수면 분석 리포트 ({date_str})</h2>
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin: 15px 0; color: #333333;">
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