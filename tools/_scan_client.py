#!/usr/bin/env python3
from pathlib import Path

t = Path(r"C:\projects\pokereye\poker-eye-v2\logs\_eye_panel_index.js").read_text(
    encoding="utf-8", errors="replace"
)

i = t.find('T7="fe5cbfbbb49302ee9aaecc99945da20f"')
print("T7 around\n", t[i - 200 : i + 400])

i = t.find("path:\"/api/authenticate\"")
print("\nauthenticate around\n", t[max(0, i - 300) : i + 400])

# request implementation nearby "this.request({path:"
i = t.find("class ") 
# find baseUrl
for s in ["baseUrl", "baseURL", "Authorization", "x-api", "X-Auth", "Bearer"]:
    print(s, t.lower().count(s.lower()) if False else t.count(s))

i = t.find('baseUrl')
print("\nbaseUrl", i, t[i:i+200] if i>=0 else None)
i = t.find("baseURL")
print("baseURL", i, t[i:i+200] if i>=0 else None)

# cookie names
i = t.find("/api/isLoggedIn")
print("\nisLoggedIn impl\n", t[i-200:i+250])
