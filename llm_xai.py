import llm
from llm.default_plugins.openai_models import Chat, Completion
from llm.utils import remove_dict_none_values
from pathlib import Path
import json
import time
import httpx
from enum import Enum
from typing import Optional
from pydantic import Field

class ReasoningEffortEnum(str, Enum):
    low = "low"
    high = "high"

def get_xAI_models():
    return fetch_cached_json(
        url="https://api.x.ai/v1/models",
        path=llm.user_dir() / "xAI_models.json",
        cache_timeout=3600,
    )["data"]

class XAIChat(Chat):
    needs_key = "xai"
    key_env_var = "XAI_KEY"
    
    class Options(Chat.Options):
        reasoning_effort: Optional[ReasoningEffortEnum] = Field(
            description="Controls how much time the model spends thinking. Use 'low' for quick responses or 'high' for complex problems.",
            default=None,
        )
    
    def __str__(self):
        return "xAI: {}".format(self.model_id)
    
    def build_kwargs(self, prompt, stream):
        # Call parent's build_kwargs but without adding stream_options
        kwargs = super().build_kwargs(prompt, False)  # Pass False to prevent adding stream_options
        # Keep the stream parameter intact for actual streaming
        return kwargs
        
    def execute(self, prompt, stream, response, conversation=None, key=None):
        # Check if reasoning_effort is in the options
        has_reasoning = any(isinstance(opt[0], str) and opt[0] == 'reasoning_effort' for opt in prompt.options)
        
        # If reasoning is requested and streaming was asked for, we need to use non-streaming
        if has_reasoning and stream:
            stream = False  # Force non-streaming mode
            
        if prompt.system and not self.allows_system_prompt:
            raise NotImplementedError("Model does not support system prompts")
            
        messages = self.build_messages(prompt, conversation)
        kwargs = self.build_kwargs(prompt, stream)
        client = self.get_client(key)
        
        # Make the API call (non-streaming in both cases when reasoning is requested)
        if not stream:
            completion = client.chat.completions.create(
                model=self.model_name or self.model_id,
                messages=messages,
                stream=False,
                **kwargs,
            )
            
            # Store response and usage info
            response.response_json = completion.model_dump()
            if completion.usage:
                self.set_usage(response, completion.usage.model_dump())
            
            # Check if reasoning_content exists
            has_reasoning_content = (hasattr(completion.choices[0].message, 'reasoning_content') 
                                    and completion.choices[0].message.reasoning_content)
            
            # If there's reasoning content, yield it first with a clear label
            if has_reasoning and has_reasoning_content:
                reasoning = completion.choices[0].message.reasoning_content
                # Show reasoning first with label, then show the normal content
                yield "Reasoning Content:\n" + reasoning + "\n\nFinal Response:\n" + completion.choices[0].message.content
            else:
                # If no reasoning, just yield the normal content
                yield completion.choices[0].message.content
                
            return
        
        # Standard streaming behavior (no reasoning)
        completion = client.chat.completions.create(
            model=self.model_name or self.model_id,
            messages=messages,
            stream=True,
            **kwargs,
        )
        
        # Process streaming chunks
        all_content = []
        for chunk in completion:
            if chunk.usage:
                self.set_usage(response, chunk.usage.model_dump())
                
            try:
                content = chunk.choices[0].delta.content
                if content is not None:
                    all_content.append(content)
                    yield content
            except (IndexError, AttributeError):
                pass
        
        # Store content in response
        response.response_json = {"content": "".join(all_content)}

