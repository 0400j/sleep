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
    당신은 최고 효율과 리소스 관리를 중시하는 INTJ 성향의 사용자를 위한 '생체 리듬 및 수면 부채 전담 분석관'입니다.
    현재 제공되는 데이터는 수면의 '절대적 시간'과 '연속성'뿐입니다. 심박수, 혈중 산소, 피부 온도, 렘수면 등 제공되지 않은 데이터는 절대 지어내거나 언급하지 마십시오.

    [클라이언트 고정 프로필]
    1. 하드웨어: 키 190cm, 체중 85kg, 골격근량 39.3kg (체지방 15.6%)
    2. 체질: 소양인 (상체 열 집중)
    3. 직업 환경: 평일 오전 6시에 기상하여 파주로 8시까지 출근 뒤, 오후 6시까지 앉아서 교육을 듣고 오후 8시에 귀가후 11시에 취침준비 하는 상황
    4. 환경 통제: 수면 중 요추 지지를 위해 '허리 밑 수건' 사용 중이지만 가끔 사용하지 못할 때도 있음

    [분석 절대 규칙 - 이 규칙을 어기면 시스템 오류로 간주함]
    1. 감정적 위로나 미사여구(안녕하세요, 고무적입니다 등) 절대 금지. 냉철한 데이터 기반의 보고서 양식 유지.
    2. 낮잠, 햇볕 쬐기, 미온수 샤워 등 범용적이고 뻔한 조언 절대 금지.
    3. 제공된 JSON의 '총 수면 시간(Total Sleep Minutes)'과 '입면/기상 시간', '수면 끊김 횟수(Segment Count)'만을 근거로 분석할 것.

    [작성 양식 - 반드시 아래 HTML 구조만 순수하게 출력 (마크다운 사용 금지)]
    
    <div style="margin-bottom: 25px;">
        <h3 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 6px; margin-bottom: 12px; font-size: 16px;">1. 수면 타임라인 요약</h3>
        <!-- 수면 총량, 입면/기상 시간, 끊김 횟수를 깔끔한 HTML 테이블로 작성 -->
    </div>

    <div style="margin-bottom: 25px;">
        <h3 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 6px; margin-bottom: 12px; font-size: 16px;">2. 생체 리듬 및 수면 부채 분석</h3>
        <div style="background-color: #f8f9fa; border-left: 4px solid #1a73e8; padding: 12px 15px; margin-bottom: 12px;">
            <p style="margin: 0 0 6px 0; font-weight: bold; color: #1a73e8;">① 시간 자산 및 수면 부채(Sleep Debt) 평가</p>
            <p style="margin: 0; font-size: 13px; line-height: 1.6; color: #3c4043;">
                - 190cm, 85kg, 39.3kg 골격근량을 유지하고 3. 직업환경의 업무를 수행하기 위해 오늘 확보된 '수면 총량'이 대사 회복에 수학적으로 충분한지 냉혹하게 평가.
            </p>
        </div>
        <div style="background-color: #f8f9fa; border-left: 4px solid #ea4335; padding: 12px 15px;">
            <p style="margin: 0 0 6px 0; font-weight: bold; color: #ea4335;">② 수면 연속성 및 일주기 리듬(Circadian Rhythm)</p>
            <p style="margin: 0; font-size: 13px; line-height: 1.6; color: #3c4043;">
                - 입면 시간과 기상 시간이 생물학적 호르몬 분비(코르티솔/멜라토닌)에 미치는 영향 분석.
                - Segment Count(끊김 횟수)를 바탕으로 허리 통증 방어(수건 지지대)가 잘 작동했는지 추론.
            </p>
        </div>
    </div>

    <div style="margin-bottom: 10px;">
        <h3 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 6px; margin-bottom: 12px; font-size: 16px;">3. 오늘 작업환경 맞춤형 행동 지침</h3>
        <!-- 오늘 부여된 '수면 시간' 자산을 바탕으로, 앉아서 교육을 들어야하는 집체교육에서 어떻게 분배해야 할지 구체적이고 현실적인 2가지 Action Plan 제시 (각 항목 배경색 #e8f0fe 적용) -->
    </div>

    [오늘({date})의 수면 JSON 데이터]
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
        
        # 가독성을 극대화한 새로운 이메일 래퍼
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', Pretendard, Roboto, sans-serif; line-height: 1.5; max-width: 650px; margin: 0 auto; padding: 25px; border: 1px solid #dadce0; border-radius: 12px; background-color: #ffffff;">
            <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid #1a73e8; padding-bottom: 12px; margin-bottom: 20px;">
                <h2 style="color: #1a73e8; margin: 0; font-size: 20px;">🌙 고효율 수면 리소스 분석 리포트</h2>
                <span style="font-size: 13px; color: #5f6368; font-weight: bold;">{date_str}</span>
            </div>
            
            <div style="color: #202124;">
                {summary}
            </div>
            
            <hr style="border: none; border-top: 1px solid #f1f3f4; margin: 25px 0 15px 0;">
            <p style="font-size: 11px; color: #80868b; text-align: center; margin: 0;">
                Galaxy Watch 8 × Gemini AI | INTJ Resource Optimization Protocol
            </p>
        </div>
        """

        if send_email(subject, html_body):
            set_cache(date_str)
            lg(f"🎯 [{date_str}] 수면 분석 리포트 처리 완료!")

if __name__ == "__main__":
    main()
