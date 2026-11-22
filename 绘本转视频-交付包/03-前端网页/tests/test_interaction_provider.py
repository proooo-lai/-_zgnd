"""Optional fast vision must not change import/OCR or leak credentials."""
import asyncio
import json
import os

import httpx
import pytest
from PIL import Image

from storybook.interaction import Interactor
from storybook.providers import Provider, ProviderError
from storybook.settings import Settings
from storybook.storage import Store
from tests.test_interaction import make_book, encoded_frame


def service(model):
    return {"kind": "openai", "base_url": "https://models.example/v1", "model": model,
            "api_key": "test-only-never-return"}


def configured(tmp_path, **extra):
    return Settings(data_dir=tmp_path / "data", config_file=tmp_path / "config.json",
                    providers={"vision": service("import-quality"), "tts": {"kind": "openai"}, **extra})


def test_fast_vision_only_changes_interaction_requests(tmp_path):
    async def run():
        settings = configured(tmp_path, interaction_vision=service("fast-interaction"))
        provider = Provider(settings)
        seen = []
        def reply(request):
            payload = json.loads(request.content)
            seen.append(payload["model"])
            result = {"found": True, "label": "小鸟", "description": "小鸟站在这里。",
                      "bbox_1000": [400, 300, 650, 800], "confidence": .9}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        store = Store(settings.data_dir)
        book = make_book(store)
        book["scenes"][0]["transform"] = {"kind": "generated"}
        store.save(book)
        image = tmp_path / "page.png"
        Image.new("RGB", (80, 80), "green").save(image)
        try:
            await provider.vision("Read a new page", [image])
            result = await Interactor(store, provider, settings).touch(
                "synthetic", .47, .55, 10, "s1", encoded_frame())
            assert result["object"]["label"] == "小鸟" and result["uncertain"] is False
            assert seen == ["import-quality", "fast-interaction"]
        finally:
            await provider.close()
    asyncio.run(run())


def test_optional_configuration_survives_save_and_keeps_key_private(tmp_path):
    settings = configured(tmp_path)
    settings.save_providers({"interaction_vision": service("fast-interaction")})
    reloaded = Settings(data_dir=settings.data_dir, config_file=settings.config_file)
    public = reloaded.public()
    assert public["vision"]["model"] == "import-quality"
    assert public["interaction_vision"]["model"] == "fast-interaction"
    assert public["interaction_vision"]["has_key"] is True
    assert "test-only-never-return" not in json.dumps(public)
    if os.name != "nt":
        assert settings.config_file.stat().st_mode & 0o777 == 0o600


def test_empty_optional_role_falls_back_and_runtime_updates_take_effect(tmp_path):
    async def run():
        settings = configured(tmp_path, interaction_vision={"kind": "openai"})
        provider = Provider(settings)
        reader = Interactor(Store(settings.data_dir), provider, settings)
        seen = []
        def reply(request):
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ready":true}'}}]})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            await reader.provider.vision("Check", [])
            settings.save_providers({"interaction_vision": service("fast-interaction")})
            await reader.provider.vision("Check", [])
            settings.save_providers({"interaction_vision": {"base_url": "", "model": ""}})
            await reader.provider.vision("Check", [])
            assert seen == ["import-quality", "fast-interaction", "import-quality"]
        finally:
            await provider.close()
    asyncio.run(run())


def test_incomplete_explicit_role_does_not_silently_use_different_service(tmp_path):
    async def run():
        settings = configured(tmp_path, interaction_vision={"model": "fast-interaction"})
        provider = Provider(settings)
        reader = Interactor(Store(settings.data_dir), provider, settings)
        try:
            assert reader.provider.available("vision") is False
            with pytest.raises(ProviderError, match="interaction_vision"):
                await reader.provider.vision("Check", [])
        finally:
            await provider.close()
    asyncio.run(run())
