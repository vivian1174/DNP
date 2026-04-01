import anthropic
import requests
import os
import base64
from datetime import datetime
from playwright.sync_api import sync_playwright

# === 設定（從環境變數讀取，不要硬寫在程式裡）===
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
NGI_USERNAME = os.environ["NGI_USERNAME"]
NGI_PASSWORD = os.environ["NGI_PASSWORD"]

class NoIssueToday(Exception):
    """當天沒有新聞發布時拋出此例外（美國假日等）"""
    pass


# === 登入並下載 PDF ===
def download_pdf():
    from zoneinfo import ZoneInfo
    tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
    today = tw_now.strftime("%Y%m%d")
    today_display = tw_now.strftime("%Y-%m-%d")
    pdf_path = f"NGI_daily_{today}.pdf"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--start-maximized",
            ]
        )
        context = browser.new_context(
            ignore_https_errors=True,
            # ✅ 修改1：升級為更新的 User-Agent（Chrome 124）
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            # ✅ 修改2：Viewport 擴大為 1920x1080
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )

        page = context.new_page()

        # ✅ 修改3：加入 webdriver 偽裝腳本，移除自動化特徵
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # ✅ 修改4：wait_until 改為 domcontentloaded，避免被廣告腳本卡住
        page.goto("https://www.naturalgasintel.com/account/login/", wait_until="domcontentloaded")
        page.screenshot(path="debug_login.png", full_page=True)
        print("頁面標題:", page.title())
        print("頁面 URL:", page.url)
        print("頁面 HTML:", page.content()[:2000])

        page.wait_for_selector('input[name="username"]', timeout=60000)
        page.wait_for_timeout(1000)
        page.fill('input[name="username"]', NGI_USERNAME)
        page.wait_for_timeout(500)
        page.wait_for_selector('input[name="password"]', timeout=60000)
        page.fill('input[name="password"]', NGI_PASSWORD)
        page.wait_for_timeout(500)
        page.click('button[type="submit"]')

        # ✅ 修改5：登入後等待改為 domcontentloaded，並增加等待時間至 3 秒
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        print("登入後 URL:", page.url)
        print("登入後標題:", page.title())
        if "login" in page.url or "sign-in" in page.url:
            page.screenshot(path="debug_login.png", full_page=True)
            error_text = page.locator(".errorlist, .alert, .error, [class*='error']").all_text_contents()
            print("登入錯誤訊息:", error_text)
            raise Exception(f"登入失敗，仍在登入頁: {page.url}")

        # ✅ 修改6：前往目標頁面也改為 domcontentloaded
        page.goto("https://www.naturalgasintel.com/news/daily-gas-price-index/", wait_until="domcontentloaded")

        latest_date_option = page.locator("select option").first.get_attribute("value")
        print(f"網站最新日期: {latest_date_option}，今天: {today_display}")

        import re
        date_match = re.search(r'dg(\d{4})(\d{2})(\d{2})', latest_date_option or "")
        if date_match:
            latest_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        else:
            latest_date = latest_date_option

        print(f"解析後最新日期: {latest_date}，今天: {today_display}")

        if latest_date != today_display:
            browser.close()
            raise NoIssueToday(
                f"今日（{today_display}）無新聞，"
                f"網站最新為 {latest_date}（可能為美國假日）"
            )

        view_issue_href = page.locator("a:has-text('View Issue')").get_attribute("href")
        print(f"View Issue href: {view_issue_href}")

        if view_issue_href.startswith("http"):
            doc_url = view_issue_href
        else:
            doc_url = f"https://www.naturalgasintel.com{view_issue_href}"
        print(f"前往文件頁面: {doc_url}")

        cookies = context.cookies()
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"])
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.naturalgasintel.com/news/daily-gas-price-index/",
        })
        pdf_response = session.get(doc_url, allow_redirects=True)
        print(f"下載狀態: {pdf_response.status_code}, Content-Type: {pdf_response.headers.get('Content-Type')}, 大小: {len(pdf_response.content)} bytes")

        with open(pdf_path, "wb") as f:
            f.write(pdf_response.content)
        print(f"PDF 儲存完成: {pdf_path}")

        browser.close()

    print(f"✅ PDF 下載完成: {pdf_path}")
    return pdf_path


