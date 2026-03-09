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
    today = datetime.now().strftime("%Y%m%d")
    today_display = datetime.now().strftime("%Y-%m-%d")
    pdf_path = f"NGI_daily_{today}.pdf"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )
        # 隱藏 webdriver 特徵
        browser.add_init_script = None  # placeholder

        page = context.new_page()

        # 登入
        page.goto("https://www.naturalgasintel.com/account/login/")
        page.wait_for_load_state("networkidle")
        # 截圖 debug：看 Playwright 實際看到什麼
        page.screenshot(path="debug_login.png", full_page=True)
        print("頁面標題:", page.title())
        print("頁面 URL:", page.url)
        # 印出頁面 HTML 前 2000 字
        print("頁面 HTML:", page.content()[:2000])
        # 等待輸入欄位出現（最多60秒，應對 JS 動態渲染）
        page.wait_for_selector('input[name="username"]', timeout=60000)
        page.wait_for_timeout(1000)  # 模仿真人停頓
        page.fill('input[name="username"]', NGI_USERNAME)
        page.wait_for_timeout(500)
        page.wait_for_selector('input[name="password"]', timeout=60000)
        page.fill('input[name="password"]', NGI_PASSWORD)
        page.wait_for_timeout(500)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        # 確認登入成功
        print("登入後 URL:", page.url)
        print("登入後標題:", page.title())
        if "login" in page.url or "sign-in" in page.url:
            # 截圖看錯誤訊息
            page.screenshot(path="debug_login.png", full_page=True)
            # 印出表單錯誤
            error_text = page.locator(".errorlist, .alert, .error, [class*='error']").all_text_contents()
            print("登入錯誤訊息:", error_text)
            raise Exception(f"登入失敗，仍在登入頁: {page.url}")

        # 前往 Daily Gas Price Index 頁面
        page.goto("https://www.naturalgasintel.com/news/daily-gas-price-index/")
        page.wait_for_load_state("networkidle")

        # 防呆：從下拉選單的 value 裡取出日期（格式: /protected_documents/dg20260309/）
        latest_date_option = page.locator("select option").first.get_attribute("value")
        print(f"網站最新日期: {latest_date_option}，今天: {today_display}")

        # 從路徑取出日期，例如 dg20260309 -> 2026-03-09
        import re
        date_match = re.search(r'dg(\d{4})(\d{2})(\d{2})', latest_date_option or "")
        if date_match:
            latest_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        else:
            latest_date = latest_date_option  # fallback

        print(f"解析後最新日期: {latest_date}，今天: {today_display}")

        if latest_date != today_display:
            browser.close()
            raise NoIssueToday(
                f"今日（{today_display}）無新聞，"
                f"網站最新為 {latest_date}（可能為美國假日）"
            )

        # 取得 View Issue 的連結網址
        view_issue_href = page.locator("a:has-text('View Issue')").get_attribute("href")
        print(f"View Issue href: {view_issue_href}")

        # 組成完整 PDF 頁面 URL
        if view_issue_href.startswith("http"):
            doc_url = view_issue_href
        else:
            doc_url = f"https://www.naturalgasintel.com{view_issue_href}"
        print(f"前往文件頁面: {doc_url}")

        # 用 requests 帶 cookie 直接下載 PDF（不透過 Playwright）
        cookies = context.cookies()
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"])
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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
  "one_year_strip": 數字,
  "summer_2026": 數字,
  "winter_2026_2027": 數字,
  "national_avg": 數字
}

**第二部分：新聞摘要**
根據 PDF 中的新聞內容，依以下六個主題整理摘要（繁體中文，台灣用語）。
每個主題限 50 字以內，沒提到的主題直接省略。

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

    # 找出 JSON 區塊
    json_match = re.search(r'\{[^{}]+\}', result, re.DOTALL)
    prices = {}
    if json_match:
        try:
            prices = json.loads(json_match.group())
        except:
            pass

    # 摘要是 JSON 之後的部分
    summary_start = result.find("LNG：")
    if summary_start == -1:
        summary_start = result.find("LNG:")
    summary = result[summary_start:].strip() if summary_start != -1 else result

    return prices, summary

