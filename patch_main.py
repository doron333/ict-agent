"""Apply coordination patch to main.py"""
import re

with open('/root/repo/main.py', 'r') as f:
    content = f.read()

# Add import after existing imports
import_section = "import requests\n"
new_imports = "import requests\nimport json as _json\nfrom redis_coordinator import aggregator\n"
content = content.replace(import_section, new_imports, 1)

# Find and replace send_telegram function
old_send_telegram = '''def send_telegram(text):
    if not BOT or not CHAT:
        print("[DRY-RUN telegram]\\n" + text + "\\n")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15).raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}")'''

new_send_telegram = '''def send_telegram(text):
    """
    Modified to support multi-agent coordination via Redis.
    Non-lead agents queue signals; only lead agent sends.
    """
    if aggregator.r:
        # Extract signal data from formatted text (simplified)
        import sys
        if "--backtest" not in sys.argv and not aggregator.is_lead():
            # Non-lead agents store their signals
            signal_data = {"_agent_id": aggregator.agent_id, "_raw_text": text}
            try:
                aggregator.queue_signal(_json.dumps(signal_data))
            except:
                pass
            return
        
        # Lead agent aggregates every minute
        if "--backtest" not in sys.argv and aggregator.is_lead():
            batch_msg = aggregator.collect_batch()
            if batch_msg:
                text = batch_msg  # Use aggregated message
    
    # Send to Telegram
    if not BOT or not CHAT:
        print("[DRY-RUN telegram]\\n" + text + "\\n")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15).raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}")'''

content = content.replace(old_send_telegram, new_send_telegram)

with open('/root/repo/main.py', 'w') as f:
    f.write(content)

print("✓ Patched main.py with Redis coordination")