# === 用 Claude 直接讀 PDF，擷取價格 + 產生摘要 ===
def process_pdf(pdf_path):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    with open(pdf_path, "rb") as f:
        pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

    prompt = """請從這份 NGI Daily Gas Price Index PDF 中完成以下兩件事：

**第一部分：擷取價格數據（JSON格式）**
請找出最新一天的以下價格，以 JSON 格式輸出：
{
  "date": "YYYY-MM-DD",
  "henry_hub_spot": 數字,
  "prompt_futures": 數字,
  "columbia_gulf_mainline": 數字,
  "texas_gas_zone_1": 數字
}

**第二部分：新聞摘要**
Here is today's full natural gas news article. Please follow the steps below to create the summary:
1.First, summarize strictly based on the news content in English. Do not add external information.
2.Organize the summary under the following six themes: LNG, Price, Production/Storage, Policy, Weather, Non-weather demand.
 Focus on near-term and short-term information. If unavailable, then include long-term information.
 If the news does not mention a certain theme, simply omit it. Don't be too short and avoid overly general statements.
3.Keep each summary point within 50 words (English).
4.After completing the English summary, translate it fully into Traditional Chinese (Taiwan usage), keeping it professional, concise, and fluent. Ensure that all six themes are translated completely into Chinese if they appear. Do not omit any theme.
5.Ensure consistency in describing market directions (price up/down) without contradictions.
6.Only output the final full summary in Traditional Chinese (Taiwan usage). Do not output the English version.
7.以下是專有名詞的翻譯對照表，請在翻譯時使用這些對照表中的中文詞彙：荷姆茲海峽	Strait of Hormuz;入料氣	Feed Gas;接收站	LNG Terminal;儲槽	Storage Tank;

格式：
LNG：摘要內容
價格：摘要內容
產量/儲量：摘要內容
政策：摘要內容
天氣：摘要內容
非天氣因素需求：摘要內容

請先輸出 JSON，然後空一行，再輸出新聞摘要。"""

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_data
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    )

    result = response.content[0].text
    print("✅ Claude 處理完成")
    return result


# === 解析 Claude 回應，分離 JSON 和摘要 ===
def parse_result(result):
    import json
    import re

    json_match = re.search(r'\{[^{}]+\}', result, re.DOTALL)
    prices = {}
    if json_match:
        try:
            prices = json.loads(json_match.group())
        except:
            pass

    summary_start = result.find("LNG：")
    if summary_start == -1:
        summary_start = result.find("LNG:")
    summary = result[summary_start:].strip() if summary_start != -1 else result

    return prices, summary


