from openai import OpenAI
import dotenv
import os
dotenv.load_dotenv()
NVIDIA_API_KEY = dotenv.get_key(os.path.join(os.path.dirname(__file__), ".env"), "nvidia_api_key")
client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = NVIDIA_API_KEY
)

completion = client.chat.completions.create(
  model="qwen/qwen3-235b-a22b",
  messages=[{"role":"user","content":"Calculate 5+10"}],
  temperature=0.2,
  top_p=0.7,
  max_tokens=8192,
  extra_body={"chat_template_kwargs": {"thinking":True}},
  stream=False
)

reasoning = getattr(completion.choices[0].message, "reasoning_content", None)
if reasoning:
  print(reasoning)
print("repy:",completion.choices[0].message.content)

