import imaplib
import email
import os
import re
import json
import base64
import requests
import anthropic
from datetime import datetime, timezone, timedelta
from email.header import decode_header
from email.utils import parsedate
from bs4 import BeautifulSoup

# ── 환경변수 ──────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
ARCHIVE_BOT_TOKEN  = os.environ["ARCHIVE_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN           = os.environ["GH_TOKEN"]

GMAIL_LABEL = "04-2 BCG Report"
KST         = timezone(timedelta(hours=9))
REPO_NAME   = "RyanHSoo/hsbotboard"
ARCHIVE_URL = "https://ryanhsoo.github.io/hsbotboard/bcg.html"

# Claude 응답이 실패(메타코멘터리)임을 나타내는 패턴 (영어/한국어/일본어)
ERROR_PATTERNS = [
    # 영어
    "i appreciate your", "i must be transparent", "the email content provided is incomplete",
    "i cannot", "i don't have access", "i'm unable", "i am unable",
    "incomplete email", "no actual content", "the content you provided",
    "i need to inform", "unfortunately", "i apologize",
    # 한국어
    "죄송합니다", "제공해 주신", "이메일 내용이", "정확한 분석이", "불완전한",
    "본문이 없", "내용이 없", "확인이 필요", "원문을 제공", "전체 내용을",
    # 일본어
    "申し訳", "提供いただいた", "完全な情報", "正確な分析", "メールコンテンツ",
    "記事本文がない", "コンテンツは", "ご対応をお願い",
]


# ── Gmail 읽기 ─────────────────────────────────────────────────────────────────────────────────────────
def get_bcg_emails(target_date):
    """target_date: datetime (KST) — 해당 날짜 수신 이메일 반환"""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

    status, _ = mail.select(f'"{GMAIL_LABEL}"')
    if status != "OK":
        print("Label not found, using inbox")
        mail.select("inbox")

    date_since  = target_date.strftime("%d-%b-%Y")
    date_before = (target_date + timedelta(days=1)).strftime("%d-%b-%Y")
    _, message_ids = mail.search(None, f'(SINCE {date_since} BEFORE {date_before})')
    target_ids = message_ids[0].split()
    print(f"  Found {len(target_ids)} emails for {date_since}")

    emails = []
    for msg_id in target_ids:
        _, msg_data = mail.fetch(msg_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject_raw = msg.get("Subject", "(No Subject)")
        decoded_parts = decode_header(subject_raw)
        subject = ""
        for part, enc in decoded_parts:
            if isinstance(part, bytes):
                subject += part.decode(enc or "utf-8", errors="ignore")
            else:
                subject += part

        body_text    = ""
        report_titles = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                decoded = payload.decode("utf-8", errors="ignore")

                if content_type == "text/plain":
                    body_text += decoded[:5000]
                elif content_type == "text/html":
                    soup = BeautifulSoup(decoded, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a.get("href", "")
                        if "bcg.com" in href:
                            if any(p in href for p in ["/publications/", "/insights/",
                                                       "/capabilities/", "/industries/",
                                                       "/featured-insights/"]):
                                title = a.get_text(strip=True)
                                if 15 < len(title) < 200 and title not in report_titles:
                                    report_titles.append(title)
                    if not body_text:
                        for tag in soup(["script", "style", "nav", "footer", "header"]):
                            tag.decompose()
                        text = soup.get_text(separator="\n")
                        lines = [l.strip() for l in text.splitlines() if l.strip()]
                        body_text = "\n".join(lines)[:5000]
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body_text = payload.decode("utf-8", errors="ignore")[:5000]

        has_content = len(body_text.strip()) >= 500
        if not report_titles:
            report_titles = [subject]

        print(f"  Email: {subject}")
        print(f"  Mode: {'body summary' if has_content else 'web search'} | titles: {len(report_titles)}")

        emails.append({
            "subject":       subject,
            "body":          body_text,
            "has_content":   has_content,
            "report_titles": report_titles[:5],
        })

    mail.logout()
    return emails


# ── Claude 요약 ────────────────────────────────────────────────────────────────────────────────────────
def summarize_email(email_data):
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    subject = email_data["subject"]
    body    = email_data["body"]
    titles  = email_data["report_titles"]

    use_web_search = not email_data["has_content"]

    if not use_web_search:
        prompt = f"""You are a BCG report analyst for senior executives.

Summarize the BCG email content below.
Email subject: {subject}

EMAIL CONTENT:
{body}

Identify each distinct report or article and summarize each one.

STRICT FORMAT for each report:
📌 [EXACT original English title from the email — do NOT translate or paraphrase]
- 핵심 주제: (1-2줄)
- 인사이트:
  - (구체적 내용 1줄)
  - (구체적 내용 1줄)
  - (구체적 내용 1줄)
- 시사점: (2-3줄, 경영진 관점)

RULES:
- Start directly with the first report, no preamble
- Titles: use EXACT English title as it appears in the email content
- Descriptions: Korean only
- Include specific numbers/data when available
- Only summarize content actually in the email
"""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3500,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text.strip()

        # 응답이 메타코멘터리이면 web_search 모드로 재시도
        result_lower = result.lower()
        if any(p in result_lower for p in ERROR_PATTERNS) or len(result) < 80:
            print("  [WARN] body 요약 실패 (개시란 컨텐츠 불충분), web_search 모드로 재시도")
            use_web_search = True
        else:
            return result

    # web_search 모드 (원래 or 폴백)
    titles_text = "\n".join([f"- {t}" for t in titles])
    prompt = f"""You are a BCG report analyst for senior executives.

Search the web for each BCG report title below and summarize them.

Report titles:
{titles_text}

STRICT FORMAT:
📌 [EXACT original English title — do NOT translate or paraphrase]
- 핵심 주제: (1-2줄)
- 인사이트:
  - (구체적 내용 1줄)
  - (구체적 내용 1줄)
  - (구체적 내용 1줄)
- 시사점: (2-3줄, 경영진 관점)

If NOT found:
📌 [Report Title in English]
- 핵심 주제: (제목 기반 1줄)
- ⚠️ 검색 불가 - 원문 확인 필요

RULES: Start directly, titles in English, descriptions in Korean.
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    result = ""
    for block in response.content:
        if block.type == "text":
            result += block.text
    return result.strip()


# ── GitHub 업데이트 ──────────────────────────────────────────────────────────────────────────────
def fetch_json(url, headers, headers_raw):
    """SHA는 일반 API로, 내용은 raw API로 가져와 1MB 제한 우회."""
    sha  = requests.get(url, headers=headers).json()["sha"]
    data = json.loads(requests.get(url, headers=headers_raw).text)
    return data, sha

def update_board(entries, target_date, board_date):
    headers     = {"Authorization": f"token {GH_TOKEN}"}
    headers_raw = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.raw"}
    base_url    = f"https://api.github.com/repos/{REPO_NAME}/contents"
    date_dot    = board_date.strftime("%Y.%m.%d")

    # ① bcg_data.json
    url  = f"{base_url}/bcg_data.json"
    data, sha = fetch_json(url, headers, headers_raw)
    existing_ids = {item["id"] for item in data}
    added = 0
    for entry in reversed(entries):
        row_id = f"row-{entry['row_id']}"
        if row_id in existing_ids:
            print(f"  중복 스�: {row_id}")
            continue
        data.insert(0, {
            "id":     row_id,
            "date":   date_dot,
            "title":  entry["subject"],
            "detail": entry["summary_html"],
        })
        existing_ids.add(row_id)
        added += 1
    if added == 0:
        print("bcg_data.json: 신규 항목 없음 (모두 중복)")
    else:
        data.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
        r = requests.put(url, headers=headers, json={
            "message": f"BCG update: {date_dot} ({len(entries)}건)",
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode(),
            "sha":     sha,
        })
        print(f"bcg_data.json: {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  오류: {r.text[:200]}")

    # ② index_data.json
    url  = f"{base_url}/index_data.json"
    data, sha = fetch_json(url, headers, headers_raw)
    existing_idx_ids = {item["id"] for item in data}
    idx_added = 0
    for entry in reversed(entries):
        idx_id = f"idx-{entry['row_id']}"
        if idx_id in existing_idx_ids:
            continue
        data.insert(0, {
            "id":       idx_id,
            "category": "BCG Report",
            "date":     date_dot,
            "title":    entry["subject"],
            "detail":   entry["summary_html"],
        })
        existing_idx_ids.add(idx_id)
        idx_added += 1
    if idx_added > 0:
        data.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
        data = data[:300]
        r = requests.put(url, headers=headers, json={
            "message": f"BCG index: {date_dot} ({len(entries)}건)",
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode(),
            "sha":     sha,
        })
        print(f"index_data.json: {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  오류: {r.text[:200]}")


# ── 텔레그램 ───────────────────────────────────────────────────────────────────────────────────────
def send_telegram(text):
    import time
    for attempt in range(3):
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{ARCHIVE_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            print(f"텔레그램: {res.status_code}")
            if res.status_code == 200:
                return
            print(f"  오류: {res.text[:200]}")
        except Exception as e:
            print(f"텔레그램 오류 (시도 {attempt+1}/3): {e}")
        if attempt < 2:
            time.sleep(3)
    print("텔레그램 발송 최종 실패")


# ── 메인 ────────────────────────────────────────────────────────────────────────────────────
def main():
    target_date_str = os.environ.get("TARGET_DATE", "").strip()
    today_kst       = datetime.now(KST)

    if target_date_str:
        try:
            td = datetime.strptime(target_date_str, "%Y-%m-%d")
            target_date = td.replace(tzinfo=KST)
        except ValueError:
            print(f"날짜 형식 오류: {target_date_str}")
            return
        board_date = target_date
        send_tg    = os.environ.get("SEND_TELEGRAM", "false").lower() == "true"
    else:
        target_date = today_kst - timedelta(days=1)
        board_date  = today_kst
        send_tg     = True

    print(f"target_date={target_date.strftime('%Y-%m-%d')}, board_date={board_date.strftime('%Y-%m-%d')}")

    print("Gmail 읽는 중...")
    emails = get_bcg_emails(target_date)
    if not emails:
        print("이메일 없음, 종료")
        return

    entries  = []
    date_str = target_date.strftime("%Y%m%d")
    for i, em in enumerate(emails, 1):
        print(f"\nClaude 요약 중 ({i}/{len(emails)}): {em['subject'][:60]}")
        summary_text = summarize_email(em)
        subject  = " ".join(em["subject"].splitlines()).strip()
        row_id   = f"bcg-{date_str}-{i:03d}"
        summary_html = (
            f'<div style="text-align:left;font-size:15px;color:#1e293b;line-height:1.8;">'
            f'<pre style="white-space:pre-wrap;font-family:inherit;">{summary_text}</pre>'
            f'</div>'
        )
        entries.append({
            "row_id":       row_id,
            "subject":      subject,
            "summary_html": summary_html,
        })

    print("\nGitHub 업데이트 중...")
    update_board(entries, target_date, board_date)

    if send_tg:
        short_date = target_date.strftime("%-m/%-d")
        links = "\n\n".join(
            f"📌 {e['subject']}\n🔗 {ARCHIVE_URL}?open=row-{e['row_id']}"
            for e in entries
        )
        text = (
            f"📊 <b>Ryan's Archive 업데이트 알림</b>\n\n"
            f"✅ 카테고리: 📊 BCG ({short_date})\n\n"
            f"{links}"
        )
        send_telegram(text)

    print("✅ 완료!")


if __name__ == "__main__":
    main()
