# test_proxy.py
from openai import OpenAI

print("Testing connection to ProxyGateLLM on http://localhost:3333/v1 ...")
try:
    client = OpenAI(
        base_url="http://localhost:3333/v1",
        api_key="not-needed"
    )
    response = client.chat.completions.create(
        model="auto",
        messages=[{"role": "user", "content": "Say 'Proxy is working!' in one word."}],
        temperature=0.1
    )
    print("\n✅ SUCCESS! Proxy responded with:")
    print(response.choices[0].message.content)
except Exception as e:
    print("\n❌ FAILED! Proxy could not be reached or rejected the request:")
    print(e)