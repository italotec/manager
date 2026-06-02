import requests

PHONE_NUMBER_ID = "1031324240075282"
ACCESS_TOKEN = "EAAUvxqVuih8BRVipubz41WKkJBNrBKhYCMCvLoITSTChMDZCG79qrmzZA15uHo1ZBAVHomIWwRTz66Rme6KdAOdGqR42HejRV8WdjmMRT1HJiAURYnioCWpv7CBoVYfaugGJUZBUAxHsNmPD92uc9P6W8rY5lKxyh8XFy2uzCsRer4jiTDMQqsZBmwGilbXeXGgZDZD"
API_VERSION = "v23.0"

url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}?fields=whatsapp_business_manager_messaging_limit"
headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

r = requests.get(url, headers=headers, timeout=30)
print(f"Status: {r.status_code}")
print(r.json())
