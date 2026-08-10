import pickle
import requests
from datetime import datetime, timezone, timedelta
import os

KST = timezone(timedelta(hours=9))

def test_google_fit():
    if not os.path.exists('token.pickle'):
        print("❌ token.pickle 파일이 없습니다! 계정 인증부터 다시 해야 합니다.")
        return

    with open('token.pickle', 'rb') as token:
        creds = pickle.load(token)
    
    now = datetime.now(KST)
    start_time = now - timedelta(days=2)
    
    headers = {"Authorization": f"Bearer {creds.token}"}
    
    # 💡 activityType(72) 필터를 아예 빼버리고 모든 세션을 가져옵니다.
    response = requests.get(
        "https://www.googleapis.com/fitness/v1/users/me/sessions",
        headers=headers,
        params={
            "startTime": start_time.astimezone(timezone.utc).isoformat(),
            "endTime": now.astimezone(timezone.utc).isoformat(),
        }
    )
    
    if response.status_code != 200:
        print(f"❌ API 에러: {response.status_code} - {response.text}")
        return
        
    sessions = response.json().get("session", [])
    print(f"✅ 구글 서버에서 총 {len(sessions)}개의 데이터를 찾았습니다!\n")
    
    for s in sessions:
        a_type = s.get('activityType')
        s_time = datetime.fromtimestamp(int(s['startTimeMillis'])/1000, tz=KST).strftime('%Y-%m-%d %H:%M')
        print(f"- 종류: {a_type} (72번이 수면) / 시간: {s_time} / 이름: {s.get('name', '이름없음')}")

if __name__ == "__main__":
    test_google_fit()