# === 產生預覽 HTML，push 到 GitHub Pages ===
def push_preview_to_github(prices, summary):
    today = datetime.now().strftime("%Y年%m月%d日")
    today_key = datetime.now().strftime("%Y%m%d")

    # 組成 LINE 訊息內容（和現在你用的格式一樣）
    line_message = f"""今日天然氣新聞摘要 ({today})

【價格數據】
Henry Hub (現貨)：{prices.get('henry_hub_spot', 'N/A')} USD/MMBtu
Prompt Futures：{prices.get('prompt_futures', 'N/A')} USD/MMBtu
1-Year Strip：{prices.get('one_year_strip', 'N/A')}
Summer 2026：{prices.get('summer_2026', 'N/A')}
Winter 2026/2027：{prices.get('winter_2026_2027', 'N/A')}
National Avg：{prices.get('national_avg', 'N/A')}

【新聞摘要】
{summary}"""

    # 產生預覽 HTML
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
  .btn {{ display: block; width: 100%; padding: 16px; background: #06C755; color: white; border: none; border-radius: 12px; font-size: 17px; font-weight: bold; cursor: pointer; margin-top: 24px; }}
  .btn:hover {{ background: #05a848; }}
  .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
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
        <div class="price-label">1-Year Strip</div>
        <div class="price-value">{prices.get('one_year_strip', 'N/A')}</div>
      </div>
      <div class="price-item">
        <div class="price-label">National Avg</div>
        <div class="price-value">{prices.get('national_avg', 'N/A')}</div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>新聞摘要</h2>
    <div class="summary">{summary}</div>
  </div>

  <button class="btn" id="sendBtn" onclick="sendToLine()">
    📤 發送到 LINE
  </button>
  <div class="status" id="status"></div>
</div>

<script>
const GITHUB_PAT = "{os.environ.get('GITHUB_PAT', '')}";
const GITHUB_REPO = "{os.environ.get('GITHUB_REPO', '')}";
const MESSAGE = {repr(line_message)};

async function sendToLine() {{
  const btn = document.getElementById('sendBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  btn.textContent = '發送中...';

  try {{
    const response = await fetch(
      `https://api.github.com/repos/${{GITHUB_REPO}}/actions/workflows/send_line.yml/dispatches`,
      {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + GITHUB_PAT,
          'Accept': 'application/vnd.github.v3+json'
        }},
        body: JSON.stringify({{
          ref: 'main',
          inputs: {{ message: MESSAGE }}
        }})
      }}
    );

    if (response.status === 204) {{
      btn.textContent = '✅ 已發送！';
      btn.style.background = '#888';
      status.textContent = '訊息已成功發送到 LINE';
    }} else {{
      const err = await response.text();
      throw new Error('HTTP ' + response.status + ': ' + err);
    }}
  }} catch (e) {{
    btn.disabled = false;
    btn.textContent = '📤 發送到 LINE';
    status.textContent = '❌ 發送失敗：' + e.message;
  }}
}}
</script>
</body>
</html>"""

    # 用 GitHub API 把 HTML 推上去
    github_token = os.environ["GITHUB_TOKEN"]
    github_repo = os.environ["GITHUB_REPO"]  # 格式: username/repo-name
    file_path = f"preview/{today_key}.html"

    # 先確認檔案是否已存在（取得 sha）
    check_url = f"https://api.github.com/repos/{github_repo}/contents/{file_path}"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    check_res = requests.get(check_url, headers=headers)
    sha = check_res.json().get("sha") if check_res.status_code == 200 else None

    # 上傳檔案
    payload = {
        "message": f"Add preview for {today_key}",
        "content": base64.b64encode(html_content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(check_url, headers=headers, json=payload)
    if res.status_code in [200, 201]:
        # 取得 GitHub Pages URL
        repo_name = github_repo.split("/")[1]
        username = github_repo.split("/")[0]
        preview_url = f"https://{username}.github.io/{repo_name}/preview/{today_key}.html"
        print(f"✅ 預覽頁面已上傳: {preview_url}")
        return preview_url
    else:
        raise Exception(f"GitHub 上傳失敗: {res.text}")

# === 私訊你預覽連結 ===
def send_preview_link(preview_url, prices):
    today = datetime.now().strftime("%Y年%m月%d日")
    message = f"""📊 {today} 天然氣摘要已準備好

Henry Hub: {prices.get('henry_hub_spot', 'N/A')} USD/MMBtu
Prompt Futures: {prices.get('prompt_futures', 'N/A')}

請點擊預覽並確認後發送：
{preview_url}"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    # 私訊給你（需要你的 LINE User ID）
    payload = {
        "to": os.environ["LINE_USER_ID"],
        "messages": [{"type": "text", "text": message}]
    }
    r = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload)
    print("✅ 預覽連結已傳送:", r.status_code)

# === 主程式 ===
if __name__ == "__main__":
    try:
        # 1. 下載 PDF（若今天無新聞會拋出 NoIssueToday）
        pdf_path = download_pdf()

        # 2. Claude 讀 PDF
        result = process_pdf(pdf_path)

        # 3. 解析結果
        prices, summary = parse_result(result)
        print("價格:", prices)
        print("摘要:", summary[:100], "...")

        # 4. 推上 GitHub Pages
        preview_url = push_preview_to_github(prices, summary)

        # 5. LINE 私訊你預覽連結
        send_preview_link(preview_url, prices)

        print("🎉 全部完成！")

    except NoIssueToday as e:
        # 假日或無新聞：靜默通知，不算錯誤
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
        # 真正的錯誤：通知你並拋出讓 GitHub Actions 標記失敗
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
