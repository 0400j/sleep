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

KST = timezone(timedelta(hours=9))

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MAIL_FROM = os.getenv("MAIL_FROM_ADDRESS")
MAIL_PASS = os.getenv("MAIL_PASSWORD") 
MAIL_TO_LIST = [mail for mail in [os.getenv("MAIL_TO"), os.getenv("MAIL_TO2")] if mail]

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
        search_start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        headers = {"Authorization": f"Bearer {creds.token}"}
        response = requests.get(
            "https://www.googleapis.com/fitness/v1/users/me/sessions",
            headers=headers,
            params={
                "startTime": time_to_rfc3339(search_start),
                "endTime": time_to_rfc3339(now),
                "activityType": 72
            }
        )
        
        if response.status_code != 200:
            lg(f"Fit API 호출 실패: {response.status_code} - {response.text}")
            return None
            
        data = response.json()
        raw_sessions = data.get("session", [])
        
        if not raw_sessions:
            return None

        cutoff_time = now - timedelta(hours=24)
        sessions = [s for s in raw_sessions if datetime.fromtimestamp(int(s["endTimeMillis"]) / 1000, tz=KST) > cutoff_time]

        if not sessions:
            return None

        total_minutes = 0
        first_start_ms = int(sessions[0]["startTimeMillis"])
        last_end_ms = int(sessions[-1]["endTimeMillis"])
        
        session_segments = []
        for s in sessions:
            s_start = int(s["startTimeMillis"])
            s_end = int(s["endTimeMillis"])
            duration = int((s_end - s_start) / (1000 * 60))
            total_minutes += duration
            
            dt_start = datetime.fromtimestamp(s_start / 1000, tz=KST)
            dt_end = datetime.fromtimestamp(s_end / 1000, tz=KST)
            
            session_segments.append({
                "start": dt_start.strftime('%p %I:%M'),
                "end": dt_end.strftime('%p %I:%M'),
                "minutes": duration
            })
        
        hours = total_minutes // 60
        minutes = total_minutes % 60
        start_dt = datetime.fromtimestamp(first_start_ms / 1000, tz=KST).strftime('%Y-%m-%d %p %I:%M')
        end_dt = datetime.fromtimestamp(last_end_ms / 1000, tz=KST).strftime('%Y-%m-%d %p %I:%M')
        
        return {
            "date": now.strftime('%Y-%m-%d'),
            "sleep_start": start_dt,
            "sleep_end": end_dt,
            "total_sleep_minutes": total_minutes,
            "duration_formatted": f"{hours}시간 {minutes}분",
            "sleep_segment_count": len(sessions),
            "segments_detail": session_segments
        }
    except Exception as e:
        lg(f"수면 데이터 수집 에러: {e}")
        return None

def clean_html_response(text):
    if not text:
        return ""
    match = re.search(r"```(?:html)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()

def analyze_sleep_with_ai(sleep_data):
    date = sleep_data.get('date')
    sleep_str = json.dumps(sleep_data, ensure_ascii=False, indent=2)
    
    prompt = f"""
당신은 세계 최고 수준의 수면 분석가이자 생체역학, 열역학 전문가입니다.
클라이언트는 아래와 같은 명확한 신체적, 성향적 특성을 가집니다. 분석 시 이 데이터를 반드시 인과관계의 근거로 사용하십시오.

[클라이언트 고정 프로필]
1. 하드웨어: 키 190cm, 체중 85kg, 골격근량 39.3kg (체지방 15.6%) / 의장대 출신의 고효율 실전형 근육 보유.
2. 체질: 소양인 (상체로 열이 쏠리기 쉽고 방열이 수면의 핵심 변수임).
3. 직업 환경: 1시간 30분 맞교대로 계속 서서 말하는 도슨트 업무 (하체 및 요추 과부하, 발성으로 인한 상열 발생).
4. 현재 물리적 통제 상황: 수면 중 요추 전만을 지지하기 위해 '허리 밑에 얇은 수건'을 깔고 자며 척추를 세팅 중.
5. 성향: INTJ. 합리성, 논리, 시간 가성비를 최우선으로 함.

[출력(분석) 절대 규칙]
1. 감정적인 위로나 미사여구를 절대 금지합니다.
2. "성인 권장 수면 시간", "낮잠 추천", "햇볕 쬐기" 같은 범용적인 조언을 절대 출력하지 마십시오.
3. 분석은 철저히 제공된 JSON 수치 데이터와 클라이언트의 '신체적 특성'을 결합한 생물학적/물리학적 인과관계로만 설명하십시오.
4. 문체는 냉철하고 단호하며, 데이터 기반의 논리적 추론 형태로 작성하십시오.
5. "안녕하세요", "분석해 드립니다" 같은 인사말이나 서론/결론 텍스트, 보고서 제목을 절대 쓰지 마세요.
6. 마크다운(##, **, ``` 등)을 절대 사용하지 말고, 순수하게 HTML 태그(<p>, <h3>, <ul>, <li>, <b> 등)로만 구성된 본문 내용만 출력하세요.

[필수 출력 포맷 (HTML 태그 사용)]
반드시 다음 3가지 섹션으로 나누어 작성하십시오.
<h3 style="color: #1a73e8;">1. 수면 지표 핵심 요약</h3> (데이터 객관적 평가)
<h3 style="color: #1a73e8;">2. 심층 데이터 분석 및 인과관계</h3> (생물학적 심층 분석)
<h3 style="color: #1a73e8;">3. 리소스 최적화 및 행동 지침</h3> (명확한 Action Plan 2~3가지)

[오늘({date})의 수면 JSON 데이터 (한국 표준시 KST 기준)]
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
            return clean_html_response(response.text)
            
        except Exception as e:
            error_msg = str(e).lower()
            if any(k in error_msg for k in ["429", "quota", "exhausted", "404", "not_found", "not found"]):
                lg(f"⚠️ [{model_name}] 이용 불가/할당량 초과. 다음 모델로 교체.")
                time.sleep(2)
                continue
            else:
                lg(f"⚠️ [{model_name}] 분석 실패: {e}")
                time.sleep(2)
                continue

    lg("🚨 가용 모델 없음.")
    return None

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

def main():
    lg("수면 분석 파이프라인 시작...")
    
    sleep_data = fetch_sleep_json()
    if not sleep_data:
        lg("수면 데이터 없음. 동기화 확인 요망.")
        return
        
    date_str = sleep_data['date']
    
    if is_cached(date_str):
        lg(f"CACHED! [{date_str}] 기발송 완료.")
        return

    summary = analyze_sleep_with_ai(sleep_data)
    
    if summary:
        subject = f"🌙 [수면 리포트] {date_str} 분석 결과 및 리소스 최적화 지침"
        
        html_body = f"""
        <div style="font-family: 'Apple SD Gothic Neo', Arial, sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 10px;">
            <h2 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 10px; margin-top: 0;">🌙 심층 수면 분석 리포트 ({date_str})</h2>
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