class XAICompletion(Completion):
    needs_key = "xai"
    key_env_var = "XAI_KEY"
    
    class Options(Completion.Options):
        reasoning_effort: Optional[ReasoningEffortEnum] = Field(
            description="Controls how much time the model spends thinking. Use 'low' for quick responses or 'high' for complex problems.",
            default=None,
        )
    
    def __str__(self):
        return "xAI: {}".format(self.model_id)
    
    def build_kwargs(self, prompt, stream):
        # Call parent's build_kwargs but without adding stream_options
        kwargs = super().build_kwargs(prompt, False)  # Pass False to prevent adding stream_options
        # Keep the stream parameter intact for actual streaming
        return kwargs
        
    def execute(self, prompt, stream, response, conversation=None, key=None):
        # Check if reasoning_effort is in the options
        has_reasoning = any(isinstance(opt[0], str) and opt[0] == 'reasoning_effort' for opt in prompt.options)
        
        # If reasoning is requested and streaming was asked for, we need to use non-streaming
        if has_reasoning and stream:
            stream = False  # Force non-streaming mode
        
        if prompt.system:
            raise NotImplementedError("System prompts are not supported for OpenAI completion models")
            
        messages = []
        if conversation is not None:
            for prev_response in conversation.responses:
                messages.append(prev_response.prompt.prompt)
                messages.append(prev_response.text())
        messages.append(prompt.prompt)
        
        kwargs = self.build_kwargs(prompt, stream)
        client = self.get_client(key)
        
        if not stream:
            completion = client.completions.create(
                model=self.model_name or self.model_id,
                prompt="\n".join(messages),
                stream=False,
                **kwargs,
            )
            
            # Store response
            response.response_json = completion.model_dump()
            if completion.usage:
                self.set_usage(response, completion.usage.model_dump())
            
            # Check if there's reasoning content
            has_reasoning_content = False
            reasoning_content = None
            
            if has_reasoning and response.response_json and 'choices' in response.response_json:
                for choice in response.response_json['choices']:
                    if 'message' in choice and 'reasoning_content' in choice['message']:
                        has_reasoning_content = True
                        reasoning_content = choice['message']['reasoning_content']
                        break
            
            # If we have reasoning content, only show that
            if has_reasoning and has_reasoning_content and reasoning_content:
                yield "Reasoning Content:\n" + reasoning_content + "\n\nFinal Response:\n" + completion.choices[0].text
            else:
                # Otherwise show the normal content
                yield completion.choices[0].text
            
            return
        
        # Standard streaming behavior for non-reasoning requests
        completion = client.completions.create(
            model=self.model_name or self.model_id,
            prompt="\n".join(messages),
            stream=True,
            **kwargs,
        )
        
        chunks = []
        for chunk in completion:
            chunks.append(chunk)
            try:
                content = chunk.choices[0].text
            except IndexError:
                content = None
            if content is not None:
                yield content
                
        # Store response data
        response.response_json = {"text": "".join([c.choices[0].text for c in chunks if hasattr(c, 'choices') and c.choices])}

@llm.hookimpl
def register_models(register):
    # Only do this if the xAI key is set
    key = llm.get_key("", "xai", "LLM_XAI_KEY")
    if not key:
        return

    models = get_xAI_models()
    for model_definition in models:
        chat_model = XAIChat(
            model_id="xAI/{}".format(model_definition["id"]),
            model_name=model_definition["id"],
            api_base="https://api.x.ai/v1/",
            headers={"HTTP-Referer": "https://llm.datasette.io/", "X-Title": "LLM"},
        )
        register(chat_model)

    for model_definition in models:
        completion_model = XAICompletion(
            model_id="xAIcompletion/{}".format(model_definition["id"]),
            model_name=model_definition["id"],
            api_base="https://api.x.ai/v1/",
            headers={"HTTP-Referer": "https://llm.datasette.io/", "X-Title": "LLM"},
        )
        register(completion_model)

class DownloadError(Exception):
    pass

def fetch_cached_json(url, path, cache_timeout):
    path = Path(path)
    # Create directories if not exist
    path.parent.mkdir(parents=True, exist_ok=True)

    # Get the API key
    key = llm.get_key("", "xai", "LLM_XAI_KEY")

    if path.is_file():
        # Get the file's modification time
        mod_time = path.stat().st_mtime
        # Check if it's more than the cache_timeout old
        if time.time() - mod_time < cache_timeout:
            # If not, load the file
            with open(path, "r") as file:
                return json.load(file)

    # Try to download the data
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        response = httpx.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()  # This will raise an HTTPError if the request fails

        # If successful, write to the file
        with open(path, "w") as file:
            json.dump(response.json(), file)
        return response.json()

    except httpx.HTTPError:
        # If there's an existing file, load it
        if path.is_file():
            with open(path, "r") as file:
                return json.load(file)
        else:
            # If not, raise an error
            raise DownloadError(
                f"Failed to download data and no cache is available at {path}"
            )
