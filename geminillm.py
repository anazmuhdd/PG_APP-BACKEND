from langchain_core.language_models import LLM
from typing import Optional
from langchain_core.outputs import Generation
import google.generativeai as genai
from pydantic import PrivateAttr

class GeminiLLM(LLM):
    model_name: str = "gemini-1.5-flash"
    temperature: float = 0.7
    _model: Optional[genai.GenerativeModel] = PrivateAttr()

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model_name=self.model_name)

    def _call(self, prompt: str, stop: Optional[list] = None) -> str:
        response = self._model.generate_content(prompt)
        return response.text

    @property
    def _llm_type(self) -> str:
        return "gemini-custom"
