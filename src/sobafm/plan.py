"""What a program plays, and its mapping to Lyria RealTime prompts and configuration."""

from typing import Annotated

from google.genai import types
from pydantic import BaseModel, BeforeValidator, Field

# Lyria RealTime's defaults; SobaFM fixes them rather than letting a model choose them.
GUIDANCE = 4.0
TEMPERATURE = 1.1
TOP_K = 40

MAX_PROMPT_LENGTH = 120
MAX_PROMPTS = 4
ADDED_WEIGHT = 0.5  # a request added to the current music, so it nudges rather than replaces
MAX_TITLE_LENGTH = 60


def _known_scale(value: object) -> object:
    """Accept only Lyria's scales, and read SCALE_UNSPECIFIED as no scale.

    The SDK's enum turns any string into a member with only a warning, so names are checked here.
    """
    if isinstance(value, str):  # the enum's members are strings too
        if value.upper() == types.Scale.SCALE_UNSPECIFIED.value:
            return None
        if value.upper() not in types.Scale.__members__:
            raise ValueError(f"unknown scale {value!r}")
    return value


def _one_line(value: object) -> object:
    """Collapse whitespace, so text reads on one line and blank text is empty."""
    return " ".join(value.split()) if isinstance(value, str) else value


class Prompt(BaseModel):
    text: Annotated[str, BeforeValidator(_one_line)] = Field(
        min_length=1, max_length=MAX_PROMPT_LENGTH
    )
    weight: float = Field(default=1.0, ge=0.1, le=1.0)


class MusicPlan(BaseModel):
    """A program's musical direction (ADR-0002); unset values are left to Lyria."""

    title: Annotated[str, BeforeValidator(_one_line)] = Field(
        min_length=1, max_length=MAX_TITLE_LENGTH
    )
    prompts: list[Prompt] = Field(min_length=1, max_length=MAX_PROMPTS)
    bpm: int | None = Field(default=None, ge=60, le=200)
    scale: Annotated[types.Scale | None, BeforeValidator(_known_scale)] = None
    density: float | None = Field(default=None, ge=0.0, le=1.0)
    brightness: float | None = Field(default=None, ge=0.0, le=1.0)
    mute_drums: bool = False
    vocalization: bool = False

    @classmethod
    def from_request(cls, request: str) -> MusicPlan:
        """A plan that plays the request text itself as the only prompt; it must not be blank."""
        text = " ".join(request.split())[:MAX_PROMPT_LENGTH]
        return cls(title=text[:MAX_TITLE_LENGTH], prompts=[Prompt(text=text)])

    def with_request(self, request: str) -> MusicPlan:
        """This plan with the request text added as a lighter prompt, keeping every setting.

        A full plan drops its lowest-weight prompt, the latest of equals, to make room.
        """
        added = Prompt(text=" ".join(request.split())[:MAX_PROMPT_LENGTH], weight=ADDED_WEIGHT)
        kept = list(self.prompts)
        if len(kept) >= MAX_PROMPTS:
            last_lowest = min(range(len(kept) - 1, -1, -1), key=lambda index: kept[index].weight)
            del kept[last_lowest]
        return self.model_copy(update={"prompts": [*kept, added]})

    def weighted_prompts(self) -> list[types.WeightedPrompt]:
        return [types.WeightedPrompt(text=p.text, weight=p.weight) for p in self.prompts]

    def to_config(self) -> types.LiveMusicGenerationConfig:
        """The generation config, with every field SobaFM controls set.

        Lyria resets any field a config omits, so only the plan's unset optional values are
        left to Lyria's defaults.
        """
        return types.LiveMusicGenerationConfig(
            guidance=GUIDANCE,
            temperature=TEMPERATURE,
            top_k=TOP_K,
            bpm=self.bpm,
            scale=self.scale,
            density=self.density,
            brightness=self.brightness,
            mute_bass=False,
            mute_drums=self.mute_drums,
            only_bass_and_drums=False,
            music_generation_mode=(
                types.MusicGenerationMode.VOCALIZATION
                if self.vocalization
                else types.MusicGenerationMode.QUALITY
            ),
        )
