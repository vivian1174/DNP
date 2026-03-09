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
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        # 登入
        page.goto("https://www.natgasintel.com/login/")
        page.wait_for_load_state("networkidle")
        # 等待輸入欄位出現（最多60秒，應對 JS 動態渲染）
        page.wait_for_selector('input[name="username"]', timeout=60000)
        page.fill('input[name="username"]', NGI_USERNAME)
        page.wait_for_selector('input[name="password"]', timeout=60000)
        page.fill('input[name="password"]', NGI_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # 前往 Daily Gas Price Index 頁面
        page.goto("https://www.natgasintel.com/news/daily-gas-price-index/")
        page.wait_for_load_state("networkidle")

        # 防呆：檢查下拉選單的最新日期是否是今天
        latest_date_option = page.locator("select option").first.get_attribute("value")
        print(f"網站最新日期: {latest_date_option}，今天: {today_display}")

        if latest_date_option != today_display:
            browser.close()
            raise NoIssueToday(
                f"今日（{today_display}）無新聞，"
                f"網站最新為 {latest_date_option}（可能為美國假日）"
            )

        # 點擊 "View Issue" 按鈕，等待 PDF 出現
        with page.expect_popup() as popup_info:
            page.click("button:has-text('View Issue')")
        pdf_page = popup_info.value
        pdf_page.wait_for_load_state("networkidle")

        # 取得 PDF URL 並下載（帶登入 cookie）
        pdf_url = pdf_page.url
        print(f"PDF URL: {pdf_url}")

        cookies = context.cookies()
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"])
        pdf_response = session.get(pdf_url)

        with open(pdf_path, "wb") as f:
            f.write(pdf_response.content)

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
const LINE_TOKEN = "{os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')}";
const MESSAGE = {repr(line_message)};

async function sendToLine() {{
  const btn = document.getElementById('sendBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  btn.textContent = '發送中...';

  try {{
    const response = await fetch('https://api.line.me/v2/bot/message/broadcast', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + LINE_TOKEN
      }},
      body: JSON.stringify({{
        messages: [{{ type: 'text', text: MESSAGE }}]
      }})
    }});

    if (response.ok) {{
      btn.textContent = '✅ 已發送！';
      btn.style.background = '#888';
      status.textContent = '訊息已成功發送到 LINE';
    }} else {{
      throw new Error('HTTP ' + response.status);
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
