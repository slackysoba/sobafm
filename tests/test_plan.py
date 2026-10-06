import pytest
from google.genai import types
from pydantic import ValidationError

from sobafm.plan import MusicPlan, Prompt


def test_from_request_uses_the_text_as_the_only_prompt() -> None:
    plan = MusicPlan.from_request("  rainy   lo-fi\nwith soft piano ")

    assert plan.title == "rainy lo-fi with soft piano"
    assert plan.weighted_prompts() == [
        types.WeightedPrompt(text="rainy lo-fi with soft piano", weight=1.0)
    ]


def test_from_request_truncates_long_requests() -> None:
    plan = MusicPlan.from_request("x" * 500)

    assert len(plan.prompts[0].text) == 120
    assert len(plan.title) == 60


def test_with_request_adds_a_lighter_prompt_and_keeps_the_settings() -> None:
    prompts = [Prompt(text="rainy lo-fi")]
    current = MusicPlan(title="Rainy", prompts=prompts, bpm=80, mute_drums=True)

    plan = current.with_request("  darker\n")

    assert [(p.text, p.weight) for p in plan.prompts] == [("rainy lo-fi", 1.0), ("darker", 0.5)]
    assert (plan.title, plan.bpm, plan.mute_drums) == ("Rainy", 80, True)


def test_with_request_on_a_full_plan_drops_the_last_lowest_weight_prompt() -> None:
    weights = [("a", 1.0), ("b", 0.4), ("c", 0.4), ("d", 1.0)]
    current = MusicPlan(title="Busy", prompts=[Prompt(text=t, weight=w) for t, w in weights])

    plan = current.with_request("e")

    assert [p.text for p in plan.prompts] == ["a", "b", "d", "e"]


def test_config_is_complete_with_fixed_sampling_values() -> None:
    config = MusicPlan.from_request("ambient").to_config()

    assert (config.guidance, config.temperature, config.top_k) == (4.0, 1.1, 40)
    assert config.mute_bass is False
    assert config.only_bass_and_drums is False
    assert config.music_generation_mode == types.MusicGenerationMode.QUALITY
    assert config.bpm is None


def test_config_carries_the_plan() -> None:
    plan = MusicPlan(
        title="Night drive",
        prompts=[Prompt(text="synthwave"), Prompt(text="arpeggiated bass", weight=0.5)],
        bpm=110,
        scale=types.Scale.A_MAJOR_G_FLAT_MINOR,
        density=0.6,
        brightness=0.4,
        mute_drums=True,
        vocalization=True,
    )

    config = plan.to_config()

    assert (config.bpm, config.scale, config.density, config.brightness) == (
        110,
        types.Scale.A_MAJOR_G_FLAT_MINOR,
        0.6,
        0.4,
    )
    assert config.mute_drums is True
    assert config.music_generation_mode == types.MusicGenerationMode.VOCALIZATION


def test_titles_read_on_one_line() -> None:
    plan = MusicPlan(title=" Rainy\n lo-fi\u3000", prompts=[Prompt(text="lo-fi")])

    assert plan.title == "Rainy lo-fi"


@pytest.mark.parametrize(
    "fields",
    [
        {"bpm": 59},
        {"bpm": 201},
        {"density": 1.5},
        {"prompts": []},
        {"title": ""},
        {"title": " \n\t"},
    ],
)
def test_rejects_values_outside_lyria_ranges(fields: dict[str, object]) -> None:
    values: dict[str, object] = {"title": "Plan", "prompts": [Prompt(text="ambient")]} | fields

    with pytest.raises(ValidationError):
        MusicPlan.model_validate(values)


@pytest.mark.parametrize(
    "value",
    ["SCALE_UNSPECIFIED", "scale_unspecified", types.Scale.SCALE_UNSPECIFIED],
    ids=["name", "lower case", "member"],
)
def test_reads_an_unspecified_scale_as_none(value: object) -> None:
    plan = MusicPlan.model_validate(
        {"title": "Plan", "prompts": [{"text": "ambient"}], "scale": value}
    )

    assert plan.scale is None


def test_accepts_scale_names_in_any_case() -> None:
    plan = MusicPlan.model_validate(
        {"title": "Plan", "prompts": [{"text": "ambient"}], "scale": "c_major_a_minor"}
    )

    assert plan.scale is types.Scale.C_MAJOR_A_MINOR


def test_rejects_scales_lyria_does_not_have() -> None:
    with pytest.raises(ValidationError, match="unknown scale"):
        MusicPlan.model_validate(
            {"title": "Plan", "prompts": [{"text": "ambient"}], "scale": "E_MINOR"}
        )