# === 產生預覽 HTML，push 到 GitHub Pages ===
def push_preview_to_github(prices, summary):
    from zoneinfo import ZoneInfo
    tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
    today = tw_now.strftime("%Y年%m月%d日")
    today_key = tw_now.strftime("%Y%m%d")

    line_message = f"""今日天然氣新聞摘要 ({today})

【價格數據】
Henry Hub (現貨)：{prices.get('henry_hub_spot', 'N/A')} USD/MMBtu
Prompt Futures：{prices.get('prompt_futures', 'N/A')} USD/MMBtu
Columbia Gulf Mainline：{prices.get('columbia_gulf_mainline', 'N/A')}
Texas Gas Zone 1：{prices.get('texas_gas_zone_1', 'N/A')}

【新聞摘要】
{summary}"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>天然氣新聞摘要 {today}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 680px; margin: 40px auto; padding: 20px; background: #f5f5f5; }}
  .card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
  h1 {{ font-size: 20px; color: #333; margin-bottom: 4px; }}
  .date {{ color: #888; font-size: 14px; margin-bottom: 24px; }}
  .section {{ margin-bottom: 20px; }}
  .section h2 {{ font-size: 14px; color: #666; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }}
  .price-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .price-item {{ background: #f8f9fa; border-radius: 8px; padding: 10px 14px; }}
  .price-label {{ font-size: 12px; color: #888; }}
  .price-value {{ font-size: 18px; font-weight: bold; color: #1a1a1a; }}
  .summary {{ white-space: pre-wrap; line-height: 1.8; color: #333; font-size: 15px; }}
  .status {{ text-align: center; margin-top: 12px; font-size: 14px; color: #666; }}
</style>
</head>
<body>
<div class="card">
  <h1>📊 今日天然氣新聞摘要</h1>
  <div class="date">{today}</div>
  <div class="section">
    <h2>價格數據</h2>
    <div class="price-grid">
      <div class="price-item">
        <div class="price-label">Henry Hub (現貨)</div>
        <div class="price-value">{prices.get('henry_hub_spot', 'N/A')}</div>
      </div>
      <div class="price-item">
        <div class="price-label">Prompt Futures</div>
        <div class="price-value">{prices.get('prompt_futures', 'N/A')}</div>
      </div>
      <div class="price-item">
        <div class="price-label">Columbia Gulf Mainline</div>
        <div class="price-value">{prices.get('columbia_gulf_mainline', 'N/A')}</div>
      </div>
      <div class="price-item">
        <div class="price-label">Texas Gas Zone 1</div>
        <div class="price-value">{prices.get('texas_gas_zone_1', 'N/A')}</div>
      </div>
    </div>
  </div>
  <div class="section">
    <h2>新聞摘要</h2>
    <div class="summary">{summary}</div>
  </div>
  <div class="status">✅ 已自動發送到 LINE</div>
</div>
</body>
</html>"""

    github_token = os.environ["GITHUB_TOKEN"]
    github_repo = os.environ["GITHUB_REPO"]
    file_path = f"preview/{today_key}.html"

    check_url = f"https://api.github.com/repos/{github_repo}/contents/{file_path}"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    check_res = requests.get(check_url, headers=headers)
    sha = check_res.json().get("sha") if check_res.status_code == 200 else None

    payload = {
        "message": f"Add preview for {today_key}",
        "content": base64.b64encode(html_content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(check_url, headers=headers, json=payload)
    if res.status_code in [200, 201]:
        repo_name = github_repo.split("/")[1]
        username = github_repo.split("/")[0]
        preview_url = f"https://{username}.github.io/{repo_name}/preview/{today_key}.html"
        print(f"✅ 預覽頁面已上傳: {preview_url}")
        return preview_url, line_message
    else:
        raise Exception(f"GitHub 上傳失敗: {res.text}")


# === 主程式 ===
if __name__ == "__main__":
    try:
        pdf_path = download_pdf()
        result = process_pdf(pdf_path)
        prices, summary = parse_result(result)
        print("價格:", prices)
        print("摘要:", summary[:100], "...")

        preview_url, line_message = push_preview_to_github(prices, summary)

        headers_line = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
        r = requests.post("https://api.line.me/v2/bot/message/broadcast", headers=headers_line, json={
            "messages": [{"type": "text", "text": line_message}]
        })
        print("✅ LINE broadcast 狀態:", r.status_code)

        requests.post("https://api.line.me/v2/bot/message/push", headers=headers_line, json={
            "to": os.environ["LINE_USER_ID"],
            "messages": [{"type": "text", "text": f"✅ 今日摘要已自動發送\n預覽：{preview_url}"}]
        })

        print("🎉 全部完成！")

    except NoIssueToday as e:
        print(f"ℹ️ {e}")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json={
                "to": os.environ["LINE_USER_ID"],
                "messages": [{"type": "text", "text": f"📭 {str(e)}\n今日不發送摘要。"}]
            }
        )

    except Exception as e:
        print(f"❌ 發生錯誤: {e}")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json={
                "to": os.environ["LINE_USER_ID"],
                "messages": [{"type": "text", "text": f"❌ 今日天然氣摘要執行失敗\n錯誤：{str(e)}"}]
            }
        )
        raise e
