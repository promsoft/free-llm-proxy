from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rank: int
    id: str
    name: str | None = None
    score: float | None = None
    context_length: int | None = Field(default=None, alias="contextLength")
    max_completion_tokens: int | None = Field(default=None, alias="maxCompletionTokens")

    supports_tools: bool = Field(default=False, alias="supportsTools")
    supports_tool_choice: bool = Field(default=False, alias="supportsToolChoice")
    supports_structured_outputs: bool = Field(default=False, alias="supportsStructuredOutputs")
    supports_response_format: bool = Field(default=False, alias="supportsResponseFormat")
    supports_reasoning: bool = Field(default=False, alias="supportsReasoning")
    supports_include_reasoning: bool = Field(default=False, alias="supportsIncludeReasoning")
    supports_seed: bool = Field(default=False, alias="supportsSeed")
    supports_stop: bool = Field(default=False, alias="supportsStop")

    latency_ms: int | None = Field(default=None, alias="latencyMs")
    health_status: str | None = Field(default=None, alias="healthStatus")
    reason: str | None = None


class TopModelsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    count: int | None = None
    models: list[Model]
