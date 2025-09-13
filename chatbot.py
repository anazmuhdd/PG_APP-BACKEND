from openai import OpenAI

# ✅ Initialize NVIDIA Qwen client
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key="YOUR_NVIDIA_API_KEY"  # replace with your key
)

print("🤖 Qwen Chatbot (type 'exit' to quit)\n")

chat_history = []

while True:
    user_input = input("You: ")

    if user_input.lower() in ["exit", "quit"]:
        print("👋 Goodbye!")
        break

    # Append user input to history
    chat_history.append({"role": "user", "content": user_input})

    # Call NVIDIA Qwen
    completion = client.chat.completions.create(
        model="qwen/qwen2.5-coder-32b-instruct",  # or another Qwen model
        messages=chat_history,
        temperature=0.7,
        top_p=0.9,
        max_tokens=512,
        stream=False
    )

    reply = completion.choices[0].message.content
    print(f"Qwen: {reply}\n")

    # Save reply in history for context
    chat_history.append({"role": "assistant", "content": reply})
