# 채권 시장 모니터

국채·회사채 금리 추이와 공기업 발행 실적을 한 화면에 보여주는 HTML 대시보드입니다.

## 파일 구조

```
auto_project_rate/
├── update.py       ← 매일 실행하는 스크립트
├── index.html      ← 생성되는 대시보드 (브라우저로 열기)
├── requirements.txt
└── README.md
```

## 처음 설치

```bash
pip3 install requests
```

## 매일 업데이트하기

```bash
python3 update.py
```

실행하면 `index.html` 이 생성됩니다. 브라우저에서 파일을 열면 됩니다.

---

## 자동 실행 설정 (macOS cron)

매일 오전 9시에 자동으로 실행하려면:

```bash
crontab -e
```

아래 한 줄을 추가하세요 (경로는 본인 경로로 수정):

```
0 9 * * 1-5 cd /Users/kimseonhyeok/auto_project_rate && python3 update.py >> cron.log 2>&1
```

- `1-5` = 월~금 평일만 실행
- `>> cron.log` = 실행 로그를 cron.log 파일에 저장

---

## ECOS API 키 발급 (선택사항)

현재는 sample 키를 사용해 최근 10영업일 데이터만 가져옵니다.
더 긴 기간 데이터가 필요하면:

1. https://ecos.bok.or.kr 접속 → 회원가입 → Open API 신청 (무료)
2. `update.py` 첫 부분의 `ECOS_API_KEY = "sample"` 을 발급받은 키로 교체
3. `LOOKBACK_DAYS` 값도 늘릴 수 있습니다

---

## 한전채 민평금리

금융투자협회(KOFIA)가 매일 공시하는 민평금리는 자동 수집이 불가능합니다.  
아래 링크에서 직접 확인하세요:

👉 https://www.kofiabond.or.kr → 채권 시가평가 → 시가평가수익률

---

## 공기업 발행 실적 (향후 자동화)

현재는 예시 데이터입니다. 실제 데이터 출처:
- **DART**: https://dart.fss.or.kr (금융감독원 전자공시)
- **SEIBRO**: https://seibro.or.kr (한국예탁결제원)

---

## 다른 사람과 공유하기

생성된 `index.html` 파일을 GitHub Pages, Netlify, 또는 구글 드라이브로 공유하면  
다른 사람도 URL로 볼 수 있습니다.

**GitHub Pages로 무료 호스팅:**
1. GitHub에 리포지토리 생성
2. `index.html` 커밋 & 푸시
3. Settings → Pages → main 브랜치 선택
4. 자동으로 URL이 생성됩니다
